"""The extracted Linux input subsystem (Future 002 A-01).

Two concerns: the extraction must not have broken any of the import paths other
modules and the test suite rely on, and the XRecord listener's closures must
bind their loop variables rather than capture them.
"""

import inspect
import os
import pathlib
import threading
import time
import unittest.mock as mock

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
    ("_stop_watcher", {"iter_done", "d_ctrl", "d_rec", "ctx", "tally", "deaf_restart"}),
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


# --- ADR 099: injection and observation use DIFFERENT displays ---------------

def test_injection_display_defaults_to_the_environment():
    from wingman import input_linux as il
    il.set_injection_display(None)
    with mock.patch.dict("os.environ", {"DISPLAY": ":0"}):
        assert il._inject_display_name() == ":0"


def test_injection_display_override_does_not_move_observation():
    """The whole point of the split. Capture and injection go to the nested
    display; the XRecord hotkey listener must keep watching the operator's
    display, or backspace / end / i-j-k-l only register while the nested window
    has focus — i.e. exactly when the operator is NOT working elsewhere."""
    from wingman import input_linux as il
    try:
        with mock.patch.dict("os.environ", {"DISPLAY": ":0"}):
            il.set_injection_display(":3")
            assert il._inject_display_name() == ":3"
            # Observation reads os.environ directly and is untouched.
            assert os.environ["DISPLAY"] == ":0"
    finally:
        il.set_injection_display(None)


def test_injection_display_can_be_cleared():
    from wingman import input_linux as il
    il.set_injection_display(":3")
    il.set_injection_display(None)
    with mock.patch.dict("os.environ", {"DISPLAY": ":0"}):
        assert il._inject_display_name() == ":0"


# --- ADR 099 regression: hotkeys died in the nested lane ---------------------

def test_observation_covers_the_operator_display():
    from wingman import input_linux as il
    il.set_injection_display(None)
    with mock.patch.dict("os.environ", {"DISPLAY": ":0"}):
        assert il._observe_display_names() == [":0"]


def test_observation_also_covers_the_injection_display():
    """The 2026-08-29 regression. Keeping the listener on the operator's DISPLAY
    was necessary but not sufficient: on Wayland that DISPLAY is a rootless
    Xwayland which only sees keys while an X11 client has focus. Moving the game
    to its own display removed the only such client, so every hotkey went dead —
    backspace and the SAF-001 manual takeover included. The operator's keys are
    then only visible on the display they are looking at."""
    from wingman import input_linux as il
    try:
        with mock.patch.dict("os.environ", {"DISPLAY": ":0"}):
            il.set_injection_display(":3")
            assert il._observe_display_names() == [":0", ":3"]
    finally:
        il.set_injection_display(None)


def test_the_same_display_is_not_observed_twice():
    """On the on-screen lane both roles are one display; two listeners on it
    would double-deliver every key to the takeover logic."""
    from wingman import input_linux as il
    try:
        with mock.patch.dict("os.environ", {"DISPLAY": ":0"}):
            il.set_injection_display(":0")
            assert il._observe_display_names() == [":0"]
    finally:
        il.set_injection_display(None)


def test_listener_loop_takes_its_display_rather_than_reading_the_environment():
    """Each listener must be pinned to its own display. Reading DISPLAY inside
    the loop would collapse every listener onto the operator's display and
    silently restore the bug."""
    sig = inspect.signature(input_linux._LinuxXTestKeyboard._listener_loop)
    assert "display_name" in sig.parameters
    src = inspect.getsource(input_linux._LinuxXTestKeyboard._listener_loop)
    assert 'os.environ.get("DISPLAY"' not in src


# --- ADR 099: ordinary typing must not fly the aircraft ----------------------
#
# Observed 2026-08-30 08:27. Before the nested lane the game held focus on the
# operator's display, so bare single-letter hotkeys were unreachable by ordinary
# typing — the keys went to the game. Moving the game to its own display freed
# the operator's keyboard, which is the point, and simultaneously made every
# hotkey fire from ordinary typing: stray 'm' presses forced GAME_LOBBY three
# times, cancelling matchmaking, and the session never reached battle. 'z' would
# have closed the game outright.

