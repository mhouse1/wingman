"""Focus guard for key injection (ADR 098).

Wingman types into whatever window has focus. On 2026-08-28 the operator's
message reached them as "tryi auganw" — the stray `i` and `w` were NOSE_UP_KEY
and WINGSWEEP_KEY, injected into their editor while they typed. These tests pin
the guard that closes that, and the three traps the focus probe caught before
the design was settled.
"""

import logging
import unittest.mock as mock
from pathlib import Path

import pytest

from wingman import controller
from wingman.focus_guard import (FOCUS_GAME, FOCUS_OTHER, FOCUS_UNKNOWN,
                                 FocusGuard, game_session_pids)

GAME, EDITOR = 200, 999


def _guard(**cfg):
    cfg.setdefault("enabled", True)
    return FocusGuard(cfg, clock=lambda: _guard.now)


_guard.now = 0.0


def _with_focus(g, wids, owner, session):
    """Patch the X layer so the decision logic can be tested without a display."""
    return (mock.patch.object(g, "_focused_windows", lambda: wids),
            mock.patch.object(g, "_owner_pid", lambda w: owner.get(w)),
            mock.patch.object(g, "_session_pids", lambda: session))


# --- V1: who may inject ------------------------------------------------------

def test_a_window_owned_by_the_game_session_may_inject():
    g = _guard()
    a, b, c = _with_focus(g, ["0x1"], {"0x1": GAME}, {GAME})
    with a, b, c:
        assert g.focus_state() == FOCUS_GAME
        assert g.may_inject() is True


def test_the_editor_is_suppressed():
    """The hazard itself: focus on VS Code, wingman still pressing keys."""
    g = _guard()
    a, b, c = _with_focus(g, ["0x1"], {"0x1": EDITOR}, {GAME})
    with a, b, c:
        assert g.focus_state() == FOCUS_OTHER
        assert g.may_inject() is False
    assert g.suppressed_total == 1


def test_a_window_titled_like_the_game_is_still_suppressed():
    """Probe trap 2: a VS Code window titled "Metalstorm config GitHub... -
    wingman - Visual Studio Code" satisfies a title substring test. Identity
    comes from the owning process, so the title is irrelevant here."""
    g = _guard()
    a, b, c = _with_focus(g, ["0x1"], {"0x1": EDITOR}, {GAME})
    with a, b, c:
        assert g.may_inject() is False


def test_either_signal_naming_the_game_is_enough():
    """ADR 098 D3. The two signals agreed on all 231 probe samples, so requiring
    both would only add ways to fail."""
    g = _guard()
    a, b, c = _with_focus(g, ["0xA", "0xB"], {"0xA": EDITOR, "0xB": GAME}, {GAME})
    with a, b, c:
        assert g.focus_state() == FOCUS_GAME


def test_an_unidentifiable_window_does_not_pass_as_the_game():
    g = _guard()
    a, b, c = _with_focus(g, ["0x1"], {"0x1": None}, {GAME})
    with a, b, c:
        assert g.focus_state() == FOCUS_OTHER


# --- V2/V3: the session definition -------------------------------------------

def test_the_session_includes_siblings_not_just_the_game_binary():
    """Probe trap 3, and the dangerous one. The WM-managed window is "Wine
    Desktop", owned by explorer.exe (3241639) — a SIBLING of Metalstorm.exe
    (3241663) under the Proton launcher. Matching only the binary suppresses
    every keypress while the game is fully focused: it fails closed and looks
    like nothing is wrong."""
    tree = {200: 100, 201: 100, 202: 100, 100: 1, 999: 1}   # 201 = explorer.exe
    with mock.patch("wingman.focus_guard.find_game_pids", lambda _n=None: {200}), \
         mock.patch("wingman.focus_guard._ppid_of", lambda pid: tree.get(pid)), \
         mock.patch("wingman.focus_guard.os.listdir", lambda _p: [str(k) for k in tree]):
        session = game_session_pids()
    assert {200, 201, 202} <= session
    assert 999 not in session, "an unrelated app was pulled into the game session"


def test_no_session_when_the_game_is_not_running():
    assert game_session_pids("definitely-not-a-process") == set()


def test_the_session_walk_terminates_on_a_cycle():
    cyclic = {5: 6, 6: 5}
    with mock.patch("wingman.focus_guard.find_game_pids", lambda _n=None: {5}), \
         mock.patch("wingman.focus_guard._ppid_of", lambda pid: cyclic.get(pid)), \
         mock.patch("wingman.focus_guard.os.listdir", lambda _p: ["5", "6"]):
        assert isinstance(game_session_pids(), set)


def test_game_not_running_is_unknown_not_suppression():
    """Nothing to protect and nothing to compare against."""
    g = _guard()
    with mock.patch.object(g, "_session_pids", lambda: set()):
        assert g.focus_state() == FOCUS_UNKNOWN


# --- V4: what an unresolvable check does -------------------------------------

def test_unknown_injects_by_default(caplog):
    """ADR 098 D4: a guard that suppresses when it cannot tell converts a
    transient X hiccup into a silently dead session."""
    g = _guard()
    with mock.patch.object(g, "_probe", lambda: FOCUS_UNKNOWN), caplog.at_level(logging.WARNING):
        assert g.may_inject() is True
    assert g.unknown_total == 1
    assert g.suppressed_total == 0
    assert any("unresolved" in r.message for r in caplog.records)


def test_unknown_can_be_configured_to_suppress(caplog):
    g = _guard(on_unknown="suppress")
    with mock.patch.object(g, "_probe", lambda: FOCUS_UNKNOWN), caplog.at_level(logging.WARNING):
        assert g.may_inject() is False
    assert g.suppressed_total == 1


