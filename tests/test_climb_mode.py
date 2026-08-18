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
    def __init__(self, stable_value, ts, fresh=True, angle=None):
        self.altitude = _AltSignal(stable_value, ts)
        self._fresh = fresh
        self._angle = angle

    def altitude_fresh(self):
        return self._fresh

    def pitch_angle_deg(self):
        return self._angle


class _FakeTelemetryAnalyzer:
    """Settable stand-in for analyzer.get_telemetry() and the fuel read."""

    def __init__(self, stable_value=None, ts=None, fresh=True, fuel=None):
        self._lock = threading.Lock()
        self._fuel = fuel
        self.set(stable_value, ts, fresh)

    def set(self, stable_value, ts, fresh=True, angle=None):
        with self._lock:
            self._snap = _Snapshot(stable_value, ts, fresh, angle)

    def set_fuel(self, fuel):
        with self._lock:
            self._fuel = fuel

    def get_afterburner_fuel_pct(self):
        with self._lock:
            return self._fuel

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


def test_fuel_config_defaults(monkeypatch):
    """ADR 075: the prologue fields are gone (sustain climb owns mission
    altitude); the fuel rearm margin defaults without a fuel config block."""
    kb = _FakeKeyboard()
    ctrl = _make_ctrl(monkeypatch, kb, _FakeTelemetryAnalyzer(), CFG)
    assert not hasattr(ctrl, "_mission_climb_alt")
    assert ctrl._fuel_rearm_margin == 5.0


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


# ---------------------------------------------------------------------------
# ADR 075: fuel-gated afterburner in the climb hold
# ---------------------------------------------------------------------------

def test_climb_burner_respects_fuel_floor(monkeypatch):
    """Sustain climbs pass the evade reserve as the floor: the burner releases
    at the floor (leaving the reserve for a missile alert and letting the game
    recharge), and re-engages only after the rearm margin refills."""
    analyzer = _FakeTelemetryAnalyzer(stable_value=None, ts=None, fresh=False,
                                      fuel=50)
    kb = _FakeKeyboard()
    cfg = dict(CFG, max_climb_s=6.0)
    ctrl = _make_ctrl(monkeypatch, kb, analyzer, cfg)

    ctrl.climb_mode(target_alt=7000.0, max_s=6.0, fuel_floor_pct=10.0)
    time.sleep(0.4)
    assert _presses(kb, AFTERBURNER_KEY), "burner not engaged with fuel above floor"

    analyzer.set_fuel(10)          # at the reserve floor
    time.sleep(0.6)
    assert ctrl.is_climbing(), "climb must continue without burner"
    assert _releases(kb, AFTERBURNER_KEY), "burner not released at the reserve floor"

    analyzer.set_fuel(30)          # >= floor + rearm margin (default 5)
    time.sleep(0.6)
    assert len(_presses(kb, AFTERBURNER_KEY)) >= 2, "burner not re-engaged after recovery"

    ctrl._climb_stop.set()
    assert _wait_done(ctrl)


def test_climb_starts_without_burner_when_fuel_empty(monkeypatch):
    """Emergency floor is 0: at 0% the burner is off in-game and holding the
    key blocks recharge, so the climb starts on pitch alone."""
    analyzer = _FakeTelemetryAnalyzer(stable_value=None, ts=None, fresh=False,
                                      fuel=0)
    kb = _FakeKeyboard()
    cfg = dict(CFG, max_climb_s=1.0)
    ctrl = _make_ctrl(monkeypatch, kb, analyzer, cfg)

    ctrl.climb_mode()
    time.sleep(0.4)
    assert not _presses(kb, AFTERBURNER_KEY), "burner pressed with an empty tank"
    ctrl._climb_stop.set()
    assert _wait_done(ctrl)


def test_climb_unknown_fuel_keeps_legacy_burner_hold(monkeypatch):
    """No fuel reading (OCR dropout, test doubles) must not change behavior:
    the burner is held as before ADR 075 (freeze policy)."""
    analyzer = _FakeTelemetryAnalyzer(stable_value=None, ts=None, fresh=False,
                                      fuel=None)
    kb = _FakeKeyboard()
    cfg = dict(CFG, max_climb_s=1.0)
    ctrl = _make_ctrl(monkeypatch, kb, analyzer, cfg)

    ctrl.climb_mode()
    time.sleep(0.3)
    assert _presses(kb, AFTERBURNER_KEY), "burner must be held when fuel is unknown"
    ctrl._climb_stop.set()
    assert _wait_done(ctrl)


