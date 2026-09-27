"""CR-018-13: building a Controller registers no hotkeys; main does, once.

Registration used to run at the end of Controller.__init__, so constructing one
with a keyboard backend hooked eleven global keys and, on Linux, started the
XRecord listener. Tests avoided it with disable_hotkeys (71 times) or by
bypassing the constructor with Controller.__new__.
"""

import logging
import pathlib
import threading
import types

import wingman.controller as controller_module
from wingman.analyzer import GameState
from wingman.controller import Controller
from wingman.controller_config import ControllerConfig
from wingman.keybindings import (
    AUTO_MISSION_KEY, CANCEL_MISSION_KEY, CAPTURE_SCREEN_SHOT, FINISH_ROUND_THEN_EXIT,
    MISSION_J20_KEY, MISSION_LOITER_KEY, MISSION_SU30_KEY, PADLOCK_CAMERA,
    SIMULATE_RESPAWN_KEY, TOGGLE_WEAPON_LOOP_KEY, _WATCHED_MANEUVER_KEYS,
)


class _RecordingKeyboard:
    def __init__(self, fail_first_with=None):
        self.pressed_keys = {}
        self.hotkeys = []
        self._fail = fail_first_with

    def on_press_key(self, key, callback, suppress=False):
        if self._fail is not None:
            exc, self._fail = self._fail, None
            raise exc
        self.pressed_keys[key] = callback

    def add_hotkey(self, key, callback):
        self.hotkeys.append(key)


def _ctrl(monkeypatch, keyboard, **cfg):
    monkeypatch.setattr(controller_module, "keyboard_module", keyboard)
    return Controller((0, 0, 1920, 1200),
                      analyzer=types.SimpleNamespace(game_state=GameState.GAME_LOBBY),
                      exit_event=threading.Event(),
                      config=ControllerConfig(**cfg))


def test_constructing_a_controller_registers_nothing(monkeypatch):
    kb = _RecordingKeyboard()
    _ctrl(monkeypatch, kb)
    assert kb.pressed_keys == {} and kb.hotkeys == [], \
        "Controller.__init__ must not touch the keyboard backend"


def test_register_hotkeys_registers_every_operator_key(monkeypatch):
    kb = _RecordingKeyboard()
    ctrl = _ctrl(monkeypatch, kb)
    ctrl.register_hotkeys()
    expected = ({"backspace", CANCEL_MISSION_KEY, MISSION_J20_KEY, MISSION_LOITER_KEY,
                 MISSION_SU30_KEY, FINISH_ROUND_THEN_EXIT, SIMULATE_RESPAWN_KEY,
                 CAPTURE_SCREEN_SHOT, PADLOCK_CAMERA, AUTO_MISSION_KEY}
                | set(_WATCHED_MANEUVER_KEYS))
    assert set(kb.pressed_keys) == expected
    assert kb.hotkeys == [TOGGLE_WEAPON_LOOP_KEY]
    assert ctrl._exit_script_hotkey is kb.pressed_keys["backspace"], \
        "cleanup(keep_hotkeys=True) re-registers this closure for standby"


def test_disable_hotkeys_still_disables_them(monkeypatch):
    """Replay and capture modes: ambient host keys must not drive the run."""
    kb = _RecordingKeyboard()
    ctrl = _ctrl(monkeypatch, kb, disable_hotkeys=True)
    ctrl.register_hotkeys()
    assert kb.pressed_keys == {} and kb.hotkeys == []


def test_no_input_permission_warns_once_and_skips_the_rest(monkeypatch, caplog):
    """The Backspace registration is the probe: an ImportError there (Linux user
    not in the 'input' group) disables every hotkey with one warning."""
    kb = _RecordingKeyboard(fail_first_with=ImportError("no access to /dev/input"))
    ctrl = _ctrl(monkeypatch, kb)
    with caplog.at_level(logging.WARNING, logger="wingman.hotkeys"):
        ctrl.register_hotkeys()
    assert kb.pressed_keys == {} and kb.hotkeys == []
    assert "keyboard hotkeys disabled" in caplog.text


def test_main_registers_them_straight_after_construction():
    """Same moment in startup as when __init__ did it: before any wiring that
    could let a key press reach a half-built session."""
    src = pathlib.Path("wingman/main.py").read_text()
    built = src.index("    ctrl = Controller(\n")
    registered = src.index("ctrl.register_hotkeys()", built)
    next_wiring = src.index("ctrl.set_target_tracker(", built)
    assert registered < next_wiring
