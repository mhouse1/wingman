"""CR-018-15: the controller reads and commands perception only through its port.

It used 21 analyzer members at 61 call sites, three of them private. The port
(wingman/perception.py) is now the list; this file fails when the controller or
the hotkey handlers use an analyzer member not on it, or any private one.
"""

import ast
import copy
import pathlib

import pytest
import yaml

from constants import CONFIG_PATH
from tests.perception_fake import PerceptionFake
from wingman.analyzer import GameStateAnalyzer
from wingman.perception import PERCEPTION_MEMBERS, Perception
from wingman.state import GameState

_USERS = {"wingman/controller.py": ("self", "_analyzer"),
          "wingman/hotkeys.py": ("ctrl", "_analyzer")}


def _members_used():
    used = []
    for path, (owner, attr) in _USERS.items():
        tree = ast.parse(pathlib.Path(path).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (isinstance(node, ast.Attribute)
                    and isinstance(node.value, ast.Attribute) and node.value.attr == attr
                    and isinstance(node.value.value, ast.Name) and node.value.value.id == owner):
                used.append((path, node.lineno, node.attr))
    return used


def test_the_controller_uses_only_the_port():
    off_port = [f"{p}:{ln} {name}" for p, ln, name in _members_used()
                if name not in PERCEPTION_MEMBERS]
    assert off_port == [], (
        "an analyzer member used outside the Perception port: add it to "
        f"wingman/perception.py on purpose, or read it another way: {off_port}")


def test_nothing_private_is_reached_into():
    private = [f"{p}:{ln} {name}" for p, ln, name in _members_used() if name.startswith("_")]
    assert private == []


def test_every_port_member_is_used():
    """A member nothing reads is a stale promise the analyzer has to keep."""
    used = {name for _, _, name in _members_used()}
    assert used >= PERCEPTION_MEMBERS, PERCEPTION_MEMBERS - used


def test_the_real_analyzer_provides_the_whole_port():
    missing = [m for m in PERCEPTION_MEMBERS if not hasattr(GameStateAnalyzer, m)]
    assert missing == []


def test_the_shared_fake_provides_the_whole_port():
    """Built from the port, so it cannot quietly lag the real type."""
    fake = PerceptionFake()
    assert isinstance(fake, Perception)
    missing = [m for m in PERCEPTION_MEMBERS if not hasattr(fake, m)]
    assert missing == []


def test_the_fake_fsm_is_the_real_table():
    fake = PerceptionFake()
    assert fake.trigger_event("unknown_to_lobby_detected") is True
    assert fake.game_state == GameState.GAME_LOBBY
    assert fake.trigger_event("eject_complete") is False, "refused, as the real FSM refuses it"
    assert fake.game_state == GameState.GAME_LOBBY
    assert fake.trigger_calls == ["unknown_to_lobby_detected", "eject_complete"]


@pytest.fixture
def analyzer():
    with pathlib.Path(CONFIG_PATH).open("r", encoding="utf-8") as fh:
        a = GameStateAnalyzer(copy.deepcopy(yaml.safe_load(fh)))
    try:
        yield a
    finally:
        a.cleanup()


def test_note_lobby_click_stamps_the_quick_scan_cooldown(analyzer):
    """The 'm' hotkey's click must hold off the quick-scan's own re-click."""
    analyzer._last_lobby_play_click_ts = 0.0
    analyzer.note_lobby_click()
    assert analyzer._last_lobby_play_click_ts > 0.0


def test_note_battle_event_stamps_the_battle_clock(analyzer):
    analyzer._last_battle_event_ts = 0.0
    analyzer.note_battle_event()
    assert analyzer._last_battle_event_ts > 0.0
