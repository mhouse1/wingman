"""ADR 038 — closed-loop eject_and_dive nose-direction verification tests.

Exercises Controller._eject_nose_phase_closed_loop with a stub analyzer that
serves scripted TelemetrySnapshots. simulate_os_input records key intents so
corrective inputs are observable without a keyboard.
"""

import threading
import time

import pytest

import wingman.controller as controller_module
from wingman.controller import Controller, NOSE_DOWN_KEY, NOSE_UP_KEY
from wingman.telemetry import TelemetrySignal, TelemetrySnapshot, TREND_UNKNOWN


class _TelemetryStub:
    """Analyzer stand-in: serves snapshots built from a mutable alt rate."""

    def __init__(self, speed=600, alt_rate=-800.0, available=True, speed_trend=TREND_UNKNOWN):
        self.speed = speed
        self.alt_rate = alt_rate
        self.available = available
        self.speed_trend = speed_trend
        # When True every snapshot carries the SAME altitude timestamp, i.e. the
        # sensor has not refreshed and the loop is re-reading one physical sample.
        self.freeze_ts = False
        self._frozen_ts = time.time()
        # When True the speed signal is aged out, so pitch_band() returns None
        # while altitude stays fresh.
        self.stale_speed = False
        # eject_and_dive() resets health state through these; real Lock so the
        # timeout-acquire path behaves as in production.
        self._health_lock = threading.Lock()
        self._game_battle_alive = False
        self._health_no_digits_since = 0.0

    def get_telemetry(self):
        if not self.available:
            return None
        now = time.time()
        sample_ts = self._frozen_ts if self.freeze_ts else now
        speed_ts = (now - 999.0) if self.stale_speed else sample_ts
        return TelemetrySnapshot(
            speed=TelemetrySignal(value=self.speed, ts=speed_ts, stable_value=float(self.speed),
                                   trend=self.speed_trend),
            altitude=TelemetrySignal(value=10000, ts=sample_ts, stable_value=10000.0,
                                     rate=self.alt_rate),
            taken_at_s=now,
            stale_after_s=6.0,
        )


def _make_ctrl(monkeypatch, stub, **ecl_overrides):
    monkeypatch.setattr(controller_module, "keyboard_module", None)
    ecl = {
        "enabled": True,
        "verify_window_s": 0.15,
        "check_interval_s": 0.05,
        "max_corrections": 2,
        "legacy_nose_hold_s": 0.5,
    }
    ecl.update(ecl_overrides)
    return Controller(
        (0, 0, 1920, 1200),
        analyzer=stub,
        exit_event=threading.Event(),
        capture=None,
        simulate_os_input=True,
        disable_hotkeys=True,
        telemetry_cfg={"eject_closed_loop": ecl},
    )


def _intents(ctrl):
    with ctrl._action_intents_lock:
        return list(ctrl._action_intents)


def test_steep_dive_confirmed_exits_without_corrections(monkeypatch):
    stub = _TelemetryStub(alt_rate=-800.0)  # ratio -0.91 at 600 MPH → steep dive
    ctrl = _make_ctrl(monkeypatch, stub)

    t0 = time.time()
    cancelled = ctrl._eject_nose_phase_closed_loop()

    assert cancelled is False
    assert time.time() - t0 < 1.0
    assert _intents(ctrl) == []  # no corrective inputs issued


def test_no_telemetry_falls_back_to_legacy_timer(monkeypatch):
    stub = _TelemetryStub(available=False)
    ctrl = _make_ctrl(monkeypatch, stub, legacy_nose_hold_s=0.3)

    t0 = time.time()
    cancelled = ctrl._eject_nose_phase_closed_loop()

    assert cancelled is False
    assert time.time() - t0 >= 0.25  # held for (roughly) the legacy window
    assert _intents(ctrl) == []      # absence of data never triggers corrections


def test_level_flight_triggers_nose_down_reissue(monkeypatch):
    stub = _TelemetryStub(alt_rate=0.0)  # flying straight — unambiguous
    ctrl = _make_ctrl(monkeypatch, stub, max_corrections=1)

    cancelled = ctrl._eject_nose_phase_closed_loop()

    assert cancelled is False
    keys = [(i["action_type"], i["key"]) for i in _intents(ctrl)]
    assert ("key_release", NOSE_DOWN_KEY) in keys
    assert ("key_press", NOSE_DOWN_KEY) in keys
    assert all(k != NOSE_UP_KEY for _, k in keys)  # never nose-up for level flight


