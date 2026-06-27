"""
Tests for Controller behaviour when the keyboard module is unavailable (item 5.2).

Verifies that Controller initialises without exception when the module-level
keyboard_module is None and that game-control methods degrade gracefully instead
of raising AttributeError.
"""

import threading
import pytest
import yaml
import wingman.controller as controller_module

from constants import CONFIG_PATH
from wingman.controller import Controller
from wingman.analyzer import GameState


def _load_config():
    with CONFIG_PATH.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


@pytest.fixture
def ctrl(monkeypatch):
    """Controller with keyboard_module patched to None."""
    monkeypatch.setattr(controller_module, "keyboard_module", None)
    cfg = _load_config()
    region = (0, 0, cfg["region"]["width"], cfg["region"]["height"])
    exit_event = threading.Event()
    c = Controller(
        region,
        analyzer=None,
        weapon_loop_interval=0.5,
        exit_event=exit_event,
        capture=None,
        on_auto_mission_key=None,
    )
    yield c
    exit_event.set()
    c.cancel_mission()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_init_without_keyboard(ctrl):
    """Controller must initialise without raising when keyboard_module is None."""
    assert ctrl is not None


def test_deploy_flares_without_keyboard(ctrl):
    """deploy_flares() must not raise when keyboard is None."""
    ctrl.deploy_flares(hold_seconds=0.0)


def test_cancel_mission_without_keyboard(ctrl):
    """cancel_mission() must not raise when keyboard is None."""
    ctrl.cancel_mission()


def test_is_mission_running_without_keyboard(ctrl):
    """is_mission_running() must return a bool without raising."""
    result = ctrl.is_mission_running()
    assert isinstance(result, bool)


def test_restart_last_mission_no_history(ctrl):
    """restart_last_mission() defaults to J20 and returns True when no prior mission recorded."""
    result = ctrl.restart_last_mission()
    assert result is True
    # _last_mission must now be set so future restarts also work
    with ctrl._last_mission_lock:
        assert ctrl._last_mission == "j20"
    ctrl.cancel_mission()  # stop the spawned thread


def test_restart_last_mission_returns_false_when_running(ctrl):
    """restart_last_mission() returns False when the mission lock is held."""
    ctrl._mission_lock.acquire(blocking=False)
    try:
        result = ctrl.restart_last_mission()
        assert result is False
    finally:
        if ctrl._mission_lock.locked():
            ctrl._mission_lock.release()


class _AnalyzerStub:
    def __init__(self, state: GameState):
        self.game_state = state
        self.trigger_calls = []

    def trigger_event(self, name: str):
        self.trigger_calls.append(name)
        return True


class _KeyboardStub:
    def __init__(self):
        self.handlers = {}

    def on_press_key(self, key, callback, suppress=False):
        self.handlers[key] = callback

    def add_hotkey(self, _key, _callback):
        return None


class _ThreadStub:
    started_targets = []

    def __init__(self, target=None, daemon=None, args=None, kwargs=None):
        self._target = target
        self._daemon = daemon
        self._args = args or ()
        self._kwargs = kwargs or {}

    def start(self):
        _ThreadStub.started_targets.append(self._target)


def test_maneuver_key_triggers_manual_takeover_in_battle(monkeypatch):
    """CR-003-12: maneuver key path should cancel mission and trigger manual_takeover in battle."""
    monkeypatch.setattr(controller_module, "keyboard_module", None)
    cfg = _load_config()
    region = (0, 0, cfg["region"]["width"], cfg["region"]["height"])
    analyzer = _AnalyzerStub(GameState.GAME_BATTLE)
    ctrl = Controller(region, analyzer=analyzer)

    ctrl._mission_lock.acquire(blocking=False)
    try:
        handled = ctrl._handle_maneuver_key_press("j", is_injected=False)
        assert handled is True
        assert ctrl._mission_cancel.is_set() is True
        assert ctrl._auto_respawn_restart is False
        assert analyzer.trigger_calls == ["manual_takeover"]
    finally:
        if ctrl._mission_lock.locked():
            ctrl._mission_lock.release()


def test_maneuver_key_does_not_trigger_manual_takeover_outside_battle(monkeypatch):
    """Maneuver key still cancels mission outside battle but should not fire manual_takeover trigger."""
    monkeypatch.setattr(controller_module, "keyboard_module", None)
    cfg = _load_config()
    region = (0, 0, cfg["region"]["width"], cfg["region"]["height"])
    analyzer = _AnalyzerStub(GameState.GAME_LOBBY)
    ctrl = Controller(region, analyzer=analyzer)

    ctrl._mission_lock.acquire(blocking=False)
    try:
        handled = ctrl._handle_maneuver_key_press("j", is_injected=False)
        assert handled is True
        assert ctrl._mission_cancel.is_set() is True
        assert analyzer.trigger_calls == []
    finally:
        if ctrl._mission_lock.locked():
            ctrl._mission_lock.release()


def test_maneuver_key_ignores_injected_events(monkeypatch):
    """Injected/programmatic key events must not force manual takeover."""
    monkeypatch.setattr(controller_module, "keyboard_module", None)
    cfg = _load_config()
    region = (0, 0, cfg["region"]["width"], cfg["region"]["height"])
    analyzer = _AnalyzerStub(GameState.GAME_BATTLE)
    ctrl = Controller(region, analyzer=analyzer)

    ctrl._mission_lock.acquire(blocking=False)
    try:
        handled = ctrl._handle_maneuver_key_press("j", is_injected=True)
        assert handled is False
        assert ctrl._mission_cancel.is_set() is False
        assert analyzer.trigger_calls == []
    finally:
        if ctrl._mission_lock.locked():
            ctrl._mission_lock.release()


def test_j20_hotkey_forces_battle_via_fsm_trigger(monkeypatch):
    """J20 hotkey should call analyzer trigger path instead of direct FSM state assignment."""
    keyboard_stub = _KeyboardStub()
    monkeypatch.setattr(controller_module, "keyboard_module", keyboard_stub)
    monkeypatch.setattr(controller_module.threading, "Thread", _ThreadStub)

    cfg = _load_config()
    region = (0, 0, cfg["region"]["width"], cfg["region"]["height"])
    analyzer = _AnalyzerStub(GameState.GAME_LOBBY)
    ctrl = Controller(region, analyzer=analyzer)
    ctrl.mission_j20 = lambda: None

    _ThreadStub.started_targets = []
    handler = keyboard_stub.handlers[controller_module.MISSION_J20_KEY]
    handler(type("_Event", (), {"name": controller_module.MISSION_J20_KEY})())

    assert analyzer.trigger_calls == ["manual_force_battle"]
    assert len(_ThreadStub.started_targets) == 1
