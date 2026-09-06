"""Where did a manual takeover come from? (ADR 099 hotkey observation)

`should_deliver_hotkey` decides from (display, key, modifier state) and, until
now, recorded none of it. So a takeover raised because the operator focused the
nested game window and pressed ENTER, and one raised from their ordinary desktop
with ctrl+alt held, produced byte-identical log lines:

    Controller: maneuver key 'enter' pressed - entering GAME_BATTLE_MANUAL

Two of those landed mid-eject on 2026-09-05 and could not be attributed to
either source afterwards. Diagnosing "wingman stopped flying when I alt-tabbed"
needs that attribution, and reconstructing it from nearby lines does not survive
a log rotation.

These tests are on the attribution itself. The takeover DECISION is unchanged
and is covered by tests/test_controller_no_keyboard.py.
"""

import logging

import pytest

from wingman import controller as controller_module
from wingman import input_linux as il
from wingman.analyzer import GameState
from wingman.controller import Controller, describe_key_source
from wingman.input_linux import _XKeyEvent
from wingman.keybindings import MANUAL_TAKEOVER_KEY

from tests.test_controller_no_keyboard import _AnalyzerStub, _load_config


@pytest.fixture
def nested_display(monkeypatch):
    """The live topology: injection on :3, operator on :0."""
    monkeypatch.setattr(il, "_injection_display", ":3")
    monkeypatch.setenv("DISPLAY", ":0")
    return ":3"


# --- the event carries its origin -------------------------------------------

def test_the_event_carries_the_display_it_was_observed_on():
    """The real type, not a stand-in: a fake mirroring the caller's assumptions
    would have reproduced the missing field rather than caught it."""
    ev = _XKeyEvent(name="enter", is_injected=False, display=":3", state=0)
    assert ev.display == ":3"
    assert ev.state == 0


def test_the_event_still_matches_the_keyboard_module_shape():
    """`_XKeyEvent` stands in for keyboard.KeyboardEvent; the fields the rest of
    the code reads must survive the addition."""
    ev = _XKeyEvent(name="enter", is_injected=True, display=":0", state=12)
    assert (ev.name, ev.is_injected, ev.event_type) == ("enter", True, "down")


def test_display_and_state_default_so_existing_callers_are_unaffected():
    ev = _XKeyEvent(name="enter", is_injected=False)
    assert ev.display is None and ev.state == 0


# --- what the origin means ---------------------------------------------------

def test_the_nested_display_means_the_operator_focused_the_game(nested_display):
    out = describe_key_source(":3", 0)
    assert ":3" in out and "nested" in out


def test_the_operator_desktop_is_named_as_such(nested_display):
    out = describe_key_source(":0", 0)
    assert ":0" in out and "operator desktop" in out


def test_the_ctrl_alt_modifier_is_decoded(nested_display):
    """ADR 099 requires ctrl+alt for hotkeys on the operator's display. Whether
    they were actually held is the evidence that a delivery was legitimate."""
    assert "mods=ctrl+alt" in describe_key_source(":0", il._OPERATOR_MOD_MASK)


def test_no_modifiers_is_stated_rather_than_omitted(nested_display):
    """Silence must not be readable as 'modifiers unknown'."""
    assert "mods=none" in describe_key_source(":0", 0)


def test_an_unknown_source_says_unknown(nested_display):
    """The Windows `keyboard` fallback delivers no display. Reporting that
    honestly beats defaulting to a display it did not come from."""
    assert describe_key_source(None) == "source=unknown"


def test_an_unrecognised_display_is_flagged_not_silently_accepted(nested_display):
    assert "unrecognised" in describe_key_source(":9", 0)


# --- the takeover log carries it --------------------------------------------

def _battle_controller(monkeypatch):
    monkeypatch.setattr(controller_module, "keyboard_module", None)
    cfg = _load_config()
    region = (0, 0, cfg["region"]["width"], cfg["region"]["height"])
    return Controller(region, analyzer=_AnalyzerStub(GameState.GAME_BATTLE))


def test_the_takeover_log_names_the_source(monkeypatch, caplog, nested_display):
    ctrl = _battle_controller(monkeypatch)
    ctrl._mission_lock.acquire(blocking=False)
    try:
        with caplog.at_level(logging.INFO, logger="wingman.controller"):
            assert ctrl._handle_maneuver_key_press(
                MANUAL_TAKEOVER_KEY, is_injected=False, display=":3", state=0) is True
        line = [r.getMessage() for r in caplog.records if "manual takeover" in r.getMessage()]
        assert line, "takeover was not logged at INFO"
        assert ":3" in line[0] and "nested" in line[0]
    finally:
        if ctrl._mission_lock.locked():
            ctrl._mission_lock.release()


def test_a_caller_passing_no_source_still_takes_over(monkeypatch, nested_display):
    """Attribution is instrumentation. It must never gate SAF-001's takeover —
    a source we cannot name is still the operator taking the aircraft."""
    ctrl = _battle_controller(monkeypatch)
    ctrl._mission_lock.acquire(blocking=False)
    try:
        assert ctrl._handle_maneuver_key_press("j", is_injected=False) is True
    finally:
        if ctrl._mission_lock.locked():
            ctrl._mission_lock.release()
