"""ADR 102: walk GAME_STARTING back to GAME_LOBBY when PLAY is still on screen.

2026-09-01 06:47:53. PLAY was clicked, a CANCEL read carried the FSM
GAME_LOBBY to GAME_WAITING to GAME_STARTING — and the match never began. The
game sat at the lobby with PLAY visible while wingman pressed 'u' every five
seconds and ran 94 health probes, every one of them "no digits", for 141.7 s
until the starting timeout fired:

    06:50:26  GAME_STARTING health probe summary: 94 attempts over 141.7s
              — NO raw read at any point
    06:50:26  FSM: GAME_STARTING -> GAME_STARTING_STALLED

The evidence that the state was wrong was on screen the whole time. Nothing
looked for it, because the quick-scan skipped GAME_STARTING entirely.
"""

from wingman.analyzer import (
    GameState,
    LOBBY_RECHECK_STATES,
    POPUP_DISMISS_STATES,
    STARTING_PLAY_CONFIRM_READS,
    _FSM_TRANSITIONS,
)


def _transitions(trigger):
    return [t for t in _FSM_TRANSITIONS if t["trigger"] == trigger]


# --- the FSM edge -------------------------------------------------------------

def test_there_is_a_way_back_to_the_lobby_from_starting():
    """Before this ADR the only exits from GAME_STARTING were 'Good Luck' and
    the 150 s timeout — nothing acted on the lobby still being up."""
    edges = _transitions("starting_play_visible")
    assert len(edges) == 1
    assert edges[0]["source"] == "GAME_STARTING"
    assert edges[0]["dest"] == "GAME_LOBBY"


def test_the_walk_back_is_not_a_wildcard():
    """manual_reset already goes anywhere-to-lobby. A second wildcard would let
    a stray PLAY read reset the FSM from GAME_BATTLE, which is the aircraft in
    flight."""
    assert _transitions("starting_play_visible")[0]["source"] != "*"


# --- scope --------------------------------------------------------------------

def test_only_game_starting_rechecks_the_lobby():
    assert LOBBY_RECHECK_STATES == (GameState.GAME_STARTING,)


def test_the_recheck_does_not_grant_popup_dismissal():
    """The two sets are deliberately separate. Adding GAME_STARTING to the
    popup set would let the scanner click dialogs during a match that is
    genuinely starting; the recheck only READS one crop."""
    assert GameState.GAME_STARTING not in POPUP_DISMISS_STATES
    for st in LOBBY_RECHECK_STATES:
        assert st not in POPUP_DISMISS_STATES, st


def test_battle_states_are_never_rechecked():
    """A PLAY misread mid-battle must not be able to reset the FSM."""
    from wingman.analyzer import BATTLE_STATES
    for st in BATTLE_STATES:
        assert st not in LOBBY_RECHECK_STATES, st


# --- debounce -----------------------------------------------------------------

def test_a_single_read_cannot_abort_a_starting_match():
    """At a ~1 s scan cadence this is ~3 s of PLAY continuously visible. One
    stray read aborting a real match start would be a worse failure than the
    150 s stall it replaces."""
    assert STARTING_PLAY_CONFIRM_READS >= 2


def test_the_debounce_is_far_cheaper_than_the_timeout_it_replaces():
    """The whole point is speed: the stall it fixes cost 141.7 s."""
    assert STARTING_PLAY_CONFIRM_READS <= 5