def test_manual_takeover_state_ends_climb(monkeypatch):
    """SAF-001: the FSM entering GAME_BATTLE_MANUAL must end a running climb
    — the 2026-08-17 session showed the hold pulsing nose-up 45 s into
    manual flight because nothing stopped the thread."""
    from wingman.analyzer import GameState

    analyzer = _FakeTelemetryAnalyzer(stable_value=None, ts=None, fresh=False)
    analyzer.game_state = GameState.GAME_BATTLE
    kb = _FakeKeyboard()
    cfg = dict(CFG, max_climb_s=10.0)
    ctrl = _make_ctrl(monkeypatch, kb, analyzer, cfg)

    t0 = time.time()
    ctrl.climb_mode()
    assert ctrl.is_climbing()
    time.sleep(0.2)
    analyzer.game_state = GameState.GAME_BATTLE_MANUAL
    assert _wait_done(ctrl), "climb did not end on GAME_BATTLE_MANUAL"
    assert time.time() - t0 < 5.0, "climb ended by cap, not the state exit"
    for key in CLIMB_KEYS:
        assert _releases(kb, key), f"'{key}' never released"


def test_takeover_handler_stops_running_climb(monkeypatch):
    """SAF-001: a physical maneuver key must trigger takeover and stop the
    climb hold even with NO mission thread running (the tree selects Climb
    with mission=False after a respawn cancels the mission)."""
    analyzer = _FakeTelemetryAnalyzer(stable_value=None, ts=None, fresh=False)
    kb = _FakeKeyboard()
    cfg = dict(CFG, max_climb_s=10.0)
    ctrl = _make_ctrl(monkeypatch, kb, analyzer, cfg)

    ctrl.climb_mode()
    assert ctrl.is_climbing()
    time.sleep(0.2)
    assert not ctrl.is_mission_running()
    took_over = ctrl._handle_maneuver_key_press("l")
    assert took_over, "maneuver key did not trigger takeover during a bare climb hold"
    assert _wait_done(ctrl), "takeover did not stop the climb hold"
    for key in CLIMB_KEYS:
        assert _releases(kb, key), f"'{key}' never released"


# ---------------------------------------------------------------------------
# ADR 076 d3: nose-down over-rotation ceiling
# ---------------------------------------------------------------------------

def test_nose_down_pulse_above_rate_ceiling(monkeypatch):
    """Above max_climb_rate the pulse controller rotates BACK DOWN — the
    spawn guard can pre-load pitch before the climb thread starts, and
    declining to add nose-up is not enough to prevent the loop."""
    from wingman.controller import NOSE_DOWN_KEY

    t0 = time.time()
    analyzer = _FakeTelemetryAnalyzer(stable_value=3000.0, ts=t0)
    kb = _FakeKeyboard()
    cfg = dict(CFG, max_climb_s=2.5, pitch_pulse_s=0.2, pulse_observe_s=0.2,
               min_climb_rate=30.0, max_climb_rate=100.0)
    ctrl = _make_ctrl(monkeypatch, kb, analyzer, cfg)

    ctrl.climb_mode(target_alt=7000, max_s=2.5)
    # Feed fresh advancing samples reporting a rate far above the ceiling.
    for i in range(6):
        time.sleep(0.25)
        analyzer.set(3000.0 + i, t0 + 0.1 * (i + 1))
        analyzer._snap.altitude.rate = 500.0
    ctrl._climb_stop.set()
    assert _wait_done(ctrl)
    assert _presses(kb, NOSE_DOWN_KEY), \
        "no nose-down pulse despite rate far above max_climb_rate"
    assert _releases(kb, NOSE_DOWN_KEY), "nose-down never released"


def test_no_pulse_between_rate_bands(monkeypatch):
    """Between min_climb_rate and max_climb_rate: no input in either
    direction — the aircraft is flying the climb correctly."""
    from wingman.controller import NOSE_DOWN_KEY

    t0 = time.time()
    analyzer = _FakeTelemetryAnalyzer(stable_value=3000.0, ts=t0)
    analyzer._snap.altitude.rate = 60.0
    kb = _FakeKeyboard()
    cfg = dict(CFG, max_climb_s=1.4, pitch_pulse_s=0.2, pulse_observe_s=0.2,
               min_climb_rate=30.0, max_climb_rate=100.0)
    ctrl = _make_ctrl(monkeypatch, kb, analyzer, cfg)

    ctrl.climb_mode(target_alt=7000, max_s=1.4)
    time.sleep(0.3)
    analyzer.set(3100.0, t0 + 0.1)
    analyzer._snap.altitude.rate = 60.0
    time.sleep(0.4)
    up_after_rate = len(_presses(kb, NOSE_UP_KEY))
    time.sleep(0.4)
    assert len(_presses(kb, NOSE_UP_KEY)) == up_after_rate, \
        "nose-up pulse fired despite in-band climb rate"
    assert not _presses(kb, NOSE_DOWN_KEY), \
        "nose-down pulse fired despite rate below the ceiling"
    assert _wait_done(ctrl)


