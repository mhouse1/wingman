"""CR-018-14: the FSM table, checked cell by cell against an independent copy.

EXPECTED below is the intended FSM written out by hand, one trigger per row. Every
(state, trigger) pair in GameState x triggers is exercised on a real `transitions`
Machine built from wingman/state.py: a listed pair must land on its destination,
and every other pair must raise MachineError. Changing the table therefore means
changing this file too, on purpose.
"""

import collections
import pathlib

import pytest
from transitions import Machine, MachineError

from wingman.fsm_diagram import render_doc_block
from wingman.state import FSM_TRANSITIONS, GameState

_OVERRIDE_TO_BATTLE = ("GAME_UNKNOWN", "GAME_END_B", "GAME_LOBBY", "GAME_WAITING",
                       "GAME_STARTING", "GAME_STARTING_STALLED", "GAME_BATTLE_MANUAL",
                       "GAME_BATTLE_EJECT")
_OVERRIDE_TO_LOBBY = ("GAME_UNKNOWN", "GAME_BATTLE", "GAME_END_B", "GAME_WAITING",
                      "GAME_STARTING", "GAME_STARTING_STALLED", "GAME_BATTLE_MANUAL",
                      "GAME_BATTLE_EJECT")

EXPECTED = {
    "unknown_to_end_detected": {"GAME_UNKNOWN": "GAME_END_B"},
    "unknown_to_lobby_detected": {"GAME_UNKNOWN": "GAME_LOBBY"},
    "unknown_to_battle_detected": {"GAME_UNKNOWN": "GAME_BATTLE"},
    "play_clicked": {"GAME_LOBBY": "GAME_WAITING"},
    "cancel_detected": {"GAME_LOBBY": "GAME_STARTING", "GAME_WAITING": "GAME_STARTING"},
    "waiting_timeout": {"GAME_WAITING": "GAME_LOBBY"},
    "good_luck_detected": {"GAME_STARTING": "GAME_BATTLE"},
    "starting_timeout": {"GAME_STARTING": "GAME_STARTING_STALLED"},
    "starting_play_visible": {"GAME_STARTING": "GAME_LOBBY"},
    "starting_stalled_reclassify": {"GAME_STARTING_STALLED": "GAME_UNKNOWN"},
    "starting_recovery": {"GAME_STARTING_STALLED": "GAME_STARTING"},
    "starting_give_up": {"GAME_STARTING_STALLED": "GAME_LOBBY"},
    "click_to_detected": {"GAME_BATTLE": "GAME_END_B", "GAME_BATTLE_MANUAL": "GAME_END_B",
                          "GAME_BATTLE_EJECT": "GAME_END_B"},
    "manual_takeover": {"GAME_BATTLE": "GAME_BATTLE_MANUAL",
                        "GAME_BATTLE_EJECT": "GAME_BATTLE_MANUAL"},
    "respawn_reset": {"GAME_BATTLE_MANUAL": "GAME_BATTLE"},
    "manual_release": {"GAME_BATTLE_MANUAL": "GAME_BATTLE"},
    "eject_started": {"GAME_BATTLE": "GAME_BATTLE_EJECT"},
    "eject_complete": {"GAME_BATTLE_EJECT": "GAME_BATTLE"},
    "manual_force_battle": {s: "GAME_BATTLE" for s in _OVERRIDE_TO_BATTLE},
    "manual_reset": {s: "GAME_LOBBY" for s in _OVERRIDE_TO_LOBBY},
    "continue_clicked": {"GAME_END_B": "GAME_LOBBY", "GAME_BATTLE_MANUAL": "GAME_LOBBY"},
    "respawn_detected": {"GAME_END_B": "GAME_BATTLE"},
}

_STATES = [s.name for s in GameState]


def _fire(state, trigger):
    model = type("Model", (), {})()
    Machine(model=model, states=_STATES, transitions=FSM_TRANSITIONS, initial=state,
            ignore_invalid_triggers=False)
    getattr(model, trigger)()
    return model.state


def test_the_table_has_exactly_the_expected_triggers():
    assert {t["trigger"] for t in FSM_TRANSITIONS} == set(EXPECTED)


def test_every_expected_source_is_a_real_state():
    for trigger, row in EXPECTED.items():
        assert set(row) <= set(_STATES), trigger


@pytest.mark.parametrize("state", _STATES)
def test_every_trigger_from_this_state(state):
    for trigger, row in EXPECTED.items():
        if state in row:
            assert _fire(state, trigger) == row[state], f"{state} --{trigger}-->"
        else:
            with pytest.raises(MachineError):
                _fire(state, trigger)


def test_no_wildcard_and_no_self_transition():
    """A wildcard made each override a self-transition that re-ran the entry
    hook, and would accept any future state as a source (CR-018-14)."""
    for t in FSM_TRANSITIONS:
        sources = t["source"] if isinstance(t["source"], list) else [t["source"]]
        assert "*" not in sources, t["trigger"]
        assert t["dest"] not in sources, t["trigger"]


def _graph():
    edges = collections.defaultdict(set)
    for row in EXPECTED.values():
        for src, dst in row.items():
            edges[src].add(dst)
    return edges


def _reachable(start, edges):
    seen, todo = {start}, [start]
    while todo:
        for nxt in edges[todo.pop()]:
            if nxt not in seen:
                seen.add(nxt)
                todo.append(nxt)
    return seen


def test_every_state_is_reachable_from_startup():
    assert _reachable("GAME_UNKNOWN", _graph()) == set(_STATES)


def test_the_lobby_is_reachable_from_every_state():
    """Every state has a way back to where a new round starts."""
    edges = _graph()
    for state in _STATES:
        assert "GAME_LOBBY" in _reachable(state, edges), state


def test_the_architecture_diagram_is_the_generated_one():
    """Regenerate with `make fsm`."""
    doc = pathlib.Path("docs/architecture.md").read_text(encoding="utf-8")
    assert render_doc_block() in doc, "docs/architecture.md FSM diagram is stale: run make fsm"
