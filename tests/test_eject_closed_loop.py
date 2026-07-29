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
from wingman.telemetry import TelemetrySignal, TelemetrySnapshot


class _TelemetryStub:
    """Analyzer stand-in: serves snapshots built from a mutable alt rate."""

    def __init__(self, speed=600, alt_rate=-800.0, available=True):
        self.speed = speed
        self.alt_rate = alt_rate
        self.available = available

    def get_telemetry(self):
        if not self.available:
            return None
        now = time.time()
        return TelemetrySnapshot(
            speed=TelemetrySignal(value=self.speed, ts=now, stable_value=float(self.speed)),
            altitude=TelemetrySignal(value=10000, ts=now, stable_value=10000.0,
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
    # correction is nose-down; the rate then worsens, so measure-correct-
    # measure must reverse to a nose-up tap.
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

    thread.join(timeout=5.0)
    assert not thread.is_alive()
    assert result["cancelled"] is False

    keys = [(i["action_type"], i["key"]) for i in _intents(ctrl)]
    assert ("key_press", NOSE_UP_KEY) in keys      # reversal happened
    assert ("key_release", NOSE_UP_KEY) in keys    # tap released


def test_cancellation_during_phase_returns_true(monkeypatch):
    stub = _TelemetryStub(alt_rate=0.0)
    ctrl = _make_ctrl(monkeypatch, stub, verify_window_s=10.0, legacy_nose_hold_s=10.0)
    ctrl._eject_stop.set()

    assert ctrl._eject_nose_phase_closed_loop() is True
