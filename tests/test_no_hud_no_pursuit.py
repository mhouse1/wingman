"""ADR 200 — no HUD, no start key and no pursuit.

2026-11-20 09:25:49: the operator typed "funf" into the lobby's party chat during
matchmaking. The 'u' started the configured mission in GAME_STARTING, the Enter
took manual, and the next 'u' (09:26:11) resumed it. The pursuit then steered at
a false icon on the hangar jet and tapped 'f' into the chat box 211 times, with
no health, speed or altitude read off any frame.
"""

import threading
import time

import wingman.controller as controller_module
from wingman.analyzer import GameState
from wingman.config_schema import schema_default
from wingman.controller import Controller
from wingman.controller_config import ControllerConfig
from wingman.telemetry import TelemetrySignal, TelemetrySnapshot
from tests.test_pursuit_mode import (
    _AnalyzerStub as _PursuitAnalyzer, _CaptureStub, _TrackerStub,
    _wait_for_pursuit_to_settle,
)
from tests.test_controller_no_keyboard import (
    _AnalyzerStub as _HotkeyAnalyzer, _KeyboardStub, _ThreadStub, _load_config,
)


def _snapshot(altitude_age_s):
    """A real TelemetrySnapshot whose altitude was read altitude_age_s ago (None: never)."""
    now = time.time()
    ts = None if altitude_age_s is None else now - altitude_age_s
    return TelemetrySnapshot(altitude=TelemetrySignal(value=900 if ts else None, ts=ts),
                             taken_at_s=now)


class _Telemetry:
    """Mixin: get_telemetry returns a snapshot with the given altitude age."""
    altitude_age_s = None
    no_snapshot = False

    def get_telemetry(self):
        if self.no_snapshot:
            return None
        return _snapshot(self.altitude_age_s)


class _HudHotkeyAnalyzer(_Telemetry, _HotkeyAnalyzer):
    pass


class _HudPursuitAnalyzer(_Telemetry, _PursuitAnalyzer):
    pass


def test_defaults_are_declared():
    assert schema_default("pursuit_mode.no_hud_stop_s") == 15.0
    assert schema_default("pursuit_mode.start_key_hud_window_s") == 30.0


# ---------------------------------------------------------------------------
# The start keys
# ---------------------------------------------------------------------------

def _hotkey_ctrl(monkeypatch, state, altitude_age_s):
    keyboard_stub = _KeyboardStub()
    monkeypatch.setattr(controller_module, "keyboard_module", keyboard_stub)
    monkeypatch.setattr(controller_module.threading, "Thread", _ThreadStub)
    cfg = _load_config()
    analyzer = _HudHotkeyAnalyzer(state)
    analyzer.altitude_age_s = altitude_age_s
    ctrl = Controller((0, 0, cfg["region"]["width"], cfg["region"]["height"]),
                      analyzer=analyzer)
    ctrl.register_hotkeys()
    ctrl.mission_j20 = lambda **_: None
    ctrl.mission_acs = lambda **_: None
    _ThreadStub.started_targets = []
    return ctrl, analyzer, keyboard_stub


def _press(keyboard_stub, key):
    keyboard_stub.handlers[key](type("_Event", (), {"name": key})())


class TestStartKeys:
    def test_u_in_matchmaking_with_no_hud_starts_nothing(self, monkeypatch):
        """The 09:25:49 press: GAME_STARTING, last altitude 64 s before."""
        _, analyzer, kb = _hotkey_ctrl(monkeypatch, GameState.GAME_STARTING, 64.0)
        _press(kb, controller_module.MISSION_J20_KEY)
        assert _ThreadStub.started_targets == []
        assert analyzer.trigger_calls == []

    def test_u_from_manual_with_no_hud_does_not_resume(self, monkeypatch):
        """The 09:26:11 press: GAME_BATTLE_MANUAL in the lobby."""
        _, analyzer, kb = _hotkey_ctrl(monkeypatch, GameState.GAME_BATTLE_MANUAL, 85.0)
        _press(kb, controller_module.MISSION_J20_KEY)
        assert _ThreadStub.started_targets == []
        assert analyzer.trigger_calls == []

    def test_u_with_no_altitude_ever_starts_nothing(self, monkeypatch):
        _, analyzer, kb = _hotkey_ctrl(monkeypatch, GameState.GAME_LOBBY, None)
        _press(kb, controller_module.MISSION_J20_KEY)
        assert _ThreadStub.started_targets == []

    def test_o_with_no_hud_starts_nothing(self, monkeypatch):
        _, analyzer, kb = _hotkey_ctrl(monkeypatch, GameState.GAME_STARTING, 64.0)
        _press(kb, controller_module.MISSION_ACS_KEY)
        assert _ThreadStub.started_targets == []
        assert analyzer.trigger_calls == []

    def test_u_with_the_hud_up_still_resumes(self, monkeypatch):
        _, analyzer, kb = _hotkey_ctrl(monkeypatch, GameState.GAME_BATTLE_MANUAL, 1.0)
        _press(kb, controller_module.MISSION_J20_KEY)
        assert analyzer.trigger_calls == ["manual_force_battle"]
        assert len(_ThreadStub.started_targets) == 1

    def test_u_on_the_respawn_screen_still_works(self, monkeypatch):
        """The HUD is gone for a respawn's 10-20 s; inside the 30 s window."""
        _, analyzer, kb = _hotkey_ctrl(monkeypatch, GameState.GAME_BATTLE_MANUAL, 20.0)
        _press(kb, controller_module.MISSION_J20_KEY)
        assert len(_ThreadStub.started_targets) == 1

    def test_no_telemetry_to_ask_fails_open(self, monkeypatch):
        ctrl, analyzer, kb = _hotkey_ctrl(monkeypatch, GameState.GAME_STARTING, None)
        analyzer.no_snapshot = True
        assert ctrl.start_key_has_hud("u") is True

    def test_zero_window_turns_the_check_off(self, monkeypatch):
        ctrl, _, _ = _hotkey_ctrl(monkeypatch, GameState.GAME_STARTING, 500.0)
        ctrl._start_key_hud_window_s = 0.0
        assert ctrl.start_key_has_hud("u") is True


