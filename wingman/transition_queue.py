"""Every FSM transition, in order, for the main loop to act on. CR-018-14.

The main loop used to detect state changes by comparing the state it read each
tick with the one it read the tick before. That coalesced transitions: two in one
1.5 s tick reached the six tick handlers as one. Measured on 2026-09-27, 2 of 260
transitions in one session were hidden that way, one of them a GAME_BATTLE ->
GAME_BATTLE_EJECT -> GAME_BATTLE round trip inside 160 ms during a manual-takeover
handback.

Every FSM state change goes through `GameStateAnalyzer._trigger`, which emits
FSM_TRANSITION for each real change (no code sets the state or calls a trigger
method directly), so subscribing to that event sees them all. The queue is drained
on the main thread, so the handlers still run on one thread, in order.
"""

import queue

from .state import GameEvent, GameState


class TransitionQueue:
    """`drain()` returns (previous, new) state pairs, oldest first.

    The first drain also yields (None, <state at construction>), which is what
    the tick-to-tick comparison produced on its first tick: the handlers learn
    the starting state with no previous one.
    """

    def __init__(self, analyzer, *, name: str = "main_loop_state_changes"):
        self._queue: "queue.SimpleQueue" = queue.SimpleQueue()
        self._last: "GameState | None" = None
        analyzer.subscribe(GameEvent.FSM_TRANSITION, self._on_transition, name=name)
        # After subscribing, so no transition can fall between the two. If one
        # lands first, drain() drops this entry as already covered.
        self._queue.put((None, analyzer.game_state))

    def _on_transition(self, _trigger, prev_name, next_name, _ts) -> None:
        self._queue.put((GameState[prev_name], GameState[next_name]))

    def drain(self) -> "list[tuple[GameState | None, GameState]]":
        changes = []
        while True:
            try:
                prev, new = self._queue.get_nowait()
            except queue.Empty:
                return changes
            if prev is None and new == self._last:
                continue                  # the initial entry, overtaken by a real event
            changes.append((prev, new))
            self._last = new
