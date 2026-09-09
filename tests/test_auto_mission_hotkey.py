"""AUTO_MISSION_KEY ('m') battle-state guard (2026-08-17 incident).

A single 'm' press during a battle state must NOT force GAME_LOBBY: at
04:15:41 on 2026-08-17, 'm' pressed mid-manual-flight forced the FSM to
GAME_LOBBY, clicked PLAY into the battlefield, and left the lobby quick-scan
pressing ESC against the running game. A deliberate double press within 2 s
keeps the stuck-state recovery available.

GAME_BATTLE_MANUAL is the one carve-out, and it postdates that incident.
'm' is registered as a handback key alongside 'u' (main.py, SAF-001), so in
manual it means "wingman, take it back" and acts on the first press. The two
are not in tension: the guarded path is the destructive one (force GAME_LOBBY
and click PLAY into a live battlefield), while handback only returns to
GAME_BATTLE, which the operator undoes by pressing the takeover key again.
"""

import threading
import time

import wingman.controller as controller_module
from wingman.controller_config import ControllerConfig
from wingman.analyzer import GameState
from wingman.controller import Controller


class _FakeKeyboard:
    def press(self, key):
        pass

    def release(self, key):
        pass


class _FakeFSMAnalyzer:
    def __init__(self, state):
        self.game_state = state
        self.triggered = []

    def trigger_event(self, name):
        self.triggered.append(name)
        if name == "manual_reset":
            self.game_state = GameState.GAME_LOBBY
        elif name == "manual_release":
            self.game_state = GameState.GAME_BATTLE
        return True


def _make_ctrl(monkeypatch, analyzer):
    monkeypatch.setattr(controller_module, "keyboard_module", _FakeKeyboard())
    return Controller(
        (0, 0, 1920, 1200),
        analyzer=analyzer,
        exit_event=threading.Event(),
        capture=None,
        config=ControllerConfig(
            disable_hotkeys=True,
        )
    )


def test_single_press_in_battle_is_refused(monkeypatch):
    """GAME_BATTLE_MANUAL is deliberately absent — see the handback test."""
    for state in (GameState.GAME_BATTLE, GameState.GAME_BATTLE_EJECT):
        analyzer = _FakeFSMAnalyzer(state)
        ctrl = _make_ctrl(monkeypatch, analyzer)
        ctrl._on_auto_mission_hotkey()
        assert analyzer.triggered == [], (
            f"single press forced lobby from {state.name}")


def test_single_press_in_manual_hands_control_back(monkeypatch):
    """SAF-001: in manual the key means "wingman, take it" and acts at once.

    Waiting for a second press would leave the operator flying an aircraft they
    have already asked to give up. The recovery it costs is still reachable —
    this press lands in GAME_BATTLE, where the double-press guard applies.
    """
    analyzer = _FakeFSMAnalyzer(GameState.GAME_BATTLE_MANUAL)
    ctrl = _make_ctrl(monkeypatch, analyzer)
    ctrl._on_auto_mission_hotkey()
    assert analyzer.triggered == ["manual_release"]
    assert ctrl._auto_respawn_restart is True, (
        "handback must re-arm auto respawn, or wingman flies but never restarts")


def test_double_press_in_battle_forces_lobby(monkeypatch):
    analyzer = _FakeFSMAnalyzer(GameState.GAME_BATTLE)
    ctrl = _make_ctrl(monkeypatch, analyzer)
    ctrl._on_auto_mission_hotkey()
    assert analyzer.triggered == []
    # Step past the 0.5 s key-repeat debounce but stay inside the 2 s
    # double-press window.
    ctrl._last_auto_mission_key_ts = time.time() - 0.6
    ctrl._on_auto_mission_hotkey()
    assert analyzer.triggered == ["manual_reset"]


def test_double_press_window_expires(monkeypatch):
    analyzer = _FakeFSMAnalyzer(GameState.GAME_BATTLE)
    ctrl = _make_ctrl(monkeypatch, analyzer)
    ctrl._on_auto_mission_hotkey()
    ctrl._last_auto_mission_key_ts = time.time() - 0.6
    ctrl._auto_mission_force_armed_ts = time.time() - 2.5   # stale arm
    ctrl._on_auto_mission_hotkey()
    assert analyzer.triggered == [], "expired double-press window still forced lobby"


def test_non_battle_press_forces_lobby_immediately(monkeypatch):
    analyzer = _FakeFSMAnalyzer(GameState.GAME_UNKNOWN)
    ctrl = _make_ctrl(monkeypatch, analyzer)
    ctrl._on_auto_mission_hotkey()
    assert analyzer.triggered == ["manual_reset"]


def test_key_repeat_debounce_ignored_outright(monkeypatch):
    analyzer = _FakeFSMAnalyzer(GameState.GAME_BATTLE)
    ctrl = _make_ctrl(monkeypatch, analyzer)
    ctrl._on_auto_mission_hotkey()
    ctrl._on_auto_mission_hotkey()   # <0.5 s later: key repeat
    assert analyzer.triggered == []