def test_a_raising_probe_never_escapes():
    def boom():
        raise RuntimeError("X died")
    g = _guard()
    with mock.patch.object(g, "_probe", boom):
        assert g.focus_state() == FOCUS_UNKNOWN
        assert g.may_inject() is True


# --- V5/V6: wiring and caching ----------------------------------------------

def test_disabled_guard_is_a_no_op():
    """ADR 098 D6: off by default until a full session proves it."""
    g = FocusGuard({})
    assert g.enabled is False
    assert g.may_inject() is True
    assert g.focus_state() == FOCUS_GAME


def test_the_probe_is_cached_within_its_ttl():
    """ADR 098 D5: a burst of presses inside one tick shares one answer."""
    g = _guard(ttl_s=1.0)
    calls = {"n": 0}

    def counted():
        calls["n"] += 1
        return FOCUS_GAME

    _guard.now = 100.0
    with mock.patch.object(g, "_probe", counted):
        for _ in range(20):
            g.may_inject()
        assert calls["n"] == 1
        _guard.now = 101.5                    # past the ttl
        g.may_inject()
        assert calls["n"] == 2


def test_controller_hook_allows_injection_when_no_guard_is_installed():
    controller.set_focus_guard(None)
    try:
        assert controller._may_inject("key") is True
    finally:
        controller.set_focus_guard(None)


def test_controller_hook_survives_a_broken_guard():
    """A guard that throws must never stop the tick loop."""
    class Broken:
        def may_inject(self, what="key"):
            raise RuntimeError("nope")
    controller.set_focus_guard(Broken())
    try:
        assert controller._may_inject("key") is True
    finally:
        controller.set_focus_guard(None)


def test_controller_hook_suppresses_when_the_guard_says_so():
    class Deny:
        def may_inject(self, what="key"):
            return False
    controller.set_focus_guard(Deny())
    try:
        assert controller._may_inject("key") is False
    finally:
        controller.set_focus_guard(None)


# --- releases are never gated ------------------------------------------------

def test_the_guard_gates_injection_not_control_flow():
    """First implementation used `if not _may_inject(...): return` at the call
    sites. That collapsed a two-second hold into zero and — at the disengage-roll
    site — skipped the stop_search_and_destroy_loop() cleanup for a loop already
    started four lines above. Suppression must cost the same time and leave the
    same state as a real press; only the key must not reach the game.

    The invariant: the negated form belongs solely to _press_key, whose `return
    False` is a value, not a skipped block. Everywhere else the guard may only
    wrap an injection call.
    """
    src = Path(controller.__file__.replace(".pyc", ".py"))
    lines = src.read_text().splitlines()
    inside_helper = False
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("def _press_key("):
            inside_helper = True
            continue
        if inside_helper and stripped.startswith("def "):
            inside_helper = False
        if "if not _may_inject(" in line and not inside_helper:
            raise AssertionError(
                f"guard skips a block instead of gating injection, line {i+1}: {stripped}")


def test_a_suppressed_press_reports_that_it_did_not_press():
    """_press_key's return value is how a caller could tell, if it ever needs to."""
    class Deny:
        def may_inject(self, what="key"):
            return False

    controller.set_focus_guard(Deny())
    try:
        with mock.patch.object(controller, "keyboard_module") as kb:
            assert controller._press_key("f") is False
            kb.press.assert_not_called()
    finally:
        controller.set_focus_guard(None)


def test_an_allowed_press_actually_presses():
    controller.set_focus_guard(None)
    with mock.patch.object(controller, "keyboard_module") as kb:
        assert controller._press_key("f") is True
        kb.press.assert_called_once_with("f")


def test_only_presses_are_gated_never_releases():
    """A gated release leaves the key down in the X server for the rest of the
    session — the stuck-key incident the controller header warns about. Every
    guard call site must therefore sit on a press or a click path."""
    src = Path(controller.__file__.replace(".pyc", ".py"))
    lines = src.read_text().splitlines()
    for i, line in enumerate(lines):
        if '_may_inject(' not in line or line.strip().startswith(("#", "def ", "guard")):
            continue
        window = "\n".join(lines[i:i + 8])
        assert "release(" not in window, f"guard sits on a release path at line {i+1}"


@pytest.mark.parametrize("what", ["key", "click"])
def test_both_injection_kinds_are_reported(what, caplog):
    g = _guard()
    a, b, c = _with_focus(g, ["0x1"], {"0x1": EDITOR}, {GAME})
    with a, b, c, caplog.at_level(logging.WARNING):
        assert g.may_inject(what) is False
    assert any(what in r.message for r in caplog.records)


# --- ADR 099: the guard must watch the display injection targets -------------

def test_guard_follows_injection_to_the_nested_display():
    """The 2026-08-29 regression. The guard resolves its display from config or
    DISPLAY; with the game nested and DISPLAY still on the operator's screen it
    found no game window, concluded "not the game", and suppressed every click —
    10 suppressions and a 154 s stall in GAME_WAITING. The guard had silently
    disabled the thing it exists to protect."""
    from wingman.focus_guard import config_for_display
    assert config_for_display({"enabled": True}, ":3")["display"] == ":3"


def test_an_explicit_guard_display_still_wins():
    from wingman.focus_guard import config_for_display
    assert config_for_display({"display": ":9"}, ":3")["display"] == ":9"


def test_the_on_screen_lane_leaves_the_guard_display_alone():
    from wingman.focus_guard import config_for_display
    assert "display" not in config_for_display({"enabled": True}, None)


def test_config_for_display_does_not_mutate_the_caller_config():
    from wingman.focus_guard import config_for_display
    original = {"enabled": True}
    config_for_display(original, ":3")
    assert original == {"enabled": True}
