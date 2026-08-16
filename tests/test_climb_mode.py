"""Unit tests for climb_mode (ADR 073 Phase 3.2b).

Controller-side: the altitude-recovery exit on fresh advancing samples (a
stalled telemetry signal must NOT end the climb), the max_climb_s backstop,
duplicate-start suppression, eject/evade suppression and pre-emption, and the
programmatic-key bracket on NOSE_UP. No real keyboard, no OCR.
"""

import threading
import time

import wingman.controller as controller_module
from wingman.controller import (
    AFTERBURNER_KEY,
    Controller,
    NOSE_UP_KEY,
)

CLIMB_KEYS = {NOSE_UP_KEY, AFTERBURNER_KEY}


class _FakeKeyboard:
    def __init__(self):
        self.events = []

    def press(self, key):
        self.events.append(("press", key, time.time()))

    def release(self, key):
        self.events.append(("release", key, time.time()))


class _AltSignal:
    def __init__(self, stable_value, ts):
        self.stable_value = stable_value
        self.ts = ts


class _Snapshot:
    def __init__(self, stable_value, ts, fresh=True):
        self.altitude = _AltSignal(stable_value, ts)
        self._fresh = fresh

    def altitude_fresh(self):
        return self._fresh


class _FakeTelemetryAnalyzer:
    """Settable stand-in for analyzer.get_telemetry()."""

    def __init__(self, stable_value=None, ts=None, fresh=True):
        self._lock = threading.Lock()
        self.set(stable_value, ts, fresh)

    def set(self, stable_value, ts, fresh=True):
        with self._lock:
            self._snap = _Snapshot(stable_value, ts, fresh)

    def get_telemetry(self):
        with self._lock:
            return self._snap


def _make_ctrl(monkeypatch, kb, analyzer, climb_cfg):
    monkeypatch.setattr(controller_module, "keyboard_module", kb)
    return Controller(
        (0, 0, 1920, 1200),
        analyzer=analyzer,
        exit_event=threading.Event(),
        capture=None,
        disable_hotkeys=True,
        climb_cfg=climb_cfg,
    )


CFG = {"enabled": True, "enter_below_alt": 500, "exit_above_alt": 1000,
       "confirm_reads": 2, "max_climb_s": 5.0}