def test_shallow_dive_not_improving_reverses_to_nose_up(monkeypatch):
    # Shallow dive (-200 ft/s at 600 MPH → ratio -0.23, dive band). First
    # correction is nose-down; the rate then worsens WITH a rising speed
    # trend (the past-vertical-over-rotation signature: gravity/afterburner
    # still accelerating it), so measure-correct-measure must reverse to a
    # nose-up tap.
    stub = _TelemetryStub(alt_rate=-200.0)
    ctrl = _make_ctrl(monkeypatch, stub, max_corrections=2)

    result = {}

    def _run():
        result["cancelled"] = ctrl._eject_nose_phase_closed_loop()

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()

    # Wait for the first (nose-down) correction to appear, then worsen the rate.
    deadline = time.time() + 3.0
    while time.time() < deadline:
        if any(i["key"] == NOSE_DOWN_KEY and i["action_type"] == "key_release"
               for i in _intents(ctrl)):
            break
        time.sleep(0.02)
    else:
        pytest.fail("first corrective nose-down re-issue never happened")
    stub.alt_rate = -150.0  # worse than the -200 before the correction
    stub.speed_trend = "rising"

    thread.join(timeout=5.0)
    assert not thread.is_alive()
    assert result["cancelled"] is False

    keys = [(i["action_type"], i["key"]) for i in _intents(ctrl)]
    assert ("key_press", NOSE_UP_KEY) in keys      # reversal happened
    assert ("key_release", NOSE_UP_KEY) in keys    # tap released


def test_climbing_never_reverses_to_nose_up(monkeypatch):
    """Regression: a CLIMBING aircraft must never get a nose-up tap.

    Nose-down is unambiguously correct while the aircraft is going up, no
    matter how many times it has failed to take. Production logs 2026-07-30
    06:34:31 caught the opposite ("band=level, alt rate +153 ft/s ->
    corrective nose-up") after an earlier fix dropped the descending-only
    condition, and the tap pitched the aircraft into a loop.
    """
    stub = _TelemetryStub(alt_rate=-50.0)  # shallow descent, level band at 600 MPH
    ctrl = _make_ctrl(monkeypatch, stub, max_corrections=2)

    result = {}

    def _run():
        result["cancelled"] = ctrl._eject_nose_phase_closed_loop()

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()

    deadline = time.time() + 3.0
    while time.time() < deadline:
        if any(i["key"] == NOSE_DOWN_KEY and i["action_type"] == "key_release"
               for i in _intents(ctrl)):
            break
        time.sleep(0.02)
    else:
        pytest.fail("first corrective nose-down re-issue never happened")
    # Now CLIMBING (+100 ft/s): worse than the -50 before the correction, but
    # the sign of the rate says "still going up", so nose-up must not fire.
    stub.alt_rate = 100.0
    stub.speed_trend = "rising"

    thread.join(timeout=5.0)
    assert not thread.is_alive()
    assert result["cancelled"] is False

    keys = [(i["action_type"], i["key"]) for i in _intents(ctrl)]
    assert all(k != NOSE_UP_KEY for _, k in keys)  # never nose-up while climbing


def test_descending_and_worsening_reverses_regardless_of_speed_trend(monkeypatch):
    """Regression: the reversal must not be gated on a rising speed trend.

    Climbing trades speed for altitude, so "descent got shallower" and "speed
    rising" are anti-correlated by conservation of energy. Gating the reversal
    on TREND_RISING made nose-up unreachable: in the 2026-07-30 16:27 session
    it blocked all 8 of 8 corrections that passed the rate-worsened test, and
    every eject burned its full correction budget without ever establishing a
    dive. Descending + descent-got-shallower is the over-rotation signature on
    its own.
    """
    stub = _TelemetryStub(alt_rate=-200.0, speed_trend="falling")
    ctrl = _make_ctrl(monkeypatch, stub, max_corrections=2)

    result = {}

    def _run():
        result["cancelled"] = ctrl._eject_nose_phase_closed_loop()

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()

    deadline = time.time() + 3.0
    while time.time() < deadline:
        if any(i["key"] == NOSE_DOWN_KEY and i["action_type"] == "key_release"
               for i in _intents(ctrl)):
            break
        time.sleep(0.02)
    else:
        pytest.fail("first corrective nose-down re-issue never happened")
    # Still descending, but the descent got shallower after our nose-down.
    stub.alt_rate = -150.0

    thread.join(timeout=5.0)
    assert not thread.is_alive()
    assert result["cancelled"] is False

    keys = [(i["action_type"], i["key"]) for i in _intents(ctrl)]
    assert ("key_press", NOSE_UP_KEY) in keys      # reversal fired despite falling speed
    assert ("key_release", NOSE_UP_KEY) in keys