CTRL_ALT = (1 << 2) | (1 << 3)


def _lane(injection=":3", injected=("p", "u", "i", "k")):
    from wingman import input_linux as il
    il.set_injection_display(injection)
    il.set_injected_keys(injected)
    return il


def test_bare_typing_on_the_operator_display_is_ignored():
    il = _lane()
    try:
        assert il.should_deliver_hotkey(":0", "m", 0) is False
        assert il.should_deliver_hotkey(":0", "z", 0) is False
    finally:
        il.set_injection_display(None)


def test_the_modifier_makes_a_hotkey_deliverable():
    il = _lane()
    try:
        assert il.should_deliver_hotkey(":0", "m", CTRL_ALT) is True
    finally:
        il.set_injection_display(None)


def test_a_partial_modifier_is_not_enough():
    il = _lane()
    try:
        assert il.should_deliver_hotkey(":0", "m", 1 << 2) is False   # ctrl only
        assert il.should_deliver_hotkey(":0", "m", 1 << 3) is False   # alt only
    finally:
        il.set_injection_display(None)


def test_bare_keys_still_work_on_the_nested_display():
    """Typing there requires focusing the game window, which is deliberate."""
    il = _lane()
    try:
        assert il.should_deliver_hotkey(":3", "m", 0) is True
        assert il.should_deliver_hotkey(":3", "z", 0) is True
    finally:
        il.set_injection_display(None)


def test_the_dedicated_takeover_key_reaches_the_handler():
    """SAF-001: Enter is never injected by wingman, so on the injection display
    there is nothing to discriminate — no timing assumption, no calibration.

    The timing-based attempt it replaced was measured failing: echoes arrived
    1.67-9.74 s after release against a 1.0 s grace, producing four spurious
    takeovers in 23 minutes on 2026-08-30."""
    from wingman.controller import INJECTABLE_KEYS
    from wingman.keybindings import MANUAL_TAKEOVER_KEY
    assert MANUAL_TAKEOVER_KEY not in INJECTABLE_KEYS, \
        "a takeover key wingman injects is indistinguishable from its own press"
    il = _lane(injected=INJECTABLE_KEYS)
    try:
        assert il.should_deliver_hotkey(":3", MANUAL_TAKEOVER_KEY, 0) is True
    finally:
        il.set_injection_display(None)


def test_takeover_on_the_injection_display_uses_arrow_keys():
    """SAF-001 names arrow keys alongside i/j/k/l, and wingman never injects
    them — so on the injection display they are the unambiguous takeover path.

    i/j/k/l cannot serve there: echo discrimination assumes a prompt echo, and
    on the nested lane under 13 OCR workers echoes arrived 1.67-9.74 s after
    release against a 1.0 s grace window, causing four spurious takeovers in
    23 minutes on 2026-08-30. Widening the grace would suppress the operator's
    own presses for the same seconds, against SAF-001's 2.0 s bound."""
    il = _lane(injected=("p", "u", "i", "k", "j", "l"))
    try:
        for arrow in ("up", "down", "left", "right"):
            assert il.should_deliver_hotkey(":3", arrow, 0) is True
        for flight in ("i", "j", "k", "l"):
            assert il.should_deliver_hotkey(":3", flight, 0) is False
    finally:
        il.set_injection_display(None)


def test_flight_keys_still_take_over_from_the_operator_display():
    """The i/j/k/l path is not lost — it moves to the operator's display, where
    wingman injects nothing and the modifier separates it from typing."""
    il = _lane(injected=("p", "u", "i", "k", "j", "l"))
    try:
        assert il.should_deliver_hotkey(":0", "i", CTRL_ALT) is True
        assert il.should_deliver_hotkey(":0", "i", 0) is False
    finally:
        il.set_injection_display(None)


