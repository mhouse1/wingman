"""The extracted Linux input subsystem (Future 002 A-01).

Two concerns: the extraction must not have broken any of the import paths other
modules and the test suite rely on, and the XRecord listener's closures must
bind their loop variables rather than capture them.
"""

import inspect
import pathlib
import threading

import pytest

import wingman.controller as controller_module
from wingman import input_linux


def test_controller_still_re_exports_the_moved_symbols():
    """conftest.py, move_game_window.py and existing tests import these from
    wingman.controller; the extraction must keep those paths working."""
    for name in ("_ensure_xauthority", "_linux_click", "_linux_key_event",
                 "_XKEY_ALIASES", "_XKeyEvent", "_LinuxXTestKeyboard",
                 "_WINGMAN_XAUTH"):
        assert hasattr(controller_module, name), name
        assert getattr(controller_module, name) is getattr(input_linux, name)


def test_keyboard_module_is_a_controller_level_name():
    """Every test monkeypatches controller.keyboard_module; it must stay a
    module attribute of controller, not an alias into input_linux."""
    assert "keyboard_module" in vars(controller_module)


def test_punctuation_keys_resolve_through_the_alias_table():
    """ADR 070 V1: string_to_keysym(';') returns 0, so YAW_LEFT needs its X11
    keysym NAME or the release is silently dropped and the key latches."""
    assert input_linux._XKEY_ALIASES[";"] == "semicolon"
    for key in (",", ".", "/", "\\", "[", "]", "-", "=", "`", "'"):
        assert key in input_linux._XKEY_ALIASES


@pytest.mark.parametrize("closure, expected", [
    ("_stop_watcher", {"iter_done", "d_ctrl", "ctx"}),
    ("_record_handler", {"_ef", "d_rec", "display_name"}),
])
def test_listener_closures_bind_their_loop_variables(closure, expected):
    """ruff B023. `_stop_watcher` runs in its own thread and can outlive the
    iteration that created it (the reconnect path sleeps 3 s before rebinding).
    A late-bound closure would then read the NEXT iteration's Event and Display
    and disable the live record context — silently killing hotkeys, and with
    them the SAF-001 manual-takeover path."""
    source = inspect.getsource(input_linux._LinuxXTestKeyboard._listener_loop)
    signature = source.split(f"def {closure}(", 1)[1].split(")", 1)[0]
    for name in expected:
        assert f"{name}={name}" in signature, (
            f"{closure} must bind {name} as a default argument, not capture it")


def test_shim_matches_the_keyboard_module_call_signature():
    """controller.py calls on_press_key(..., suppress=False) by keyword."""
    params = inspect.signature(input_linux._LinuxXTestKeyboard.on_press_key).parameters
    assert "suppress" in params


def test_maybe_install_returns_the_fallback_on_windows(monkeypatch):
    sentinel = object()
    monkeypatch.setattr(input_linux.sys, "platform", "win32")
    assert input_linux.maybe_install_linux_keyboard(sentinel) is sentinel


def test_maybe_install_returns_the_shim_elsewhere(monkeypatch):
    monkeypatch.setattr(input_linux.sys, "platform", "linux")
    installed = input_linux.maybe_install_linux_keyboard(None)
    assert isinstance(installed, input_linux._LinuxXTestKeyboard)


def test_listener_thread_is_stoppable():
    """CLAUDE.md: long-running daemon threads must be stoppable via an Event."""
    kbd = input_linux._LinuxXTestKeyboard()
    assert isinstance(kbd._stop, threading.Event)
    kbd.unhook_all()
    assert kbd._stop.is_set()


def test_module_scope_imports_are_stdlib_only():
    """Invariant 2 of the module docstring: wingman runs on Windows too, and
    `controller.py` imports `input_linux` unconditionally to re-export its
    symbols. Hoisting `from Xlib import ...` to module scope would break Windows
    startup silently — nothing on Linux would fail. Xlib must stay lazy."""
    import ast
    import sys as _sys

    source = pathlib.Path(input_linux.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)

    module_level = []
    for node in tree.body:                     # top level only, not nested scopes
        if isinstance(node, ast.Import):
            module_level += [a.name.split(".")[0] for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            module_level.append(node.module.split(".")[0])

    non_stdlib = [m for m in module_level if m not in _sys.stdlib_module_names]
    assert not non_stdlib, (
        f"{non_stdlib} imported at module scope in input_linux.py — this module "
        "must stay importable on Windows, where Xlib is absent"
    )


def test_controller_imports_without_a_display(monkeypatch):
    """A Windows import of controller.py must not touch Xlib or the X server."""
    monkeypatch.setattr(input_linux.sys, "platform", "win32")
    sentinel = object()
    assert input_linux.maybe_install_linux_keyboard(sentinel) is sentinel
