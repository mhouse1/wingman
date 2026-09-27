"""ADR 148: a dive recovery must be able to fly a pursuit.

Measured 2026-09-25 00:03 (the operator's session, two lives): both chases ended by flying
into the ground. The tree's emergency selected Climb at 29 s to ground and again every
1.5 s to the impact, and every hold ended `climb complete (state_exit, 0.3s)`: the hold
releases on any game state other than GAME_BATTLE (the SAF-001 backstop) and a pursuit
lives in GAME_BATTLE_EJECT. The recovery was a 0.3 s nudge in every 1.5 s, and the chase
kept writing pitch and roll on top of it.

What is pinned here, with a REAL Controller, a real hold thread and the real GameState:
- a hard-emergency hold keeps flying while a pursuit runs in GAME_BATTLE_EJECT,
- every other release path still works (no pursuit, a soft hold, any other state, the
  operator's takeover, the cap, `recovery_max_s: 0`),
- the chase writes neither axis while the recovery flies, and steers again after it.
"""

import logging
import threading
import time

import wingman.controller as controller_module
from wingman.analyzer import GameState
from wingman.controller import (
    Controller, FIRE_ACTIVE_WEAPON, NOSE_DOWN_KEY, NOSE_UP_KEY, ROLL_LEFT_KEY, ROLL_RIGHT_KEY,
)
from wingman.controller_config import ControllerConfig
from wingman.telemetry import TelemetrySignal

_STEER_KEYS = {NOSE_UP_KEY, NOSE_DOWN_KEY, ROLL_LEFT_KEY, ROLL_RIGHT_KEY}


def _Alt(value):
    """The real TelemetrySignal, level flight: the dive guard reads `rate` too."""
    return TelemetrySignal(value=int(value), ts=time.time(), stable_value=value, rate=0.0)


class _Snapshot:
    """Real shape of what the hold reads: a fresh altitude every call, no angle."""

    def __init__(self, altitude):
        self.altitude = _Alt(altitude)

    def altitude_fresh(self):
        return True

    def pitch_angle_deg(self):
        return None


class _Analyzer:
    def __init__(self, state=GameState.GAME_BATTLE_EJECT, altitude=3200.0):
        self.game_state = state
        self._altitude = altitude
        self.ammo = 2

    def get_telemetry(self):
        return _Snapshot(self._altitude)

    def get_afterburner_fuel_pct(self):
        return 80

    def get_ammo_missiles(self):
        return self.ammo

    def get_ammo_flares(self):
        return 4

    def get_health(self):
        return 100

    def mark_health_dead_synthetic(self):
        pass


class _Capture:
    def grab_from_thread(self):
        return object()


class _Tracker:
    """A target right of centre and above it: the chase wants roll right and nose up."""

    def update(self, frame):
        return {"visible": True, "error_norm": 0.5, "error_norm_y": -0.3, "mode": "TRACKING"}


_CLIMB = {"enabled": True, "enter_below_alt": 500, "exit_above_alt": 1000,
          "confirm_reads": 2, "max_climb_s": 5.0}


def _ctrl(monkeypatch, analyzer, recovery_max_s=30.0):
    monkeypatch.setattr(controller_module, "keyboard_module", None)
    return Controller(
        (0, 0, 1920, 1200),
        analyzer=analyzer,
        exit_event=threading.Event(),
        capture=_Capture(),
        config=ControllerConfig(
            simulate_os_input=True,
            disable_hotkeys=True,
            climb=_CLIMB,
            tracking={"sustained_hold_enabled": True},
            pursuit_mode={"enabled": True, "pursuit_max_duration_s": 0.0,
                          "pursuit_padlock_verify": False, "ammo_zero_grace_s": 0.0,
                          "search_resume_delay_s": 0.0, "search_resume_centre_delay_s": 0.0,
                          "empty_confirm_reads": 3, "recovery_max_s": recovery_max_s},
        ),
    )


def _keys(ctrl):
    with ctrl._action_intents_lock:
        return [(i["action_type"], i["key"]) for i in ctrl._action_intents]