def test_wingmans_own_injected_keys_are_ignored_on_the_injection_display():
    """'u' is deliberately NOT covered here — see test_mission_key_is_echo_safe
    below. It is both an injected key and the operator's stuck-state escape
    hatch, so it carries its own exception; 'p' has no such conflict."""
    il = _lane()
    try:
        assert il.should_deliver_hotkey(":3", "p", 0) is False
    finally:
        il.set_injection_display(None)


def test_the_on_screen_lane_is_completely_unchanged():
    """One display, game focused: both filters must be inert."""
    from wingman import input_linux as il
    il.set_injection_display(None)
    for key in ("m", "z", "p", "u"):
        assert il.should_deliver_hotkey(":0", key, 0) is True


def test_every_injectable_key_is_declared():
    """SAF-007's list is now load-bearing twice: keys missing from it are both
    left pressed on exit and able to self-trigger their own hotkey."""
    from wingman.controller import INJECTABLE_KEYS
    from wingman.keybindings import PADLOCK_CAMERA, MISSION_J20_KEY, NOSE_UP_KEY
    for k in (PADLOCK_CAMERA, MISSION_J20_KEY, NOSE_UP_KEY, "escape"):
        assert k in INJECTABLE_KEYS


def test_the_handback_key_is_delivered_only_during_manual():
    """Unit-level check of the _handback_keys mechanism in isolation (no
    _echo_safe_keys configured). In the real running system 'u' is ALSO
    declared echo-safe (see test_mission_key_is_echo_safe below), which
    supersedes this gate for 'u' specifically — this test only pins down that
    the handback gate itself still behaves correctly for whichever key relies
    on it."""
    from wingman.controller import INJECTABLE_KEYS
    from wingman.keybindings import MISSION_J20_KEY
    assert MISSION_J20_KEY in INJECTABLE_KEYS, \
        "if wingman stops injecting 'u', this exception is no longer needed"
    il = _lane(injected=INJECTABLE_KEYS)
    manual = {"on": False}
    il.set_handback_keys((MISSION_J20_KEY,), manual_state_fn=lambda: manual["on"])
    try:
        assert il.should_deliver_hotkey(":3", MISSION_J20_KEY, 0) is False
        manual["on"] = True
        assert il.should_deliver_hotkey(":3", MISSION_J20_KEY, 0) is True
    finally:
        il.set_handback_keys((), manual_state_fn=lambda: False)
        il.set_injection_display(None)


def test_mission_key_is_echo_safe():
    """Regression test (observed 2026-09-01): 'u' forces GAME_BATTLE out of any
    wedged state — including GAME_LOBBY, which is not GAME_BATTLE_MANUAL, so
    the handback gate above does not cover it. Wingman also injects 'u' itself
    (game_starting_loop), so without this exception every genuine press
    outside manual takeover was silently dropped: no hotkey callback, no FSM
    transition, no log line — the operator's press did nothing and the
    GAME_LOBBY escape loop kept firing on its own 45s cycle as if nothing had
    happened. This is exactly the mechanism controller.py's own bracket +
    grace window (_programmatic_key_counts / _prog_release_grace_until)
    already exists to make safe — should_deliver_hotkey must get out of its
    way and let every 'u' event through, in every state."""
    from wingman.controller import INJECTABLE_KEYS
    from wingman.keybindings import MISSION_J20_KEY
    il = _lane(injected=INJECTABLE_KEYS)
    il.set_echo_safe_keys((MISSION_J20_KEY,))
    try:
        # Not in manual, no handback carve-out active: this is the exact
        # "stuck in GAME_LOBBY" scenario, and it must still get through.
        assert il.should_deliver_hotkey(":3", MISSION_J20_KEY, 0) is True
    finally:
        il.set_echo_safe_keys(())
        il.set_injection_display(None)


