"""ADR 069 — eject descent control: impulse rotation + ballistic descent.

Exercises Controller._eject_descent_control with a stub analyzer serving
scripted TelemetrySnapshots. simulate_os_input records key intents so
actuation is observable without a keyboard.

The criterion under test is the flight-path ANGLE (ADR 069 d1, revised): a
rate target alone is satisfied by SPEED, so a shallow dive that accelerates
meets any rate bar while flying across the arena. The stub flies at 600 KPH
(166.7 m/s), so an altitude rate of R m/s reads as asin(R / 166.7):

    -165 m/s -> -82 deg   -151 m/s -> -65 deg (target)   -136 m/s -> -55 deg (floor)
    -144 m/s -> -60 deg (inside the deadband)   -122 m/s -> -47 deg (shallow-but-fast)
"""

import threading
import time

import pytest

import wingman.controller as controller_module
from wingman.controller import AFTERBURNER_KEY, Controller, NOSE_DOWN_KEY
from wingman.telemetry import TelemetrySignal, TelemetrySnapshot, TREND_UNKNOWN


class _TelemetryStub:
    """Analyzer stand-in: snapshots built from a mutable altitude rate."""

    def __init__(self, speed=600, alt_rate=-20.0, available=True,
                 speed_trend=TREND_UNKNOWN):
        self.speed = speed
        self.alt_rate = alt_rate
        self.available = available
        self.speed_trend = speed_trend
        self.speed_rate = None
        # When True every snapshot carries the SAME altitude timestamp, i.e.
        # the sensor has not refreshed and the loop is re-reading one sample.
        self.freeze_ts = False
        self._frozen_ts = time.time()
        self.stale_speed = False
        # eject_and_dive() resets health state through these; real Lock so the
        # timeout-acquire path behaves as in production.
        self._health_lock = threading.Lock()
        self._game_battle_alive = False
        self._health_no_digits_since = 0.0
        self._death_observed = False

    def mark_health_dead_synthetic(self):
        with self._health_lock:
            self._game_battle_alive = False
            self._health_no_digits_since = 0.0
            self._death_observed = False

    def get_telemetry(self):
        if not self.available:
            return None
        now = time.time()
        sample_ts = self._frozen_ts if self.freeze_ts else now
        speed_ts = (now - 999.0) if self.stale_speed else sample_ts
        return TelemetrySnapshot(
            speed=TelemetrySignal(value=self.speed, ts=speed_ts,
                                  stable_value=float(self.speed),
                                  rate=self.speed_rate, trend=self.speed_trend),
            altitude=TelemetrySignal(value=10000, ts=sample_ts,
                                     stable_value=10000.0, rate=self.alt_rate),
            taken_at_s=now,
            stale_after_s=6.0,
        )


class _SequencedRateStub(_TelemetryStub):
    """Stub whose alt_rate follows a per-call script (last value repeats)."""

    def __init__(self, rates, **kw):
        super().__init__(alt_rate=rates[0], **kw)
        self._rates = list(rates)
        self._calls = 0

    def get_telemetry(self):
        idx = min(self._calls, len(self._rates) - 1)
        self._calls += 1
        self.alt_rate = self._rates[idx]
        return super().get_telemetry()


def _make_ctrl(monkeypatch, stub, **ecl_overrides):
    monkeypatch.setattr(controller_module, "keyboard_module", None)
    ecl = {
        "enabled": True,
        "check_interval_s": 0.05,
        "confirm_consecutive": 2,
        "target_dive_angle_deg": 65.0,
        "dive_angle_floor_deg": 55.0,
        "descent_target_mps": 100.0,
        "descent_floor_mps": 50.0,
        "rotation_pulse_s": 0.05,
        "observe_after_pulse_s": 0.05,
        "max_rotation_pulses": 4,
        "eject_max_s": 30.0,
        "over_rotation_after_s": 6.0,
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
        # Scales the descent loop's telemetry-loss tolerance to test speed
        # (production 6.0 s against a 1.5 s check interval).
        telemetry_cfg={"eject_closed_loop": ecl, "stale_after_s": 0.3},
    )


def _intents(ctrl):
    with ctrl._action_intents_lock:
        return list(ctrl._action_intents)