def test_sustained_descent_confirms_without_steep_band(monkeypatch):
    """ADR 058: a raw sustained descent confirms even when the sine band cannot.

    At 600 MPH (880 ft/s) a -300 ft/s descent is ratio -0.34 — the DIVE band,
    nowhere near steep_min_sin 0.8. Production flight never exceeded 0.346 in
    30 minutes, so the ratio path alone confirmed 0 times; the raw descent
    rate is the reachable signal.
    """
    stub = _TelemetryStub(alt_rate=-300.0)
    ctrl = _make_ctrl(monkeypatch, stub, confirm_descent_fps=250.0)

    cancelled = ctrl._eject_nose_phase_closed_loop()

    assert cancelled is False
    assert ctrl._eject_phase_exit_reason == "confirmed"
    assert _intents(ctrl) == []  # confirmed outright, no corrective inputs


def test_confirmation_requires_distinct_samples_not_repeated_polls(monkeypatch):
    """Regression: confirm_consecutive must count distinct telemetry samples.

    The loop polls every check_interval_s but telemetry only refreshes every
    ~3.0s (ocr_every_n_ticks=2), so counting polls let ONE physical reading
    satisfy confirm_consecutive=2 by being read twice — defeating the
    low-speed-transient protection ADR 038 added it for.

    The stub keeps the sample FRESH (rolling taken_at_s, so pitch_band stays
    valid and staleness is never the reason we exit) while pinning the sample
    timestamp, i.e. a sensor that is being polled but has not refreshed. Without
    the dedup this confirms on the second poll; with it, it never confirms and
    the phase leaves on the legacy timer.
    """
    stub = _TelemetryStub(alt_rate=-800.0)  # steep AND past confirm_descent_fps
    stub.freeze_ts = True  # every poll returns the SAME physical sample
    ctrl = _make_ctrl(monkeypatch, stub, confirm_consecutive=2,
                      legacy_nose_hold_s=0.45, verify_window_s=10.0)

    t0 = time.time()
    cancelled = ctrl._eject_nose_phase_closed_loop()
    elapsed = time.time() - t0

    assert cancelled is False
    # Left on the legacy timer, NOT via confirmation.
    assert ctrl._eject_phase_exit_reason == "no_telemetry"
    # And it left promptly on that timer rather than sitting until the 6s
    # staleness horizon — proving the dedup, not staleness, blocked the confirm.
    assert elapsed < 3.0, f"exited via staleness, not dedup (took {elapsed:.1f}s)"


def test_descent_rate_confirms_even_when_speed_is_stale(monkeypatch):
    """ADR 058's descent-rate path must survive a stale SPEED signal.

    pitch_band() returns None when either signal is stale, so evaluating the
    band bail-out first skipped the descent-rate confirmation exactly when
    speed was unavailable — the one case it exists to survive. Measured on the
    2026-07-30 18:51 session: 9 samples inside eject windows had a fresh
    altitude rate past the confirm threshold but were discarded on stale speed.
    """
    stub = _TelemetryStub(alt_rate=-400.0)
    stub.stale_speed = True  # speed unusable -> pitch_band() returns None
    ctrl = _make_ctrl(monkeypatch, stub, confirm_descent_fps=250.0,
                      confirm_consecutive=2, legacy_nose_hold_s=10.0,
                      verify_window_s=10.0)

    cancelled = ctrl._eject_nose_phase_closed_loop()

    assert cancelled is False
    assert ctrl._eject_phase_exit_reason == "confirmed"


def test_stale_speed_and_frozen_altitude_never_corrects(monkeypatch):
    """Combined missing-evidence path: stale speed AND a frozen altitude sample.

    band is None (stale speed) and descending_hard is True but the sample never
    refreshes — the loop must fall back to the legacy timer WITHOUT issuing a
    single corrective input (ADR 038: never correct against missing data), and
    without confirming off one physical reading.
    """
    stub = _TelemetryStub(alt_rate=-400.0)
    stub.stale_speed = True
    stub.freeze_ts = True
    ctrl = _make_ctrl(monkeypatch, stub, confirm_descent_fps=250.0,
                      confirm_consecutive=2, legacy_nose_hold_s=0.45,
                      verify_window_s=10.0)

    t0 = time.time()
    cancelled = ctrl._eject_nose_phase_closed_loop()
    elapsed = time.time() - t0

    assert cancelled is False
    assert ctrl._eject_phase_exit_reason == "no_telemetry"
    assert elapsed < 3.0
    assert _intents(ctrl) == []  # no corrections against missing data