# ---------------------------------------------------------------------------
# The pursuit
# ---------------------------------------------------------------------------

def _pursuit_ctrl(monkeypatch, analyzer, no_hud_stop_s):
    monkeypatch.setattr(controller_module, "keyboard_module", None)
    ctrl = Controller(
        (0, 0, 1920, 1200), analyzer=analyzer, exit_event=threading.Event(),
        capture=_CaptureStub(),
        config=ControllerConfig(
            simulate_os_input=True, disable_hotkeys=True,
            telemetry={"eject_closed_loop": {"enabled": False, "legacy_nose_hold_s": 0.05,
                                             "eject_max_s": 0.2}},
            pursuit_mode={"enabled": True, "pursuit_max_duration_s": 0.0,
                          "pursuit_padlock_verify": False, "ammo_zero_grace_s": 0.0,
                          "no_hud_stop_s": no_hud_stop_s},
        ),
    )
    ctrl.set_target_tracker(_TrackerStub())
    return ctrl


class TestPursuitStopsWithNoHud:
    def test_pursuit_with_no_hud_stops_and_hands_nothing_on(self, monkeypatch):
        analyzer = _HudPursuitAnalyzer(ammo=2)
        analyzer.altitude_age_s = 64.0
        ctrl = _pursuit_ctrl(monkeypatch, analyzer, no_hud_stop_s=0.3)
        resumed = []
        monkeypatch.setattr(ctrl, "_resume_after_rearm", lambda: resumed.append(1))
        done = []
        ctrl.pursue_and_engage(on_complete=lambda: done.append(1))
        _wait_for_pursuit_to_settle(ctrl, timeout=5.0)
        assert ctrl._eject_thread is None, "no fall-through to eject_and_dive"
        assert resumed == [], "no hand-back to the mission"
        assert done == [1]

    def test_stop_counts_from_the_pursuit_start(self, monkeypatch):
        """A stale read from the previous round does not stop it at once: the
        window is the pursuit's own time too (here 0.3 s, so it is still up at 0.1 s)."""
        analyzer = _HudPursuitAnalyzer(ammo=2)
        analyzer.altitude_age_s = None
        ctrl = _pursuit_ctrl(monkeypatch, analyzer, no_hud_stop_s=0.3)
        ctrl.pursue_and_engage()
        time.sleep(0.1)
        assert ctrl.is_pursuing()
        _wait_for_pursuit_to_settle(ctrl, timeout=5.0)

    def test_pursuit_with_the_hud_keeps_going(self, monkeypatch):
        analyzer = _HudPursuitAnalyzer(ammo=2)
        analyzer.altitude_age_s = 0.0   # every read is fresh
        ctrl = _pursuit_ctrl(monkeypatch, analyzer, no_hud_stop_s=0.3)
        ctrl.pursue_and_engage()
        time.sleep(0.8)
        assert ctrl.is_pursuing()
        ctrl._eject_stop.set()
        _wait_for_pursuit_to_settle(ctrl, timeout=3.0)

    def test_no_telemetry_to_ask_keeps_going(self, monkeypatch):
        analyzer = _HudPursuitAnalyzer(ammo=2)
        analyzer.no_snapshot = True
        ctrl = _pursuit_ctrl(monkeypatch, analyzer, no_hud_stop_s=0.3)
        ctrl.pursue_and_engage()
        time.sleep(0.8)
        assert ctrl.is_pursuing()
        ctrl._eject_stop.set()
        _wait_for_pursuit_to_settle(ctrl, timeout=3.0)