def test_a_broken_manual_predicate_does_not_open_the_gate():
    from wingman.controller import INJECTABLE_KEYS
    from wingman.keybindings import MISSION_J20_KEY
    def boom():
        raise RuntimeError("state unavailable")
    il = _lane(injected=INJECTABLE_KEYS)
    il.set_handback_keys((MISSION_J20_KEY,), manual_state_fn=boom)
    try:
        assert il.should_deliver_hotkey(":3", MISSION_J20_KEY, 0) is False
    finally:
        il.set_handback_keys((), manual_state_fn=lambda: False)
        il.set_injection_display(None)


def test_the_release_key_needs_ctrl_alt_while_wingman_is_still_running():
    """ADR 099 D4c: Backspace's FIRST press (ending wingman) is exactly D4a's
    case — wingman may still be actively flying, so a bare press must not
    reach the handler."""
    il = _lane()
    stopped = {"on": False}
    il.set_operator_release_keys(("backspace",), operator_stopped_fn=lambda: stopped["on"])
    try:
        assert il.should_deliver_hotkey(":0", "backspace", 0) is False
        assert il.should_deliver_hotkey(":0", "backspace", CTRL_ALT) is True
    finally:
        il.set_operator_release_keys((), operator_stopped_fn=lambda: False)
        il.set_injection_display(None)


def test_the_release_key_bypasses_ctrl_alt_once_wingman_has_stopped():
    """The SECOND press (close everything) fires only after the first press
    already stopped wingman — nothing left running for a stray keypress to
    hijack, so the modifier is no longer protecting anything real."""
    il = _lane()
    stopped = {"on": False}
    il.set_operator_release_keys(("backspace",), operator_stopped_fn=lambda: stopped["on"])
    try:
        stopped["on"] = True
        assert il.should_deliver_hotkey(":0", "backspace", 0) is True
    finally:
        il.set_operator_release_keys((), operator_stopped_fn=lambda: False)
        il.set_injection_display(None)


def test_the_release_key_bypass_does_not_leak_to_other_keys():
    """Only the declared release key gets the bypass — ordinary hotkeys still
    need ctrl+alt even after wingman has stopped."""
    il = _lane()
    il.set_operator_release_keys(("backspace",), operator_stopped_fn=lambda: True)
    try:
        assert il.should_deliver_hotkey(":0", "m", 0) is False
    finally:
        il.set_operator_release_keys((), operator_stopped_fn=lambda: False)
        il.set_injection_display(None)


def test_a_broken_operator_stopped_predicate_does_not_open_the_gate():
    def boom():
        raise RuntimeError("state unavailable")
    il = _lane()
    il.set_operator_release_keys(("backspace",), operator_stopped_fn=boom)
    try:
        assert il.should_deliver_hotkey(":0", "backspace", 0) is False
    finally:
        il.set_operator_release_keys((), operator_stopped_fn=lambda: False)
        il.set_injection_display(None)


def test_key_injection_counts_presses_for_the_deaf_listener_watchdog(xtest_env, monkeypatch):
    """SAF-001 watchdog input: presses are counted per injection display,
    releases are not."""
    monkeypatch.setattr(input_linux, "_injected_press_counts", {})
    display = input_linux._inject_display_name()
    input_linux._linux_key_event("k", "KeyPress")
    input_linux._linux_key_event("k", "KeyRelease")
    input_linux._linux_key_event("k", 2)           # Xlib's X.KeyPress
    assert input_linux.injected_presses(display) == 2