def _wait(pred, timeout=4.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return True
        time.sleep(0.02)
    return False


def _stop(ctrl):
    ctrl._climb_stop.set()
    ctrl._pursuing.clear()
    _wait(lambda: not ctrl.is_climbing())


# --- the hold -------------------------------------------------------------------

def test_a_hard_emergency_hold_keeps_flying_through_a_pursuit(monkeypatch):
    ctrl = _ctrl(monkeypatch, _Analyzer())
    ctrl._pursuing.set()
    ctrl.climb_mode(target_alt=5000, max_s=5.0, emergency=True)
    try:
        assert _wait(ctrl.pursuit_recovery_active), "the hold never entered recovery mode"
        time.sleep(0.8)   # three polls past the 0.25 s at which it used to release
        assert ctrl.is_climbing(), "released after 0.25 s in GAME_BATTLE_EJECT, as it did before"
    finally:
        _stop(ctrl)


def test_the_same_hold_releases_when_no_pursuit_is_running(monkeypatch, caplog):
    """eject_and_dive is a deliberate dive to die: it has no pursuit, so the backstop stands."""
    ctrl = _ctrl(monkeypatch, _Analyzer())
    with caplog.at_level(logging.INFO, logger="wingman.controller"):
        ctrl.climb_mode(target_alt=5000, max_s=5.0, emergency=True)
        assert _wait(lambda: not ctrl.is_climbing(), timeout=2.0)
    assert any("state_exit" in r.getMessage() for r in caplog.records)
    assert not ctrl.pursuit_recovery_active()


def test_a_soft_hold_still_releases_inside_a_pursuit(monkeypatch, caplog):
    """The altitude floor's climb is not a hard emergency: in a chase it stays a nudge."""
    ctrl = _ctrl(monkeypatch, _Analyzer())
    ctrl._pursuing.set()
    with caplog.at_level(logging.INFO, logger="wingman.controller"):
        ctrl.climb_mode(target_alt=5000, max_s=5.0, emergency=False)
        assert _wait(lambda: not ctrl.is_climbing(), timeout=2.0)
    assert any("state_exit" in r.getMessage() for r in caplog.records)
    assert not ctrl.pursuit_recovery_active()


def test_any_other_state_still_releases_the_hold(monkeypatch):
    ctrl = _ctrl(monkeypatch, _Analyzer(state=GameState.GAME_LOBBY))
    ctrl._pursuing.set()
    ctrl.climb_mode(target_alt=5000, max_s=5.0, emergency=True)
    assert _wait(lambda: not ctrl.is_climbing(), timeout=2.0)


def test_the_operators_takeover_still_ends_the_recovery(monkeypatch):
    """SAF-001: takeover moves the state to GAME_BATTLE_MANUAL, which is not exempt."""
    analyzer = _Analyzer()
    ctrl = _ctrl(monkeypatch, analyzer)
    ctrl._pursuing.set()
    ctrl.climb_mode(target_alt=5000, max_s=5.0, emergency=True)
    try:
        assert _wait(ctrl.pursuit_recovery_active)
        analyzer.game_state = GameState.GAME_BATTLE_MANUAL
        assert _wait(lambda: not ctrl.is_climbing(), timeout=2.0), "the takeover did not end the hold"
    finally:
        _stop(ctrl)


def test_the_pursuit_ending_ends_the_recovery(monkeypatch):
    ctrl = _ctrl(monkeypatch, _Analyzer())
    ctrl._pursuing.set()
    ctrl.climb_mode(target_alt=5000, max_s=5.0, emergency=True)
    try:
        assert _wait(ctrl.pursuit_recovery_active)
        ctrl._pursuing.clear()          # respawn, shutdown or an external stop
        assert _wait(lambda: not ctrl.is_climbing(), timeout=2.0)
    finally:
        _stop(ctrl)


def test_the_recovery_is_capped(monkeypatch, caplog):
    ctrl = _ctrl(monkeypatch, _Analyzer(), recovery_max_s=0.6)
    ctrl._pursuing.set()
    with caplog.at_level(logging.INFO, logger="wingman.controller"):
        ctrl.climb_mode(target_alt=5000, max_s=5.0, emergency=True)
        assert _wait(lambda: not ctrl.is_climbing(), timeout=3.0), "the cap never ended the hold"
    assert any("recovery_cap" in r.getMessage() for r in caplog.records)
    _stop(ctrl)


def test_recovery_max_s_zero_restores_the_old_release(monkeypatch):
    ctrl = _ctrl(monkeypatch, _Analyzer(), recovery_max_s=0.0)
    ctrl._pursuing.set()
    ctrl.climb_mode(target_alt=5000, max_s=5.0, emergency=True)
    assert _wait(lambda: not ctrl.is_climbing(), timeout=2.0)
    assert not ctrl.pursuit_recovery_active()


def test_an_emergency_that_clears_mid_recovery_keeps_the_hold_flying(monkeypatch):
    """Releasing the instant the descent stops would hand a low aircraft straight back to a
    chase that dives, so recovery mode latches until the hold finishes or the cap."""
    ctrl = _ctrl(monkeypatch, _Analyzer())
    ctrl._pursuing.set()
    ctrl.climb_mode(target_alt=5000, max_s=5.0, emergency=True)
    try:
        assert _wait(ctrl.pursuit_recovery_active)
        ctrl.set_climb_emergency(False)
        time.sleep(0.8)
        assert ctrl.is_climbing() and ctrl.pursuit_recovery_active()
    finally:
        _stop(ctrl)


def test_a_soft_hold_that_escalates_in_the_pursuit_state_recovers(monkeypatch):
    ctrl = _ctrl(monkeypatch, _Analyzer())
    ctrl._pursuing.set()
    ctrl.climb_mode(target_alt=5000, max_s=5.0, emergency=False)
    ctrl.set_climb_emergency(True)      # before the hold's first poll
    try:
        assert _wait(ctrl.pursuit_recovery_active, timeout=2.0)
    finally:
        _stop(ctrl)


def test_the_flag_clears_when_the_hold_ends(monkeypatch):
    ctrl = _ctrl(monkeypatch, _Analyzer())
    ctrl._pursuing.set()
    ctrl.climb_mode(target_alt=5000, max_s=5.0, emergency=True)
    assert _wait(ctrl.pursuit_recovery_active)
    _stop(ctrl)
    assert not ctrl._pursuit_recovery.is_set()


def test_the_predicate_needs_the_pursuit_too(monkeypatch):
    ctrl = _ctrl(monkeypatch, _Analyzer())
    ctrl._pursuit_recovery.set()
    assert ctrl.pursuit_recovery_active() is False
    ctrl._pursuing.set()
    assert ctrl.pursuit_recovery_active() is True
    ctrl._pursuit_recovery.clear()
    ctrl._pursuing.clear()


# --- the chase ------------------------------------------------------------------

def _start_chase(monkeypatch):
    ctrl = _ctrl(monkeypatch, _Analyzer())
    ctrl.set_target_tracker(_Tracker())
    ctrl.pursue_and_engage(defer_switch_until_empty=True)
    assert _wait(ctrl.is_pursuing)
    return ctrl


def _end_chase(ctrl):
    ctrl._pursuit_recovery.clear()
    ctrl.stop_eject_sequence("respawn_detected")
    _wait(lambda: not ctrl.is_pursuing())


def test_the_chase_steers_when_no_recovery_is_running(monkeypatch):
    ctrl = _start_chase(monkeypatch)
    try:
        time.sleep(0.7)
        keys = _keys(ctrl)
        assert ("key_press", ROLL_RIGHT_KEY) in keys and ("key_press", NOSE_UP_KEY) in keys
    finally:
        _end_chase(ctrl)


def test_the_chase_writes_neither_axis_while_the_recovery_flies(monkeypatch):
    ctrl = _start_chase(monkeypatch)
    try:
        time.sleep(0.5)                                  # steering is running
        ctrl._pursuit_recovery.set()                     # a dive recovery takes the airframe
        time.sleep(0.4)                                  # let the loop notice and release
        mark = len(_keys(ctrl))
        time.sleep(0.9)                                  # at least two more loop cycles
        during = _keys(ctrl)[mark:]
        assert not [k for k in during if k[1] in _STEER_KEYS and k[0] == "key_press"], during
        assert ("key_press", FIRE_ACTIVE_WEAPON) in during, "firing carries on"
        assert ctrl._roll_held is None and ctrl._pitch_held is None
    finally:
        _end_chase(ctrl)


def test_the_chase_steers_again_when_the_recovery_ends(monkeypatch):
    ctrl = _start_chase(monkeypatch)
    try:
        ctrl._pursuit_recovery.set()
        time.sleep(0.9)
        ctrl._pursuit_recovery.clear()
        mark = len(_keys(ctrl))
        time.sleep(0.9)
        after = _keys(ctrl)[mark:]
        assert ("key_press", ROLL_RIGHT_KEY) in after and ("key_press", NOSE_UP_KEY) in after
    finally:
        _end_chase(ctrl)


def test_the_chase_says_when_it_yields_and_resumes(monkeypatch, caplog):
    with caplog.at_level(logging.INFO, logger="wingman.controller"):
        ctrl = _start_chase(monkeypatch)
        try:
            ctrl._pursuit_recovery.set()
            time.sleep(0.7)
            ctrl._pursuit_recovery.clear()
            time.sleep(0.7)
        finally:
            _end_chase(ctrl)
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "yielding pitch and roll to the dive recovery" in text
    assert "dive recovery over, steering resumes" in text


def test_a_real_recovery_hold_makes_the_real_chase_yield(monkeypatch):
    """End to end inside the controller: the hold sets its own flag, the loop reads it."""
    ctrl = _start_chase(monkeypatch)
    try:
        time.sleep(0.4)
        ctrl.climb_mode(target_alt=5000, max_s=5.0, emergency=True)
        assert _wait(ctrl.pursuit_recovery_active, timeout=2.0)
        time.sleep(0.5)
        mark = len(_keys(ctrl))
        time.sleep(0.9)
        during = [k for k in _keys(ctrl)[mark:] if k[0] == "key_press" and k[1] in (ROLL_LEFT_KEY, ROLL_RIGHT_KEY)]
        assert during == [], "the chase pressed roll while the recovery was flying"
        assert ctrl._roll_held is None and ctrl._pitch_held is None, \
            "the chase kept a roll or pitch key held under the recovery"
    finally:
        _stop(ctrl)
        _end_chase(ctrl)


# --- the instrumentation added with it ------------------------------------------

def test_pitch_holds_are_logged_like_roll_holds(monkeypatch, caplog):
    ctrl = _ctrl(monkeypatch, _Analyzer())
    with caplog.at_level(logging.DEBUG, logger="wingman.controller"):
        ctrl.orient_pitch_to_target(0.4, sustained_hold=True)      # down
        ctrl.orient_pitch_to_target(0.4, sustained_hold=True)      # unchanged: no line
        ctrl.orient_pitch_to_target(-0.4, sustained_hold=True)     # up
        ctrl.orient_pitch_to_target(0.0, sustained_hold=True)      # into the deadband
    lines = [r.getMessage() for r in caplog.records if "HOLD[pitch]" in r.getMessage()]
    assert lines == [
        "HOLD[pitch]: None -> down (target err_y=+0.400)",
        "HOLD[pitch]: down -> up (target err_y=-0.400)",
        "HOLD[pitch]: up -> None (deadband err_y=+0.000)",
    ]


def test_the_shipped_config_enables_the_recovery(monkeypatch):
    import pathlib

    import yaml
    cfg = yaml.safe_load((pathlib.Path(__file__).resolve().parents[1] / "wingman" / "config.yaml")
                         .read_text(encoding="utf-8"))
    assert cfg["pursuit_mode"]["recovery_max_s"] > 0
    cc = ControllerConfig.from_config(cfg)
    assert cc.pursuit_mode["recovery_max_s"] == cfg["pursuit_mode"]["recovery_max_s"]
