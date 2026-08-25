"""The extracted Linux input subsystem (Future 002 A-01).

Two concerns: the extraction must not have broken any of the import paths other
modules and the test suite rely on, and the XRecord listener's closures must
bind their loop variables rather than capture them.
"""

import inspect
import pathlib
import threading
import time

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


# --- Shared XTest display (ADR 091) -----------------------------------------
#
# Key injection opened a throwaway Xlib Display per event. Each construction
# retains ~16.2 KB that survives close() and gc.collect(), which measured as
# 1,277 MB over a 1h46m session — 96% of all post-warm-up heap growth
# (Performance 008). These tests pin the fix: one connection, reused, with the
# lock the per-call Displays used to provide for free.

class _FakeXtest:
    def __init__(self):
        self.injected = []

    def fake_input(self, d, event_type, keycode=None, **kw):
        self.injected.append((d, event_type, keycode))


class _FakeDisplay:
    def __init__(self, name, fail_on_inject=False):
        self.name = name
        self.closed = False
        self.syncs = 0

    def keysym_to_keycode(self, keysym):
        return 42

    def sync(self):
        self.syncs += 1

    def close(self):
        self.closed = True


@pytest.fixture
def xtest_env(monkeypatch):
    """Wire input_linux to fake Xlib pieces and reset the shared connection."""
    import sys
    import types

    opened = []

    def _factory(name):
        d = _FakeDisplay(name)
        opened.append(d)
        return d

    fake_display_mod = types.ModuleType("Xlib.display")
    fake_display_mod.Display = _factory
    fake_xk = types.ModuleType("Xlib.XK")
    fake_xk.string_to_keysym = lambda n: 99
    xtest = _FakeXtest()
    fake_xtest_mod = types.ModuleType("Xlib.ext.xtest")
    fake_xtest_mod.fake_input = xtest.fake_input

    monkeypatch.setitem(sys.modules, "Xlib.display", fake_display_mod)
    monkeypatch.setitem(sys.modules, "Xlib.XK", fake_xk)
    monkeypatch.setitem(sys.modules, "Xlib.ext.xtest", fake_xtest_mod)
    monkeypatch.setattr(input_linux, "_ensure_xauthority", lambda: None)
    monkeypatch.setattr(input_linux, "_shared_display", None, raising=False)

    yield types.SimpleNamespace(opened=opened, xtest=xtest)

    input_linux._drop_shared_display()


def test_repeated_key_events_open_exactly_one_display(xtest_env):
    """The leak itself: 200 injections must not be 200 X11 connections."""
    for _ in range(200):
        input_linux._linux_key_event("k", "KeyPress")
    assert len(xtest_env.opened) == 1, (
        f"{len(xtest_env.opened)} displays opened for 200 key events — "
        "each construction retains ~16.2 KB permanently (ADR 091)")
    assert len(xtest_env.xtest.injected) == 200, "every event must still inject"


def test_the_shared_display_is_not_closed_between_events(xtest_env):
    input_linux._linux_key_event("k", "KeyPress")
    input_linux._linux_key_event("k", "KeyRelease")
    assert xtest_env.opened[0].closed is False


def test_injection_failure_drops_the_connection_and_the_retry_reconnects(xtest_env, monkeypatch):
    """A half-dead connection must never carry the release half of a pair."""
    calls = {"n": 0}

    def flaky(d, event_type, keycode=None, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("broken pipe")
        xtest_env.xtest.injected.append((d, event_type, keycode))

    import sys
    monkeypatch.setattr(sys.modules["Xlib.ext.xtest"], "fake_input", flaky)

    input_linux._linux_key_event("k", "KeyPress")

    assert len(xtest_env.opened) == 2, "retry must open a fresh connection"
    assert xtest_env.opened[0].closed is True, "the broken one must be closed"
    assert len(xtest_env.xtest.injected) == 1, "the retry must deliver the event"


def test_persistent_failure_gives_up_after_two_attempts(xtest_env, monkeypatch, caplog):
    import sys

    def always_fail(d, event_type, keycode=None, **kw):
        raise OSError("display gone")

    monkeypatch.setattr(sys.modules["Xlib.ext.xtest"], "fake_input", always_fail)
    monkeypatch.setattr(input_linux.time, "sleep", lambda s: None)

    with caplog.at_level("ERROR"):
        input_linux._linux_key_event("k", "KeyPress")

    assert len(xtest_env.opened) == 2, "exactly two attempts, not an unbounded retry"
    assert input_linux._shared_display is None, "must not leave a dead connection cached"
    assert any("failed after retry" in r.getMessage() for r in caplog.records)


def test_unknown_keysym_opens_no_display(xtest_env, monkeypatch):
    import sys
    monkeypatch.setattr(sys.modules["Xlib.XK"], "string_to_keysym", lambda n: 0)
    input_linux._linux_key_event("nosuchkey", "KeyPress")
    assert xtest_env.opened == []


def test_concurrent_injection_is_serialised(xtest_env):
    """Xlib Displays are not safe for concurrent use, and injection comes from
    the main loop, the behaviour tree and hotkey callbacks. The per-call
    Displays isolated those for free; the shared one needs the lock."""
    overlap = {"max": 0, "cur": 0}
    guard = threading.Lock()
    import sys

    def watched(d, event_type, keycode=None, **kw):
        with guard:
            overlap["cur"] += 1
            overlap["max"] = max(overlap["max"], overlap["cur"])
        time.sleep(0.001)
        with guard:
            overlap["cur"] -= 1

    sys.modules["Xlib.ext.xtest"].fake_input = watched
    threads = [threading.Thread(target=input_linux._linux_key_event, args=("k", "KeyPress"))
               for _ in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert overlap["max"] == 1, "injections overlapped on the shared connection"
    assert len(xtest_env.opened) == 1


def test_drop_shared_display_survives_a_failing_close(xtest_env):
    """Cleanup must not raise on an already-broken connection."""
    input_linux._linux_key_event("k", "KeyPress")

    def boom():
        raise OSError("already gone")

    xtest_env.opened[0].close = boom
    input_linux._drop_shared_display()          # must not raise
    assert input_linux._shared_display is None