def test_a_deaf_listener_is_restarted(monkeypatch, caplog):
    """SAF-001 watchdog: a recording that delivers nothing while wingman presses
    keys on its display is torn down and set up again, not trusted."""
    import sys
    import types

    created = []

    class _Rec:
        def __init__(self, name):
            self.name = name
            self._disabled = threading.Event()

        def keysym_to_keycode(self, ks):
            return 36

        def record_create_context(self, *a, **k):
            created.append(self.name)
            return object()

        def record_enable_context(self, ctx, handler):
            _shared["active"] = self
            self._disabled.wait(timeout=10)      # deaf: never calls the handler

        def record_disable_context(self, ctx):
            _shared["active"]._disabled.set()

        def record_free_context(self, ctx):
            pass

        def flush(self):
            pass

        def close(self):
            pass

    _shared = {}
    mods = {
        "Xlib.display": types.SimpleNamespace(Display=_Rec),
        "Xlib.X": types.SimpleNamespace(KeyPress=2, KeyRelease=3),
        "Xlib.XK": types.SimpleNamespace(string_to_keysym=lambda n: 99),
        "Xlib.ext.record": types.SimpleNamespace(AllClients=3, FromServer=0),
        "Xlib.protocol.rq": types.SimpleNamespace(EventField=lambda *_: None),
    }
    for name, mod in mods.items():
        monkeypatch.setitem(sys.modules, name, mod)
    xlib = types.ModuleType("Xlib")
    xlib.display, xlib.X, xlib.XK = mods["Xlib.display"], mods["Xlib.X"], mods["Xlib.XK"]
    ext = types.ModuleType("Xlib.ext")
    ext.record = mods["Xlib.ext.record"]
    proto = types.ModuleType("Xlib.protocol")
    proto.rq = mods["Xlib.protocol.rq"]
    monkeypatch.setitem(sys.modules, "Xlib", xlib)
    monkeypatch.setitem(sys.modules, "Xlib.ext", ext)
    monkeypatch.setitem(sys.modules, "Xlib.protocol", proto)
    monkeypatch.setattr(input_linux, "_ensure_xauthority", lambda: None)
    monkeypatch.setattr(input_linux, "_TALLY_INTERVAL_S", 0.3)
    monkeypatch.setattr(input_linux, "_injected_press_counts", {})

    kbd = input_linux._LinuxXTestKeyboard()
    stop_pressing = threading.Event()

    def _press():
        while not stop_pressing.wait(0.02):
            input_linux._note_injected_press(":9")

    presser = threading.Thread(target=_press, daemon=True)
    presser.start()
    with caplog.at_level("INFO", logger="wingman.input_linux"):
        loop = threading.Thread(target=kbd._listener_loop, args=(":9",), daemon=True)
        loop.start()
        deadline = time.time() + 6.0
        while time.time() < deadline and created.count(":9") < 4:
            time.sleep(0.05)
        stop_pressing.set()
        kbd._stop.set()
        if "active" in _shared:
            _shared["active"]._disabled.set()
        loop.join(timeout=5.0)
    # d_setup is not a recording; each iteration creates exactly one context.
    assert created.count(":9") >= 2, created
    assert any("the listener is deaf; restarting it" in r.getMessage() for r in caplog.records)
    assert not loop.is_alive()


# --- ENTER with NumLock on (operator report, 2026-09-26) ---------------------
# Every ENTER the operator pressed that session reached the :0 listener as
# `'enter' on :0 not delivered (state=0x10)`: NumLock alone. These pin the two
# ways the takeover does work, with NumLock on.

NUMLOCK = 1 << 4


def test_enter_in_the_game_window_takes_over_with_numlock_on():
    il = _lane()
    try:
        assert il.should_deliver_hotkey(":3", "enter", NUMLOCK) is True
        assert il.should_deliver_hotkey(":3", "enter", 0) is True
    finally:
        il.set_injection_display(None)


def test_enter_on_the_desktop_needs_ctrl_alt_and_numlock_does_not_break_it():
    il = _lane()
    try:
        assert il.should_deliver_hotkey(":0", "enter", NUMLOCK) is False
        assert il.should_deliver_hotkey(":0", "enter", CTRL_ALT | NUMLOCK) is True
    finally:
        il.set_injection_display(None)


# --- Anomaly 009: _linux_click reads the pointer back before every click -----