def _keys(ctrl):
    return [(i["action_type"], i["key"]) for i in _intents(ctrl)]


_LIVE = []


@pytest.fixture(autouse=True)
def _cleanup_threads():
    """Stop any descent thread a failed test left running — a leaked thread
    polls the stub for the rest of the pytest process."""
    yield
    while _LIVE:
        ctrl, thread = _LIVE.pop()
        ctrl._eject_stop.set()
        thread.join(timeout=1.0)


def _descent_in_thread(ctrl):
    """Run the descent controller on a thread; ballistic descent does not
    return on its own, so tests observe state and end it with the stop event."""
    result = {}

    def _run():
        result["cancelled"] = ctrl._eject_descent_control()

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    _LIVE.append((ctrl, thread))
    return thread, result


def _wait_for(predicate, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def _stop(ctrl, thread, result, expect_cancelled=True):
    ctrl._eject_stop.set()
    thread.join(timeout=5.0)
    assert not thread.is_alive()
    if expect_cancelled:
        assert result["cancelled"] is True


# ---------------------------------------------------------------------------
# Rotation: bounded impulses with a mandatory observation gap (d2)
# ---------------------------------------------------------------------------

def test_shallow_descent_issues_rotation_pulse(monkeypatch):
    stub = _TelemetryStub(alt_rate=-10.0)      # far short of the 100 target
    ctrl = _make_ctrl(monkeypatch, stub)

    thread, result = _descent_in_thread(ctrl)
    assert _wait_for(lambda: ("key_press", NOSE_DOWN_KEY) in _keys(ctrl))
    _stop(ctrl, thread, result)


def test_rotation_pulse_is_bounded_not_held(monkeypatch):
    """The key must come back UP after the pulse — continuous holding is the
    over-rotation-into-mush failure ADR 069 exists to end."""
    stub = _TelemetryStub(alt_rate=-10.0)
    ctrl = _make_ctrl(monkeypatch, stub)

    thread, result = _descent_in_thread(ctrl)
    assert _wait_for(lambda: ("key_release", NOSE_DOWN_KEY) in _keys(ctrl))
    _stop(ctrl, thread, result)


def test_pulses_are_bounded_by_the_budget(monkeypatch, caplog):
    stub = _TelemetryStub(alt_rate=-10.0)      # never reaches target
    ctrl = _make_ctrl(monkeypatch, stub, max_rotation_pulses=2)

    with caplog.at_level("INFO"):
        thread, result = _descent_in_thread(ctrl)
        thread.join(timeout=5.0)

    assert not thread.is_alive()
    assert result["cancelled"] is False
    assert ctrl._eject_phase_exit_reason == "pulses_exhausted"
    presses = sum(1 for a, k in _keys(ctrl) if a == "key_press" and k == NOSE_DOWN_KEY)
    assert presses == 2, f"expected exactly 2 pulses, saw {presses}"


# ---------------------------------------------------------------------------
# Ballistic descent (d1, d3)
# ---------------------------------------------------------------------------

def test_target_angle_establishes_dive_and_releases_nose(monkeypatch, caplog):
    stub = _TelemetryStub(alt_rate=-165.0)     # -82 deg, past the -75 target
    ctrl = _make_ctrl(monkeypatch, stub)

    with caplog.at_level("INFO"):
        thread, result = _descent_in_thread(ctrl)
        assert _wait_for(lambda: ctrl._eject_phase_exit_reason == "established")
        time.sleep(0.2)
        assert thread.is_alive()               # ballistic: still watching
        # Never pressed nose-down — the descent was already at target.
        assert ("key_press", NOSE_DOWN_KEY) not in _keys(ctrl)
        _stop(ctrl, thread, result)

    assert any("dive established" in r.message for r in caplog.records)


def test_single_sub_floor_sample_does_not_resume_rotation(monkeypatch):
    """One shallow sample is noise; the old decay detector fired on exactly
    this and produced an 18 s limit cycle."""
    # establish (2 samples), one dip, then back at target
    stub = _SequencedRateStub([-165.0, -165.0, -165.0, -20.0, -165.0, -165.0])
    ctrl = _make_ctrl(monkeypatch, stub)

    thread, result = _descent_in_thread(ctrl)
    assert _wait_for(lambda: ctrl._eject_phase_exit_reason == "established")
    time.sleep(0.4)
    assert ("key_press", NOSE_DOWN_KEY) not in _keys(ctrl)
    _stop(ctrl, thread, result)


def test_sustained_degradation_resumes_rotation(monkeypatch, caplog):
    stub = _SequencedRateStub([-165.0, -165.0, -165.0, -20.0, -20.0, -20.0, -20.0])
    ctrl = _make_ctrl(monkeypatch, stub)

    with caplog.at_level("INFO"):
        thread, result = _descent_in_thread(ctrl)
        assert _wait_for(lambda: ctrl._eject_phase_exit_reason == "established")
        assert _wait_for(lambda: ("key_press", NOSE_DOWN_KEY) in _keys(ctrl))
        _stop(ctrl, thread, result)

    assert any("dive shallow" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# Afterburner gating (d8) — the arena-exit fix
# ---------------------------------------------------------------------------

def test_afterburner_not_engaged_while_shallow(monkeypatch):
    """Burner while level/climbing is what carries the jet out of the arena."""
    stub = _TelemetryStub(alt_rate=+30.0)      # climbing after eject command
    ctrl = _make_ctrl(monkeypatch, stub)

    thread, result = _descent_in_thread(ctrl)
    assert _wait_for(lambda: ("key_press", NOSE_DOWN_KEY) in _keys(ctrl))
    assert ("key_press", AFTERBURNER_KEY) not in _keys(ctrl)
    assert ctrl._eject_ab_engaged is False
    _stop(ctrl, thread, result)


def test_afterburner_engages_once_descending(monkeypatch):
    stub = _TelemetryStub(alt_rate=-165.0)
    ctrl = _make_ctrl(monkeypatch, stub)

    thread, result = _descent_in_thread(ctrl)
    assert _wait_for(lambda: ("key_press", AFTERBURNER_KEY) in _keys(ctrl))
    assert ctrl._eject_ab_engaged is True
    _stop(ctrl, thread, result)


def test_afterburner_released_if_descent_goes_shallow(monkeypatch):
    stub = _SequencedRateStub([-165.0, -165.0, -165.0, -5.0, -5.0, -5.0])
    ctrl = _make_ctrl(monkeypatch, stub)

    thread, result = _descent_in_thread(ctrl)
    assert _wait_for(lambda: ("key_press", AFTERBURNER_KEY) in _keys(ctrl))
    assert _wait_for(lambda: ("key_release", AFTERBURNER_KEY) in _keys(ctrl))
    _stop(ctrl, thread, result)


# ---------------------------------------------------------------------------
# Over-rotation guard (ADR 068 d1/d5 carried forward)
# ---------------------------------------------------------------------------

def test_climb_after_descent_releases_as_over_rotation(monkeypatch):
    stub = _SequencedRateStub([-60.0, -60.0, +300.0, +300.0, +300.0])
    ctrl = _make_ctrl(monkeypatch, stub, over_rotation_after_s=0.01)
    ctrl._eject_nose_held_total_s = 10.0       # held long enough to have rotated past
    ctrl._eject_nose_down_since = None

    thread, result = _descent_in_thread(ctrl)
    thread.join(timeout=5.0)

    assert not thread.is_alive()
    assert result["cancelled"] is False
    assert ctrl._eject_phase_exit_reason == "over_rotation"


def test_zoom_climb_without_prior_descent_keeps_rotating(monkeypatch):
    """A momentum climb straight after the eject command is UNDER-rotation —
    nose-down is unambiguously correct, however long the key was held."""
    stub = _TelemetryStub(alt_rate=+300.0)     # only ever climbed
    ctrl = _make_ctrl(monkeypatch, stub, over_rotation_after_s=0.01)
    ctrl._eject_nose_held_total_s = 30.0
    ctrl._eject_nose_down_since = None

    thread, result = _descent_in_thread(ctrl)
    assert _wait_for(lambda: ("key_press", NOSE_DOWN_KEY) in _keys(ctrl))
    assert ctrl._eject_phase_exit_reason != "over_rotation"
    _stop(ctrl, thread, result)


def test_single_spurious_climb_sample_is_not_over_rotation(monkeypatch):
    """Aborting demands the same distinct-sample streak as establishing."""
    stub = _SequencedRateStub([-60.0, -60.0, +300.0, -60.0, -60.0, -60.0])
    ctrl = _make_ctrl(monkeypatch, stub, over_rotation_after_s=0.01)
    ctrl._eject_nose_held_total_s = 10.0
    ctrl._eject_nose_down_since = None

    thread, result = _descent_in_thread(ctrl)
    time.sleep(0.4)
    assert thread.is_alive(), "one spurious climb sample ended the descent"
    assert ctrl._eject_phase_exit_reason != "over_rotation"
    _stop(ctrl, thread, result)


# ---------------------------------------------------------------------------
# Missing data is never evidence (ADR 038, carried forward)
# ---------------------------------------------------------------------------

def test_no_telemetry_never_actuates(monkeypatch):
    stub = _TelemetryStub(available=False)
    ctrl = _make_ctrl(monkeypatch, stub)

    thread, result = _descent_in_thread(ctrl)
    thread.join(timeout=5.0)

    assert not thread.is_alive()
    assert result["cancelled"] is False
    assert ctrl._eject_phase_exit_reason == "no_telemetry"
    assert _intents(ctrl) == []                # no corrections against missing data


def test_telemetry_loss_after_established_keeps_the_verdict(monkeypatch, caplog):
    """A dive that WAS established and then loses telemetry must not be
    downgraded to a give-up — the eject succeeded."""
    stub = _TelemetryStub(alt_rate=-165.0)
    ctrl = _make_ctrl(monkeypatch, stub)

    with caplog.at_level("INFO"):
        thread, result = _descent_in_thread(ctrl)
        assert _wait_for(lambda: ctrl._eject_phase_exit_reason == "established")
        stub.available = False
        thread.join(timeout=5.0)

    assert not thread.is_alive()
    assert ctrl._eject_phase_exit_reason == "established"
    assert any("telemetry lost during descent" in r.message for r in caplog.records)


def test_fresh_altitude_with_no_rate_does_not_crash(monkeypatch):
    """altitude_fresh() does not imply a numeric rate — rate is None until two
    accepted readings exist. Production crash 2026-08-09 08:09."""
    stub = _TelemetryStub(alt_rate=None)
    ctrl = _make_ctrl(monkeypatch, stub)

    thread, result = _descent_in_thread(ctrl)
    thread.join(timeout=5.0)

    assert not thread.is_alive()
    assert result["cancelled"] is False
    assert _intents(ctrl) == []


def test_frozen_sensor_is_not_new_evidence(monkeypatch):
    """Re-polling one physical sample must not advance any streak."""
    stub = _TelemetryStub(alt_rate=-165.0)
    stub.freeze_ts = True
    ctrl = _make_ctrl(monkeypatch, stub)

    thread, result = _descent_in_thread(ctrl)
    thread.join(timeout=5.0)

    assert not thread.is_alive()
    # One frozen sample can never satisfy confirm_consecutive=2.
    assert ctrl._eject_phase_exit_reason == "no_telemetry"


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------

def test_cancellation_returns_true(monkeypatch):
    stub = _TelemetryStub(alt_rate=-165.0)
    ctrl = _make_ctrl(monkeypatch, stub)
    ctrl._eject_stop.set()

    assert ctrl._eject_descent_control() is True
    assert ctrl._eject_phase_exit_reason == "cancelled"


def test_wall_clock_backstop_ends_the_sequence(monkeypatch):
    stub = _TelemetryStub(alt_rate=-165.0)     # would stay ballistic forever
    ctrl = _make_ctrl(monkeypatch, stub, eject_max_s=0.3)

    t0 = time.time()
    thread, result = _descent_in_thread(ctrl)
    thread.join(timeout=5.0)

    assert not thread.is_alive()
    assert result["cancelled"] is False
    assert ctrl._eject_phase_exit_reason == "timeout"
    assert time.time() - t0 < 3.0


def test_eject_and_dive_releases_every_key(monkeypatch):
    """Whatever the descent controller does, the finally block must leave no
    flight key held."""
    stub = _TelemetryStub(alt_rate=-165.0)
    ctrl = _make_ctrl(monkeypatch, stub, eject_max_s=0.3)

    ctrl.eject_and_dive()
    assert _wait_for(lambda: not ctrl.is_ejecting(), timeout=10.0)

    keys = _keys(ctrl)
    for key in (NOSE_DOWN_KEY, AFTERBURNER_KEY):
        assert ("key_release", key) in keys, f"{key} never released"


# ---------------------------------------------------------------------------
# ADR 069 d1 revision — a rate target alone is satisfied by SPEED
# ---------------------------------------------------------------------------

def test_shallow_but_fast_dive_is_not_established(monkeypatch):
    """Flight-tested 2026-08-10 18:36: a -47 degree dive accelerating to 1576
    KPH held -187 to -309 m/s — three times the rate target — while flying 7 km
    ACROSS the arena. Meeting the rate bar must not end rotation when the
    flight path is still shallow."""
    # -122 m/s at the stub's 600 KPH is -47 deg: past descent_target_mps (100),
    # shallower than dive_angle_floor_deg (60).
    stub = _TelemetryStub(alt_rate=-122.0)
    ctrl = _make_ctrl(monkeypatch, stub)

    thread, result = _descent_in_thread(ctrl)
    # It must keep rotating, not settle.
    assert _wait_for(lambda: ("key_press", NOSE_DOWN_KEY) in _keys(ctrl))
    assert ctrl._eject_phase_exit_reason != "established"
    _stop(ctrl, thread, result)


def test_shallow_but_fast_dive_does_not_engage_afterburner(monkeypatch):
    """Burner in a shallow dive is what crosses the arena — gate it on the
    angle for the same reason the criterion is."""
    stub = _TelemetryStub(alt_rate=-122.0)     # -47 deg, fast
    ctrl = _make_ctrl(monkeypatch, stub)

    thread, result = _descent_in_thread(ctrl)
    assert _wait_for(lambda: ("key_press", NOSE_DOWN_KEY) in _keys(ctrl))
    assert ("key_press", AFTERBURNER_KEY) not in _keys(ctrl)
    _stop(ctrl, thread, result)


def test_established_dive_that_sags_shallow_resumes_rotation(monkeypatch, caplog):
    """The reported failure: established, then the game flattens it to -47 and
    the controller sat there because the rate still looked fine."""
    # establish at -82 deg, then sag to -47 deg while still fast.
    stub = _SequencedRateStub([-165.0, -165.0, -165.0, -122.0, -122.0, -122.0, -122.0])
    ctrl = _make_ctrl(monkeypatch, stub)

    with caplog.at_level("INFO"):
        thread, result = _descent_in_thread(ctrl)
        assert _wait_for(lambda: ctrl._eject_phase_exit_reason == "established")
        assert _wait_for(lambda: ("key_press", NOSE_DOWN_KEY) in _keys(ctrl)), \
            "sagging to -47 deg did not resume rotation"
        _stop(ctrl, thread, result)

    assert any("dive shallow" in r.message for r in caplog.records)


def test_deadband_between_target_and_floor_is_left_alone(monkeypatch):
    """Between -65 and -55 the aircraft is steep enough; pulsing at every
    degree of sag is what produced the ADR 068 limit cycle."""
    # establish at -82, then sit at -60 deg (-144 m/s) — inside the deadband.
    stub = _SequencedRateStub([-165.0, -165.0, -165.0, -144.0, -144.0, -144.0, -144.0])
    ctrl = _make_ctrl(monkeypatch, stub)

    thread, result = _descent_in_thread(ctrl)
    assert _wait_for(lambda: ctrl._eject_phase_exit_reason == "established")
    time.sleep(0.4)
    assert ("key_press", NOSE_DOWN_KEY) not in _keys(ctrl)
    _stop(ctrl, thread, result)


def test_rate_fallback_when_angle_unavailable(monkeypatch):
    """With speed stale the angle is None; the rate criterion carries the
    decision rather than blocking it."""
    stub = _TelemetryStub(alt_rate=-165.0)
    stub.stale_speed = True                    # pitch_angle_deg() -> None
    ctrl = _make_ctrl(monkeypatch, stub)

    thread, result = _descent_in_thread(ctrl)
    assert _wait_for(lambda: ctrl._eject_phase_exit_reason == "established")
    _stop(ctrl, thread, result)