def _wait_done(ctrl, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not ctrl.is_climbing():
            return True
        time.sleep(0.02)
    return False


def _presses(kb, key):
    return [e for e in kb.events if e[0] == "press" and e[1] == key]


def _releases(kb, key):
    return [e for e in kb.events if e[0] == "release" and e[1] == key]


def test_exits_on_confirmed_fresh_recovery(monkeypatch):
    t0 = time.time()
    analyzer = _FakeTelemetryAnalyzer(stable_value=300.0, ts=t0)
    kb = _FakeKeyboard()
    ctrl = _make_ctrl(monkeypatch, kb, analyzer, CFG)

    ctrl.climb_mode()
    assert ctrl.is_climbing()
    # Two fresh reads above exit with ADVANCING timestamps.
    time.sleep(0.3)
    analyzer.set(1100.0, t0 + 0.1)
    time.sleep(0.3)
    analyzer.set(1150.0, t0 + 0.2)

    assert _wait_done(ctrl), "climb did not end on altitude recovery"
    held = time.time() - t0
    assert held < 4.0, f"climb ran {held:.1f}s — ended by cap, not recovery"
    for key in CLIMB_KEYS:
        assert _presses(kb, key), f"'{key}' never pressed"
        assert _releases(kb, key), f"'{key}' never released"


def test_stalled_signal_does_not_end_climb(monkeypatch):
    """The same high stable value with an unchanged ts is one sample read
    repeatedly — it must count once, so the climb runs to the cap."""
    t0 = time.time()
    analyzer = _FakeTelemetryAnalyzer(stable_value=2000.0, ts=t0)
    kb = _FakeKeyboard()
    cfg = dict(CFG, max_climb_s=1.0)
    ctrl = _make_ctrl(monkeypatch, kb, analyzer, cfg)

    ctrl.climb_mode()
    assert _wait_done(ctrl)
    held = time.time() - t0
    assert held >= 0.9, f"climb ended after {held:.2f}s — stalled ts counted twice"


def test_max_climb_backstop(monkeypatch):
    analyzer = _FakeTelemetryAnalyzer(stable_value=None, ts=None, fresh=False)
    kb = _FakeKeyboard()
    cfg = dict(CFG, max_climb_s=0.5)
    ctrl = _make_ctrl(monkeypatch, kb, analyzer, cfg)

    t0 = time.time()
    ctrl.climb_mode()
    assert _wait_done(ctrl), "climb did not end at the backstop"
    for key in CLIMB_KEYS:
        assert _releases(kb, key), f"'{key}' never released"


def test_duplicate_start_suppressed(monkeypatch):
    analyzer = _FakeTelemetryAnalyzer(stable_value=None, ts=None, fresh=False)
    kb = _FakeKeyboard()
    cfg = dict(CFG, max_climb_s=2.0)
    ctrl = _make_ctrl(monkeypatch, kb, analyzer, cfg)

    ctrl.climb_mode()
    ctrl.climb_mode()
    time.sleep(0.45)   # first pitch pulse fires on the first 0.25s tick
    assert len(_presses(kb, NOSE_UP_KEY)) == 1, "second start pressed keys again"
    ctrl._climb_stop.set()
    assert _wait_done(ctrl)


def test_suppressed_while_ejecting(monkeypatch):
    analyzer = _FakeTelemetryAnalyzer(stable_value=None, ts=None, fresh=False)
    kb = _FakeKeyboard()
    ctrl = _make_ctrl(monkeypatch, kb, analyzer, CFG)
    ctrl._ejecting.set()
    ctrl.climb_mode()
    assert not ctrl.is_climbing()
    assert not kb.events


def test_evade_preempts_running_climb(monkeypatch):
    analyzer = _FakeTelemetryAnalyzer(stable_value=None, ts=None, fresh=False)
    kb = _FakeKeyboard()
    cfg = dict(CFG, max_climb_s=10.0)
    ctrl = _make_ctrl(monkeypatch, kb, analyzer, cfg)

    t0 = time.time()
    ctrl.climb_mode()
    assert ctrl.is_climbing()
    time.sleep(0.2)
    ctrl._missile_evading.set()
    assert _wait_done(ctrl), "climb did not yield to the evade"
    assert time.time() - t0 < 5.0
    for key in CLIMB_KEYS:
        assert _releases(kb, key), f"'{key}' never released"


def test_nose_up_programmatic_bracket(monkeypatch):
    """NOSE_UP is a watched maneuver key — the hold must bracket it so
    auto-repeats are not read as a manual takeover."""
    analyzer = _FakeTelemetryAnalyzer(stable_value=None, ts=None, fresh=False)
    kb = _FakeKeyboard()
    cfg = dict(CFG, max_climb_s=0.4)
    ctrl = _make_ctrl(monkeypatch, kb, analyzer, cfg)

    ctrl.climb_mode()
    time.sleep(0.15)
    assert ctrl._programmatic_key_counts.get(NOSE_UP_KEY, 0) >= 1
    assert _wait_done(ctrl)
    assert ctrl._programmatic_key_counts.get(NOSE_UP_KEY, 0) == 0


def test_no_start_without_exit_threshold(monkeypatch):
    analyzer = _FakeTelemetryAnalyzer()
    kb = _FakeKeyboard()
    ctrl = _make_ctrl(monkeypatch, kb, analyzer, {"enabled": True})
    ctrl.climb_mode()
    assert not ctrl.is_climbing()
    assert not kb.events


def test_target_alt_overrides_config_band(monkeypatch):
    """ADR 073 3.2c: the mission prologue passes its operating-altitude
    target — reads above the config band (1000) but below the target must
    NOT end the climb."""
    t0 = time.time()
    analyzer = _FakeTelemetryAnalyzer(stable_value=300.0, ts=t0)
    kb = _FakeKeyboard()
    cfg = dict(CFG, max_climb_s=1.2)
    ctrl = _make_ctrl(monkeypatch, kb, analyzer, cfg)

    ctrl.climb_mode(target_alt=7000, max_s=1.2)
    time.sleep(0.3)
    analyzer.set(2000.0, t0 + 0.1)    # above band, far below target
    time.sleep(0.3)
    analyzer.set(2500.0, t0 + 0.2)
    assert ctrl.is_climbing(), "climb ended below the mission target"
    assert _wait_done(ctrl)          # ends via the cap
    held = time.time() - t0
    assert held >= 1.0


def test_target_alt_reached_ends_climb(monkeypatch):
    t0 = time.time()
    analyzer = _FakeTelemetryAnalyzer(stable_value=6500.0, ts=t0)
    kb = _FakeKeyboard()
    ctrl = _make_ctrl(monkeypatch, kb, analyzer, dict(CFG, max_climb_s=8.0))

    ctrl.climb_mode(target_alt=7000, max_s=8.0)
    time.sleep(0.3)
    analyzer.set(7100.0, t0 + 0.1)
    time.sleep(0.3)
    analyzer.set(7200.0, t0 + 0.2)
    assert _wait_done(ctrl)
    assert time.time() - t0 < 6.0, "ended by cap, not target confirmation"


def test_mission_climb_config_defaults(monkeypatch):
    kb = _FakeKeyboard()
    ctrl = _make_ctrl(monkeypatch, kb, _FakeTelemetryAnalyzer(), CFG)
    assert ctrl._mission_climb_alt == 7000.0
    assert ctrl._mission_climb_max_s == 90.0


def test_pitch_is_pulsed_not_held(monkeypatch):
    """3.2c loop finding: NOSE_UP must be released between pulses while
    AFTERBURNER stays held — a continuously held nose-up loops the aircraft
    (2026-08-15 20:24: 60s held, alt oscillated 1650-2400, zero net gain)."""
    analyzer = _FakeTelemetryAnalyzer(stable_value=None, ts=None, fresh=False)
    kb = _FakeKeyboard()
    cfg = dict(CFG, max_climb_s=2.0, pitch_pulse_s=0.3, pulse_observe_s=0.4)
    ctrl = _make_ctrl(monkeypatch, kb, analyzer, cfg)

    ctrl.climb_mode()
    assert _wait_done(ctrl, timeout=4.0)
    nose_presses = len(_presses(kb, NOSE_UP_KEY))
    nose_releases = len(_releases(kb, NOSE_UP_KEY))
    assert nose_presses >= 2, f"expected pulsed NOSE_UP, got {nose_presses} press(es)"
    assert nose_releases >= nose_presses, "pulse releases missing"
    assert len(_presses(kb, AFTERBURNER_KEY)) == 1, "AB must be held, not pulsed"


def test_healthy_climb_rate_suppresses_pulse(monkeypatch):
    """While the telemetry climb rate meets min_climb_rate, no new pitch
    pulse fires — the aircraft is already climbing."""
    t0 = time.time()
    analyzer = _FakeTelemetryAnalyzer(stable_value=3000.0, ts=t0)
    analyzer._snap.altitude.rate = 100.0   # healthy climb
    kb = _FakeKeyboard()
    cfg = dict(CFG, max_climb_s=1.2, pitch_pulse_s=0.2, pulse_observe_s=0.2,
               min_climb_rate=30.0)
    ctrl = _make_ctrl(monkeypatch, kb, analyzer, cfg)

    ctrl.climb_mode(target_alt=7000, max_s=1.2)
    # First pulse fires immediately (rate unknown until first poll); after the
    # first fresh sample reports rate=100, no further pulses may fire.
    time.sleep(0.3)
    analyzer.set(3100.0, t0 + 0.1)
    analyzer._snap.altitude.rate = 100.0
    time.sleep(0.4)
    presses_after_rate = len(_presses(kb, NOSE_UP_KEY))
    time.sleep(0.4)
    assert len(_presses(kb, NOSE_UP_KEY)) == presses_after_rate, \
        "pulse fired despite healthy climb rate"
    assert _wait_done(ctrl)