class _FakePointerDisplay:
    """An X display whose pointer only moves when something moves it.

    Reply fields are python-xlib's own (`QueryPointer`: root_x, root_y). `drift`
    models something else moving the pointer after a click (a game recentring
    it); `deaf` models a server that ignores the XTest motion altogether.
    """

    def __init__(self, start=(0, 0), drift=None, deaf=False):
        self.pointer = start
        self.drift = drift
        self.deaf = deaf
        self.events = []
        self.closed = False

    def screen(self):
        display = self

        class _Root:
            def query_pointer(self):
                return mock.Mock(root_x=display.pointer[0], root_y=display.pointer[1])

        return mock.Mock(root=_Root())

    def sync(self):
        pass

    def close(self):
        self.closed = True


def _click_with(monkeypatch, fake, x, y, count):
    from Xlib import X
    import Xlib.display
    import Xlib.ext.xtest

    def fake_input(d, event_type, detail=0, time=0, root=0, x=0, y=0):
        if event_type == X.MotionNotify:
            d.events.append(("move", x, y))
            if not d.deaf:
                d.pointer = (x, y)
        elif event_type == X.ButtonPress:
            d.events.append(("press", d.pointer))
        elif event_type == X.ButtonRelease:
            d.events.append(("release", d.pointer))
            if d.drift is not None:
                d.pointer = d.drift

    monkeypatch.setattr(Xlib.display, "Display", lambda name: fake)
    monkeypatch.setattr(Xlib.ext.xtest, "fake_input", fake_input)
    monkeypatch.setattr(input_linux, "_ensure_xauthority", lambda: None)
    monkeypatch.setattr(input_linux.time, "sleep", lambda _s: None)
    input_linux._linux_click(x, y, count)


def test_click_on_target_moves_once_and_logs_where_each_click_landed(monkeypatch, caplog):
    fake = _FakePointerDisplay(start=(5, 5))
    with caplog.at_level("DEBUG", logger=input_linux.logger.name):
        _click_with(monkeypatch, fake, 939, 1094, 3)

    assert [e for e in fake.events if e[0] == "move"] == [("move", 939, 1094)]
    assert [e for e in fake.events if e[0] == "press"] == [("press", (939, 1094))] * 3
    assert not [r for r in caplog.records if r.levelname == "WARNING"]
    assert "pointer at each click: (939,1094) (939,1094) (939,1094)" in caplog.text
    assert fake.closed


def test_click_reaims_and_reports_a_pointer_that_moved_between_clicks(monkeypatch, caplog):
    """The 2026-09-29 signature this was written for: seven clicks sent, the
    log silent about where they landed. If something drags the pointer away
    after a click, every later click must be re-aimed and the real position
    logged."""
    fake = _FakePointerDisplay(drift=(960, 600))
    with caplog.at_level("DEBUG", logger=input_linux.logger.name):
        _click_with(monkeypatch, fake, 939, 1094, 3)

    assert [e for e in fake.events if e[0] == "press"] == [("press", (939, 1094))] * 3
    warnings = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 2  # clicks 2 and 3; the first was aimed fresh
    assert "pointer at (960, 600), not the (939, 1094)" in warnings[0]
    assert "re-aimed, now (939, 1094)" in warnings[0]
    assert "STILL OFF TARGET" not in warnings[0]


def test_click_says_so_when_the_pointer_will_not_move(monkeypatch, caplog):
    fake = _FakePointerDisplay(start=(100, 200), deaf=True)
    with caplog.at_level("DEBUG", logger=input_linux.logger.name):
        _click_with(monkeypatch, fake, 939, 1094, 2)

    warnings = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 2
    assert all("STILL OFF TARGET" in w and "now (100, 200)" in w for w in warnings)
    # The clicks still go out: a blind click is no worse than before, and the
    # log now says where it landed.
    assert [e for e in fake.events if e[0] == "press"] == [("press", (100, 200))] * 2
    assert "pointer at each click: (100,200) (100,200)" in caplog.text
