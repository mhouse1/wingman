"""CR-018-14: the main loop acts on every FSM transition, in order.

It used to compare this tick's state with the last tick's, which merged two
transitions inside one 1.5 s tick into one. Measured 2026-09-27: 2 of 260 in one
session, one a GAME_BATTLE -> GAME_BATTLE_EJECT -> GAME_BATTLE round trip inside
160 ms during a takeover handback, which the six tick handlers never saw.
"""

import copy
import pathlib

import pytest
import yaml

from constants import CONFIG_PATH
from wingman.analyzer import GameStateAnalyzer
from wingman.state import GameEvent, GameState
from wingman.transition_queue import TransitionQueue


@pytest.fixture
def analyzer():
    with pathlib.Path(CONFIG_PATH).open("r", encoding="utf-8") as fh:
        a = GameStateAnalyzer(copy.deepcopy(yaml.safe_load(fh)))
    try:
        yield a
    finally:
        a.cleanup()


def test_the_first_drain_reports_the_starting_state(analyzer):
    q = TransitionQueue(analyzer)
    assert q.drain() == [(None, GameState.GAME_UNKNOWN)]
    assert q.drain() == []


def test_a_round_trip_inside_one_tick_reaches_the_loop_whole(analyzer):
    """The case the comparison hid: every step, in order, between two drains."""
    q = TransitionQueue(analyzer)
    q.drain()
    assert analyzer.trigger_event("unknown_to_battle_detected")
    assert analyzer.trigger_event("eject_started")
    assert analyzer.trigger_event("eject_complete")
    assert q.drain() == [
        (GameState.GAME_UNKNOWN, GameState.GAME_BATTLE),
        (GameState.GAME_BATTLE, GameState.GAME_BATTLE_EJECT),
        (GameState.GAME_BATTLE_EJECT, GameState.GAME_BATTLE),
    ]


def test_a_refused_trigger_adds_nothing(analyzer):
    q = TransitionQueue(analyzer)
    q.drain()
    assert not analyzer.trigger_event("eject_complete")      # not valid from GAME_UNKNOWN
    assert q.drain() == []


def test_the_starting_entry_is_dropped_when_a_real_event_overtakes_it():
    """Subscribing first means no transition can fall between the two steps; if
    one lands first, the starting entry would repeat its state with no previous."""
    class _Analyzer:
        game_state = GameState.GAME_LOBBY

        def subscribe(self, event, callback, *, name):
            assert event is GameEvent.FSM_TRANSITION
            self.callback = callback

    fake = _Analyzer()
    q = TransitionQueue(fake)
    # A transition that happened before the starting entry was read:
    q._queue = type(q._queue)()
    fake.callback("unknown_to_lobby_detected", "GAME_UNKNOWN", "GAME_LOBBY", 0.0)
    q._queue.put((None, GameState.GAME_LOBBY))
    assert q.drain() == [(GameState.GAME_UNKNOWN, GameState.GAME_LOBBY)]


def test_the_main_loop_uses_the_queue_not_the_comparison():
    src = pathlib.Path("wingman/main.py").read_text(encoding="utf-8")
    assert "for prev_game_state, new_game_state in state_changes.drain():" in src
    assert "last_game_state" not in src
    for handler in ("waiting_fallback", "enemy_presence", "unknown_anomaly",
                    "behavior_tree", "ammo_events", "tracking_hud"):
        assert f"{handler}.on_state_change(new_game_state, prev_game_state)" in src, handler
