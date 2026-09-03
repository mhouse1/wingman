"""Unit tests for climb_mode (ADR 073 Phase 3.2b).

Controller-side: the altitude-recovery exit on fresh advancing samples (a
stalled telemetry signal must NOT end the climb), the max_climb_s backstop,
duplicate-start suppression, eject/evade suppression and pre-emption, and the
programmatic-key bracket on NOSE_UP. No real keyboard, no OCR.
"""

import threading
import time

import wingman.controller as controller_module
from wingman.controller_config import ControllerConfig
from wingman.controller import (
    AFTERBURNER_KEY,
    Controller,
    NOSE_UP_KEY,
    ROLL_RIGHT_KEY,
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
        config=ControllerConfig(
            disable_hotkeys=True,
            climb=climb_cfg,
        ),
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


def test_climb_burner_does_not_relight_while_over_pitch_ceiling(monkeypatch):
    """ADR 086 d6: fuel recovery must not relight the burner while the aircraft
    is at or above max_pitch_deg, even below the target altitude.

    ADR 083 d3 cut thrust above the target because "the pitch ceiling fighting
    a lit burner" strands high-angle stretches, but gated only on altitude.
    Below target and over the ceiling is the same trap: on 2026-08-21 a relight
    at +64deg carried the nose to +90deg and collapsed speed 1241->392, where
    the elevator has no authority and neither the ceiling nor the ADR 086 d1
    exit push could recover it.
    """
    t0 = time.time()
    analyzer = _FakeTelemetryAnalyzer(stable_value=3000.0, ts=t0, fuel=50)
    kb = _FakeKeyboard()
    cfg = dict(CFG, max_climb_s=6.0, max_pitch_deg=80.0)
    ctrl = _make_ctrl(monkeypatch, kb, analyzer, cfg)

    ctrl.climb_mode(target_alt=9000.0, max_s=6.0, fuel_floor_pct=10.0)
    time.sleep(0.4)
    assert _presses(kb, AFTERBURNER_KEY), "burner not engaged with fuel above floor"

    analyzer.set_fuel(10)                       # at the floor -> burner released
    time.sleep(0.6)
    assert _releases(kb, AFTERBURNER_KEY), "burner not released at the reserve floor"
    lit_at_ceiling = len(_presses(kb, AFTERBURNER_KEY))

    # Over-angled and still well below the target altitude.
    analyzer.set(4000.0, t0 + 1.0, angle=85.0)
    time.sleep(0.4)
    analyzer.set_fuel(40)                       # recovery would normally relight
    time.sleep(0.8)
    assert len(_presses(kb, AFTERBURNER_KEY)) == lit_at_ceiling, \
        "burner relit while over the pitch ceiling"

    # Back inside the ceiling with the same fuel: the gate is angle-specific,
    # not a blanket block, so the burner must come back.
    analyzer.set(5000.0, t0 + 2.0, angle=40.0)
    time.sleep(0.8)
    assert len(_presses(kb, AFTERBURNER_KEY)) > lit_at_ceiling, \
        "burner never relit after the nose came back inside the ceiling"

    ctrl._climb_stop.set()
    assert _wait_done(ctrl)


def test_climb_burner_blocked_by_predicted_pitch_ceiling(monkeypatch):
    """ADR 086 d7: the ceiling tests the PREDICTED angle, not the current one.

    d6 gated the relight on the current angle and was too permissive to help:
    telemetry lands every ~3s while a lit burner rotates at ~11deg/s, so
    relights at +48deg and +57deg — both legally under the 80deg ceiling —
    had the nose at +90deg before the next read (2026-08-21 09:40). Rising at
    that rate, +57deg is already committed to the ceiling and must not relight.
    """
    t0 = time.time()
    analyzer = _FakeTelemetryAnalyzer(stable_value=1000.0, ts=t0, fuel=50)
    kb = _FakeKeyboard()
    cfg = dict(CFG, max_climb_s=8.0, max_pitch_deg=80.0, pitch_lead_s=3.0)
    ctrl = _make_ctrl(monkeypatch, kb, analyzer, cfg)

    ctrl.climb_mode(target_alt=9000.0, max_s=8.0, fuel_floor_pct=10.0)
    time.sleep(0.4)
    analyzer.set_fuel(10)                        # drop to floor -> burner off
    time.sleep(0.6)
    assert _releases(kb, AFTERBURNER_KEY), "burner not released at the floor"
    lit_before = len(_presses(kb, AFTERBURNER_KEY))

    # Two samples 3s apart rising 46 -> 57 deg == ~3.7 deg/s. Predicted at the
    # next sample: 57 + 3.7*3 == ~68 deg, still under the ceiling -> may relight.
    analyzer.set(2000.0, t0 + 1.0, angle=46.0)
    time.sleep(0.4)
    analyzer.set(3000.0, t0 + 4.0, angle=57.0)
    time.sleep(0.4)
    analyzer.set_fuel(40)
    time.sleep(0.6)
    assert len(_presses(kb, AFTERBURNER_KEY)) > lit_before, \
        "a gently rising nose well under the ceiling must still relight"

    analyzer.set_fuel(10)                        # off again for the steep case
    time.sleep(0.6)
    lit_before = len(_presses(kb, AFTERBURNER_KEY))

    # Now rising 24 -> 57 in 3s == 11 deg/s, the observed burner rate. The
    # current angle (57) is legal, but 57 + 11*3 == 90 blows through the
    # ceiling before the next read, so this must NOT relight.
    analyzer.set(4000.0, t0 + 7.0, angle=24.0)
    time.sleep(0.4)
    analyzer.set(5000.0, t0 + 10.0, angle=57.0)
    time.sleep(0.4)
    analyzer.set_fuel(40)
    time.sleep(0.6)
    assert len(_presses(kb, AFTERBURNER_KEY)) == lit_before, \
        "burner relit at a pitch rate that reaches the ceiling before the next read"

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


# ---------------------------------------------------------------------------
# ADR 083: lead-the-target exit and burner cut above target
# ---------------------------------------------------------------------------

def test_lead_exits_a_sample_early(monkeypatch):
    """A fast climb exits on the PREDICTED altitude: 4000 m climbing at
    450 m/s is 5350 m one 3 s sample later, so the 5000 m target is met."""
    t0 = time.time()
    analyzer = _FakeTelemetryAnalyzer(stable_value=1000.0, ts=t0)
    kb = _FakeKeyboard()
    cfg = dict(CFG, max_climb_s=3.0, confirm_reads=1)
    ctrl = _make_ctrl(monkeypatch, kb, analyzer, cfg)

    ctrl.climb_mode(target_alt=5000, max_s=3.0, exit_lead_s=3.0)
    time.sleep(0.3)
    analyzer.set(4000.0, t0 + 0.1)          # below target...
    analyzer._snap.altitude.rate = 450.0    # ...but 5350 predicted
    assert _wait_done(ctrl, timeout=3.0), "lead-the-target exit did not fire"


def test_unknown_rate_falls_back_to_raw_altitude(monkeypatch):
    """Freeze policy: no rate means no prediction — the raw value decides,
    so a below-target read must NOT end the climb."""
    t0 = time.time()
    analyzer = _FakeTelemetryAnalyzer(stable_value=1000.0, ts=t0)
    kb = _FakeKeyboard()
    cfg = dict(CFG, max_climb_s=1.2, confirm_reads=1)
    ctrl = _make_ctrl(monkeypatch, kb, analyzer, cfg)

    t_start = time.time()
    ctrl.climb_mode(target_alt=5000, max_s=1.2, exit_lead_s=3.0)
    time.sleep(0.3)
    analyzer.set(4000.0, t0 + 0.1)          # rate stays None
    assert _wait_done(ctrl)
    assert time.time() - t_start >= 1.0, "climb ended early on an unknown rate"


def test_zero_lead_reproduces_legacy_exit(monkeypatch):
    """Emergency climbs pass lead 0 — behaviour must be byte-identical to
    the pre-ADR-083 exit (terrain outranks efficiency)."""
    t0 = time.time()
    analyzer = _FakeTelemetryAnalyzer(stable_value=1000.0, ts=t0)
    kb = _FakeKeyboard()
    cfg = dict(CFG, max_climb_s=1.2, confirm_reads=1)
    ctrl = _make_ctrl(monkeypatch, kb, analyzer, cfg)

    t_start = time.time()
    ctrl.climb_mode(target_alt=5000, max_s=1.2, exit_lead_s=0.0)
    time.sleep(0.3)
    analyzer.set(4000.0, t0 + 0.1)
    analyzer._snap.altitude.rate = 450.0     # would predict 5350 — must be ignored
    assert _wait_done(ctrl)
    assert time.time() - t_start >= 1.0, "zero lead still exited on prediction"


def test_confirm_reads_still_debounces_the_prediction(monkeypatch):
    """One predicted-over-target read must not end a confirm_reads=2 climb."""
    t0 = time.time()
    analyzer = _FakeTelemetryAnalyzer(stable_value=1000.0, ts=t0)
    kb = _FakeKeyboard()
    cfg = dict(CFG, max_climb_s=1.4, confirm_reads=2)
    ctrl = _make_ctrl(monkeypatch, kb, analyzer, cfg)

    ctrl.climb_mode(target_alt=5000, max_s=1.4, exit_lead_s=3.0)
    time.sleep(0.3)
    analyzer.set(4000.0, t0 + 0.1)
    analyzer._snap.altitude.rate = 450.0     # streak 1 only
    time.sleep(0.3)
    assert ctrl.is_climbing(), "single predicted read ended the climb"
    ctrl._climb_stop.set()
    assert _wait_done(ctrl)


def test_burner_cut_at_target_and_never_relit(monkeypatch):
    """ADR 083 d3: thrust stops on the first at-target read, and the ADR 075
    fuel rearm must not relight it above target."""
    t0 = time.time()
    analyzer = _FakeTelemetryAnalyzer(stable_value=1000.0, ts=t0, fuel=100)
    kb = _FakeKeyboard()
    cfg = dict(CFG, max_climb_s=2.0, confirm_reads=5)   # keep the hold alive
    ctrl = _make_ctrl(monkeypatch, kb, analyzer, cfg)

    ctrl.climb_mode(target_alt=5000, max_s=2.0, fuel_floor_pct=10.0)
    time.sleep(0.3)
    assert _presses(kb, AFTERBURNER_KEY), "burner never lit"
    analyzer.set(5100.0, t0 + 0.1)          # at target
    time.sleep(0.4)
    assert _releases(kb, AFTERBURNER_KEY), "burner not cut at target"
    lit_before = len(_presses(kb, AFTERBURNER_KEY))
    analyzer.set(5200.0, t0 + 0.2)          # still above, fuel still 100%
    time.sleep(0.4)
    assert len(_presses(kb, AFTERBURNER_KEY)) == lit_before, \
        "burner relit above target"
    ctrl._climb_stop.set()
    assert _wait_done(ctrl)


# ---------------------------------------------------------------------------
# ADR 086 d1 / SAF-010 — exit attitude
# ---------------------------------------------------------------------------

from wingman.controller import NOSE_DOWN_KEY  # noqa: E402

EXIT_CFG = dict(CFG, exit_pitch_deg=20.0, exit_push_pulse_s=0.05,
                exit_push_max_pulses=3)


def test_exit_pushes_nose_down_when_leaving_climb_nose_high(monkeypatch):
    """Regression, live 2026-08-21 06:01: the climb released NOSE_UP,
    NOSE_DOWN and AFTERBURNER together at +73 deg, leaving the aircraft
    ballistic. It coasted 1500 m further, stalled at 24 KPH and hit the
    ground with two missiles still racked."""
    kb = _FakeKeyboard()
    analyzer = _FakeTelemetryAnalyzer(fuel=90)
    ctrl = _make_ctrl(monkeypatch, kb, analyzer, EXIT_CFG)
    analyzer.set(1200.0, time.time(), fresh=True, angle=73.0)   # nose high

    reason = ctrl._climb_exit_push()

    assert _presses(kb, NOSE_DOWN_KEY), "climb exited without lowering the nose"
    assert _releases(kb, NOSE_DOWN_KEY), "nose-down never released"
    assert reason == "budget_exhausted"   # angle never improves in this stub


def test_exit_push_stops_once_inside_the_band(monkeypatch):
    """The push must stop on the first in-band sample, not spend its budget."""
    kb = _FakeKeyboard()
    analyzer = _FakeTelemetryAnalyzer(fuel=90)
    ctrl = _make_ctrl(monkeypatch, kb, analyzer, EXIT_CFG)
    analyzer.set(1200.0, time.time(), fresh=True, angle=12.0)   # already flyable

    reason = ctrl._climb_exit_push()

    assert reason == "in_band"
    assert not _presses(kb, NOSE_DOWN_KEY), "pushed despite already being in band"


def test_exit_push_is_single_pulse_when_blind(monkeypatch):
    """No telemetry: one bounded pulse, then hand back. An unverified small
    nose-down beats an unverified ballistic climb."""
    kb = _FakeKeyboard()
    analyzer = _FakeTelemetryAnalyzer(fuel=90)
    ctrl = _make_ctrl(monkeypatch, kb, analyzer, EXIT_CFG)
    analyzer.set(None, None, fresh=False, angle=None)

    reason = ctrl._climb_exit_push()

    assert reason == "blind_single_pulse"
    assert len(_presses(kb, NOSE_DOWN_KEY)) == 1
    assert len(_releases(kb, NOSE_DOWN_KEY)) == 1


def test_exit_push_budget_is_bounded(monkeypatch):
    """A nose that never comes down must not pin NOSE_DOWN indefinitely —
    the ADR 069 nose-hold-budget failure class."""
    kb = _FakeKeyboard()
    analyzer = _FakeTelemetryAnalyzer(fuel=90)
    ctrl = _make_ctrl(monkeypatch, kb, analyzer, EXIT_CFG)
    analyzer.set(1200.0, time.time(), fresh=True, angle=88.0)

    reason = ctrl._climb_exit_push()

    assert reason == "budget_exhausted"
    assert len(_presses(kb, NOSE_DOWN_KEY)) == 3
    assert len(_releases(kb, NOSE_DOWN_KEY)) == 3, "a pulse was left held"


def test_exit_push_disabled_when_unconfigured(monkeypatch):
    """exit_pitch_deg unset keeps the pre-ADR-086 behaviour, so the change is
    opt-out without touching code."""
    kb = _FakeKeyboard()
    analyzer = _FakeTelemetryAnalyzer(fuel=90)
    ctrl = _make_ctrl(monkeypatch, kb, analyzer, CFG)   # no exit_pitch_deg
    analyzer.set(1200.0, time.time(), fresh=True, angle=73.0)

    assert ctrl._climb_exit_push() == "disabled"
    assert not _presses(kb, NOSE_DOWN_KEY)


def test_exit_push_yields_to_stop_event(monkeypatch):
    """Manual takeover / cleanup must cut the exit push (SAF-001, SAF-008)."""
    kb = _FakeKeyboard()
    analyzer = _FakeTelemetryAnalyzer(fuel=90)
    ctrl = _make_ctrl(monkeypatch, kb, analyzer, EXIT_CFG)
    analyzer.set(1200.0, time.time(), fresh=True, angle=73.0)
    ctrl._climb_stop.set()

    reason = ctrl._climb_exit_push()

    assert reason == "interrupted"
    assert len(_presses(kb, NOSE_DOWN_KEY)) <= 1
    assert len(_releases(kb, NOSE_DOWN_KEY)) == len(_presses(kb, NOSE_DOWN_KEY))


# ---------------------------------------------------------------------------
# ADR 088: an inbound missile outranks every afterburner reserve policy
# ---------------------------------------------------------------------------

class _IncomingAnalyzer(_FakeTelemetryAnalyzer):
    """Telemetry stand-in that can also report an incoming-missile alert."""

    def __init__(self, *a, incoming=False, **kw):
        super().__init__(*a, **kw)
        self._incoming = incoming

    def set_incoming(self, value):
        with self._lock:
            self._incoming = value

    def get_incoming_cache_result(self):
        with self._lock:
            return (self._incoming, 1.0, "test")


def test_incoming_overrides_the_fuel_reserve_floor(monkeypatch):
    """ADR 088: outrunning a missile beats preserving the evade reserve.

    Observed 2026-08-22 01:47:39 — the evade released at its manoeuvre limit
    with incoming still present, Climb took over, and the burner was cut 1.7s
    later. The aircraft coasted while a missile was inbound.
    """
    analyzer = _IncomingAnalyzer(stable_value=None, ts=None, fresh=False, fuel=50)
    kb = _FakeKeyboard()
    ctrl = _make_ctrl(monkeypatch, kb, analyzer, dict(CFG, max_climb_s=8.0))

    ctrl.climb_mode(target_alt=9000.0, max_s=8.0, fuel_floor_pct=10.0)
    time.sleep(0.4)
    analyzer.set_fuel(8)                    # below the floor -> normally released
    time.sleep(0.6)
    assert _releases(kb, AFTERBURNER_KEY), "burner not released at the floor"
    lit = len(_presses(kb, AFTERBURNER_KEY))

    analyzer.set_incoming(True)             # missile inbound, fuel still 8%
    time.sleep(0.8)
    assert len(_presses(kb, AFTERBURNER_KEY)) > lit, \
        "burner not forced on for an inbound missile below the reserve floor"

    ctrl._climb_stop.set()
    assert _wait_done(ctrl)


def test_incoming_does_not_force_burner_on_an_empty_tank(monkeypatch):
    """ADR 075 still stands at 0%: no thrust, and a held key blocks recharge."""
    analyzer = _IncomingAnalyzer(stable_value=None, ts=None, fresh=False,
                                 fuel=0, incoming=True)
    kb = _FakeKeyboard()
    ctrl = _make_ctrl(monkeypatch, kb, analyzer, dict(CFG, max_climb_s=6.0))

    ctrl.climb_mode(target_alt=9000.0, max_s=6.0, fuel_floor_pct=10.0)
    time.sleep(0.8)
    assert not _presses(kb, AFTERBURNER_KEY), \
        "burner pressed with an empty tank despite ADR 075"

    ctrl._climb_stop.set()
    assert _wait_done(ctrl)


# ---------------------------------------------------------------------------
# ADR 107: BoundaryTurn owns roll AND pitch
# ---------------------------------------------------------------------------

def _wait_for(pred, timeout=2.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return True
        time.sleep(0.02)
    return False


def test_the_boundary_turn_banks_and_pulls(monkeypatch):
    """ADR 101 held roll alone and it was measured inert — 8 s of rolling on
    2026-09-03 left the aircraft at its closest approach, because Climb owned
    pitch and a bank without a pull does not turn the flight path. Owning both
    axes is the whole reason this became a tactic."""
    analyzer = _FakeTelemetryAnalyzer(stable_value=None, ts=None, fresh=False)
    kb = _FakeKeyboard()
    ctrl = _make_ctrl(monkeypatch, kb, analyzer, CFG)

    ctrl.boundary_turn_mode(max_s=10.0)
    assert _wait_for(lambda: _presses(kb, ROLL_RIGHT_KEY)), "never banked"
    assert _presses(kb, NOSE_UP_KEY), "banked without pulling — the ADR 101 defect"
    assert ctrl.is_boundary_turning()
    ctrl._boundary_turn_stop.set()
    assert _wait_for(lambda: not ctrl.is_boundary_turning()), "turn never ended"


def test_the_turn_hands_the_airframe_back_flyable(monkeypatch):
    """SAF-010. This tactic holds NOSE_UP, so it owes the same exit push the
    climb does: ADR 086 exists because a climb released at +73 degrees coasted
    1500 m, stalled at 24 KPH and hit the ground."""
    analyzer = _FakeTelemetryAnalyzer(stable_value=None, ts=None, fresh=False)
    kb = _FakeKeyboard()
    # exit_pitch_deg is what ARMS the push; unset it is the documented
    # pre-ADR-086 behaviour, so the test must configure it to assert on it.
    ctrl = _make_ctrl(monkeypatch, kb, analyzer, dict(CFG, exit_pitch_deg=10))

    ctrl.boundary_turn_mode(max_s=0.3)
    assert _wait_for(lambda: not ctrl.is_boundary_turning(), timeout=4.0)
    assert _releases(kb, ROLL_RIGHT_KEY), "roll left held"
    assert _releases(kb, NOSE_UP_KEY), "nose-up left held"
    assert _presses(kb, NOSE_DOWN_KEY), "no SAF-010 exit push"


def test_the_turn_is_idempotent_while_running(monkeypatch):
    """The ADR 070 d8 pattern: a second selection must not start a second
    thread onto the same two flight axes."""
    analyzer = _FakeTelemetryAnalyzer(stable_value=None, ts=None, fresh=False)
    kb = _FakeKeyboard()
    ctrl = _make_ctrl(monkeypatch, kb, analyzer, CFG)

    ctrl.boundary_turn_mode(max_s=10.0)
    assert _wait_for(lambda: _presses(kb, ROLL_RIGHT_KEY))
    before = len(_presses(kb, ROLL_RIGHT_KEY))
    ctrl.boundary_turn_mode(max_s=10.0)
    time.sleep(0.3)
    assert len(_presses(kb, ROLL_RIGHT_KEY)) == before
    ctrl._boundary_turn_stop.set()
    _wait_for(lambda: not ctrl.is_boundary_turning())


def test_manual_takeover_stops_the_turn(monkeypatch):
    """SAF-001. It holds two flight axes, so it must let go the instant the
    operator asks for the aircraft."""
    analyzer = _FakeTelemetryAnalyzer(stable_value=None, ts=None, fresh=False)
    kb = _FakeKeyboard()
    ctrl = _make_ctrl(monkeypatch, kb, analyzer, CFG)

    ctrl.boundary_turn_mode(max_s=30.0)
    assert _wait_for(lambda: ctrl.is_boundary_turning())
    ctrl.release_for_manual_takeover()
    assert _wait_for(lambda: not ctrl.is_boundary_turning(), timeout=4.0)
    assert _releases(kb, ROLL_RIGHT_KEY) and _releases(kb, NOSE_UP_KEY)
