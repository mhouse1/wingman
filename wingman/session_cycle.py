"""End a live session after a set number of rounds, so the game restarts fresh (ADR 202).

MetalStorm's resident memory grows for as long as it runs: 513 to 3,870 MB over
the 7.1 h session of 2026-11-22 21:10, +304 MB/h, and wingman's own grows too
(+89 MB/h). Nothing wingman does releases the game's memory, so the session is
kept short instead: after ``rounds`` finished rounds wingman asks for the ADR 094
finish-round stop, which exits at the lobby and closes MetalStorm and the nested
display. ``main`` then waits ``wait_s`` and exits with :data:`EXIT_CODE`, and
``make r`` (so r1 to r3) relaunches the game and wingman on that code.

A round counts once it has reached a battle state and then left for the end
screen or the lobby; GAME_UNKNOWN in between (ADR 195) does not end it. The
operator keeps the last word: a ``z`` pressed while the cycle's stop is pending
cancels it, as it cancels any finish-round stop, and turns the cycle off for the
rest of the session; a ``z`` of the operator's own ends wingman with exit 0, so
the make loop ends too.

Standard library only, like ``state.py``.
"""

from __future__ import annotations

import logging

from .state import BATTLE_STATES, GameState

logger = logging.getLogger(__name__)

# EX_TEMPFAIL: "try again". The make loop relaunches on this code and no other.
EXIT_CODE = 75

_ROUND_ENDS = (GameState.GAME_END_B, GameState.GAME_LOBBY)


class SessionCycle:
    """Counts finished rounds and says when the session's last one has ended."""

    def __init__(self, enabled: bool, rounds: int, wait_s: float):
        self.enabled = bool(enabled) and int(rounds) > 0
        self.rounds = int(rounds)
        self.wait_s = float(wait_s)
        self.finished = 0
        self.pending = False        # the cycle asked for the finish-round stop
        self._in_round = False

    @classmethod
    def from_config(cls, section, live: bool) -> "SessionCycle":
        """The cycle as the config says, in a live run only.

        Replay and capture runs read the shipped config and must not stop on a
        round count.
        """
        from .config_schema import schema_default
        section = section or {}
        enabled = bool(section.get("enabled", schema_default("session_cycle.enabled")))
        return cls(
            enabled=enabled and live,
            rounds=int(section.get("rounds", schema_default("session_cycle.rounds"))),
            wait_s=float(section.get("wait_s", schema_default("session_cycle.wait_s"))),
        )

    def note_state(self, new_state: GameState, stop_requested: bool) -> bool:
        """Feed one FSM transition. True when this round end is the session's last.

        ``stop_requested`` is whether a finish-round stop is already pending:
        the operator's own ``z`` is not the cycle's, and must not become one.
        """
        if new_state in BATTLE_STATES:
            self._in_round = True
            return False
        if new_state not in _ROUND_ENDS or not self._in_round:
            return False
        self._in_round = False
        self.finished += 1
        if (not self.enabled or self.pending or stop_requested
                or self.finished < self.rounds):
            return False
        self.pending = True
        return True

    def note_stop_flag(self, stop_requested: bool) -> bool:
        """Watch the finish-round flag. True once, when the operator cancels the cycle's stop."""
        if self.pending and not stop_requested:
            self.pending = False
            self.enabled = False
            return True
        return False

    def should_restart(self, finish_round_exit: bool) -> bool:
        """True when the session ended on the cycle's own stop."""
        return self.pending and finish_round_exit