def test_unset_ceiling_disables_nose_down(monkeypatch):
    """No max_climb_rate in config: pre-ADR 076 behavior — never nose-down."""
    from wingman.controller import NOSE_DOWN_KEY

    t0 = time.time()
    analyzer = _FakeTelemetryAnalyzer(stable_value=3000.0, ts=t0)
    kb = _FakeKeyboard()
    cfg = dict(CFG, max_climb_s=1.0, pitch_pulse_s=0.2, pulse_observe_s=0.2,
               min_climb_rate=30.0)   # no max_climb_rate key
    ctrl = _make_ctrl(monkeypatch, kb, analyzer, cfg)

    ctrl.climb_mode(target_alt=7000, max_s=1.0)
    time.sleep(0.3)
    analyzer.set(3050.0, t0 + 0.1)
    analyzer._snap.altitude.rate = 5000.0
    time.sleep(0.5)
    assert not _presses(kb, NOSE_DOWN_KEY), \
        "nose-down fired with the ceiling unset"
    assert _wait_done(ctrl)


# ---------------------------------------------------------------------------
# ADR 081 d1: pitch ceiling outranks the rate floor
# ---------------------------------------------------------------------------

def test_over_angle_pulses_nose_down_despite_low_rate(monkeypatch):
    """The inversion case: near vertical the rate decays below the floor,
    and rate logic alone would pulse MORE nose-up — the ceiling must win."""
    from wingman.controller import NOSE_DOWN_KEY

    t0 = time.time()
    analyzer = _FakeTelemetryAnalyzer(stable_value=3000.0, ts=t0)
    kb = _FakeKeyboard()
    cfg = dict(CFG, max_climb_s=2.0, pitch_pulse_s=0.2, pulse_observe_s=0.2,
               min_climb_rate=30.0, max_pitch_deg=80.0)
    ctrl = _make_ctrl(monkeypatch, kb, analyzer, cfg)

    ctrl.climb_mode(target_alt=9000, max_s=2.0)
    for i in range(5):
        time.sleep(0.25)
        analyzer.set(3000.0 + i, t0 + 0.1 * (i + 1), angle=85.0)
        analyzer._snap.altitude.rate = 5.0   # decayed rate — below the floor
    ctrl._climb_stop.set()
    assert _wait_done(ctrl)
    assert _presses(kb, NOSE_DOWN_KEY), \
        "no nose-down pulse at 85° — rate floor won over the pitch ceiling"


def test_below_ceiling_keeps_rate_logic(monkeypatch):
    from wingman.controller import NOSE_DOWN_KEY

    t0 = time.time()
    analyzer = _FakeTelemetryAnalyzer(stable_value=3000.0, ts=t0)
    kb = _FakeKeyboard()
    cfg = dict(CFG, max_climb_s=1.4, pitch_pulse_s=0.2, pulse_observe_s=0.2,
               min_climb_rate=30.0, max_pitch_deg=80.0)
    ctrl = _make_ctrl(monkeypatch, kb, analyzer, cfg)

    ctrl.climb_mode(target_alt=9000, max_s=1.4)
    time.sleep(0.3)
    analyzer.set(3050.0, t0 + 0.1, angle=60.0)
    analyzer._snap.altitude.rate = 5.0       # below floor, angle in range
    time.sleep(0.6)
    assert not _presses(kb, NOSE_DOWN_KEY), \
        "nose-down fired below the pitch ceiling"
    assert len(_presses(kb, NOSE_UP_KEY)) >= 1
    assert _wait_done(ctrl)


def test_unset_ceiling_reproduces_legacy_behavior(monkeypatch):
    from wingman.controller import NOSE_DOWN_KEY

    t0 = time.time()
    analyzer = _FakeTelemetryAnalyzer(stable_value=3000.0, ts=t0)
    kb = _FakeKeyboard()
    cfg = dict(CFG, max_climb_s=1.0, pitch_pulse_s=0.2, pulse_observe_s=0.2,
               min_climb_rate=30.0)   # no max_pitch_deg key
    ctrl = _make_ctrl(monkeypatch, kb, analyzer, cfg)

    ctrl.climb_mode(target_alt=9000, max_s=1.0)
    time.sleep(0.3)
    analyzer.set(3050.0, t0 + 0.1, angle=89.0)
    analyzer._snap.altitude.rate = 5.0
    time.sleep(0.4)
    assert not _presses(kb, NOSE_DOWN_KEY), \
        "nose-down fired with the ceiling unset"
    assert _wait_done(ctrl)