def test_dive_confirms_post_release(monkeypatch):
    """ADR 058 decision 10: confirmation keeps running after nose release.

    The dive typically establishes after the nose phase ends (63 eligible
    samples post-release vs 4 in-phase on 2026-07-30). Here the nose phase
    gives up on the legacy timer with no telemetry, then telemetry comes back
    deep in the afterburner-hold — the sequence must still record 'confirmed'.
    """
    stub = _TelemetryStub(alt_rate=-400.0, available=False)  # nose phase: nothing
    ctrl = _make_ctrl(monkeypatch, stub, confirm_descent_fps=250.0,
                      confirm_consecutive=2, legacy_nose_hold_s=0.2,
                      verify_window_s=10.0)

    ctrl.eject_and_dive()
    # Nose phase exits on the legacy timer; then the sensor comes back mid-dive.
    deadline = time.time() + 3.0
    while time.time() < deadline and ctrl._eject_phase_exit_reason != "no_telemetry":
        time.sleep(0.02)
    stub.available = True

    deadline = time.time() + 5.0
    while time.time() < deadline and ctrl._eject_phase_exit_reason != "confirmed":
        time.sleep(0.02)
    ctrl.stop_eject_sequence()

    assert ctrl._eject_phase_exit_reason == "confirmed", (
        f"post-release confirmation never fired (exit reason: "
        f"{ctrl._eject_phase_exit_reason!r})"
    )


def test_two_distinct_samples_do_confirm(monkeypatch):
    """Companion to the dedup test: distinct samples must still confirm.

    Guards against the dedup being so strict that a real two-sample
    confirmation can never happen.
    """
    stub = _TelemetryStub(alt_rate=-800.0)
    ctrl = _make_ctrl(monkeypatch, stub, confirm_consecutive=2,
                      legacy_nose_hold_s=10.0, verify_window_s=10.0)

    cancelled = ctrl._eject_nose_phase_closed_loop()

    assert cancelled is False
    assert ctrl._eject_phase_exit_reason == "confirmed"


def test_total_nose_budget_caps_the_hold(monkeypatch):
    """Nose-down must not be held indefinitely across phases/re-entries.

    Budget comes from config through the real constructor, and the clock is
    driven by the production hold accounting (_account_nose_hold via
    _eject_key) rather than by setting a deadline attribute directly.
    """
    stub = _TelemetryStub(alt_rate=0.0)  # level: never confirms, always corrects
    ctrl = _make_ctrl(monkeypatch, stub, max_corrections=99,
                      verify_window_s=0.05, legacy_nose_hold_s=99.0,
                      total_nose_budget_s=0.6)
    assert ctrl._eject_cl_total_nose_budget_s == 0.6  # config actually reached the controller

    # Open the budget exactly the way eject_and_dive() does, then press
    # NOSE_DOWN through the normal path so the hold clock starts.
    ctrl._eject_nose_held_total_s = 0.0
    ctrl._eject_nose_down_since = None
    ctrl._eject_key(True, NOSE_DOWN_KEY)

    t0 = time.time()
    cancelled = ctrl._eject_nose_phase_closed_loop()
    elapsed = time.time() - t0

    assert cancelled is False
    assert ctrl._eject_phase_exit_reason == "nose_budget_exhausted"
    assert elapsed < 3.0, f"budget cap did not bound the hold (took {elapsed:.1f}s)"


def test_nose_budget_ignores_time_when_nose_is_up(monkeypatch):
    """The budget must measure HELD time, not wall clock since eject start.

    The afterburner hold phase runs up to 120s with nose-down released; charging
    that to the nose budget would make every dive-decay re-entry an instant
    no-op.
    """
    stub = _TelemetryStub(alt_rate=0.0)
    ctrl = _make_ctrl(monkeypatch, stub, total_nose_budget_s=0.5)
    ctrl._eject_nose_held_total_s = 0.0
    ctrl._eject_nose_down_since = None

    # Nose up the whole time: plenty of wall clock passes, budget untouched.
    time.sleep(0.7)
    assert ctrl._eject_nose_budget_exhausted() is False

    # Now actually hold it past the budget.
    ctrl._eject_key(True, NOSE_DOWN_KEY)
    time.sleep(0.6)
    assert ctrl._eject_nose_budget_exhausted() is True

    # Releasing banks the elapsed hold rather than resetting it.
    ctrl._eject_key(False, NOSE_DOWN_KEY)
    assert ctrl._eject_nose_held_total_s >= 0.5
    assert ctrl._eject_nose_budget_exhausted() is True


def test_cancellation_during_phase_returns_true(monkeypatch):
    stub = _TelemetryStub(alt_rate=0.0)
    ctrl = _make_ctrl(monkeypatch, stub, verify_window_s=10.0, legacy_nose_hold_s=10.0)
    ctrl._eject_stop.set()

    assert ctrl._eject_nose_phase_closed_loop() is True
