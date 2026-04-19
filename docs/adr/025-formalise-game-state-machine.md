# ADR 025 — Formalise the Game State Machine

| Status   | Date       | Wingman Version |
|----------|------------|-----------------|
| Accepted | 2026-04-19 | 1.6.3           |

## Context

The game loop has six named states defined in `GameState` (enum, `analyzer.py`):

```
GAME_LOBBY → GAME_WAITING → GAME_STARTING → GAME_BATTLE → GAME_END_B → GAME_LOBBY …
                                          ↘ GAME_STARTING_STALLED
```

These states are represented today as **five separate boolean flags** on `GameStateAnalyzer`:

```python
self._game_lobby          = True
self._game_waiting        = False
self._game_starting       = False
self._game_starting_stalled = False
self._game_end_b          = False
```

`game_state` computes the current state by reading those flags in priority order. Any code that wants to transition state sets the relevant flags directly — there is no central transition method.

A grep across the three runtime files finds **46 flag mutation sites**:

| File | Mutations |
|------|-----------|
| `analyzer.py` | 13 |
| `controller.py` | 22 |
| `main.py` | 11 |

### Problems this causes

**1. Inconsistent intermediate states.** Each transition clears several flags and sets one — done with multiple assignment statements, not atomically. A background OCR thread reading `game_state` between those assignments can observe a state that matches no valid enum value (all flags False → falls through to `GAME_BATTLE` default, silently).

**2. No valid-transition enforcement.** Any code can write any combination of flags. Invalid transitions (e.g. `GAME_LOBBY → GAME_BATTLE` directly) are not prevented or logged. The only detection is downstream misbehaviour.

**3. No entry/exit hooks.** Side effects that should accompany a transition (e.g. `_start_game_starting_loop()` on entry to `GAME_STARTING`) are called manually at each transition site. If a new path to `GAME_STARTING` is added, the developer must remember to call the hook. Three such hooks are currently missing from some paths.

**4. Health override masks state.** The `game_state` property previously returned `GAME_BATTLE` whenever `_health >= 1`, overriding any explicit flag — including `_game_lobby = True`. This caused OCR to keep scanning during the entire lobby phase (fixed in v1.6.3, ADR 024 context). The fix was to reorder the property. The root cause is that `game_state` is computed from multiple mutable flags rather than read from a single authoritative field.

**5. BT dependency.** ADR 024 requires `game_state` to be trustworthy for the py-trees behavior tree root condition (`Idle — if game_state != GAME_BATTLE`). A scattered flag system is an unreliable foundation for that check.

---

## Decision

Replace the five boolean flags with a **single `_state: GameState` field** and a `transition(new_state)` method that:

1. Validates the transition against a static table of allowed edges
2. Calls exit hooks for the old state
3. Sets `_state` atomically
4. Calls entry hooks for the new state
5. Logs the transition

All 46 external flag mutations become a single `analyzer.transition(GameState.XXX)` call.

---

## Design

### Valid transition table

```python
_VALID_TRANSITIONS: frozenset[tuple[GameState, GameState]] = frozenset({
    (GAME_LOBBY,             GAME_WAITING),          # PLAY clicked
    (GAME_LOBBY,             GAME_STARTING),         # CANCEL detected directly in lobby
    (GAME_WAITING,           GAME_STARTING),         # CANCEL confirmed
    (GAME_WAITING,           GAME_LOBBY),            # 180s timeout
    (GAME_STARTING,          GAME_BATTLE),           # Good Luck + mission start
    (GAME_STARTING,          GAME_STARTING_STALLED), # timeout without Good Luck
    (GAME_STARTING_STALLED,  GAME_STARTING),         # recovery attempt
    (GAME_STARTING_STALLED,  GAME_LOBBY),            # give up
    (GAME_BATTLE,            GAME_END_B),            # Click to Continue detected
    (GAME_BATTLE,            GAME_LOBBY),            # manual reset (End key)
    (GAME_END_B,             GAME_LOBBY),            # continue clicked
    (GAME_END_B,             GAME_BATTLE),           # respawn/incoming during end screen
})
```

### `transition()` method

```python
def transition(self, new_state: GameState, *, force: bool = False) -> bool:
    with self._state_lock:
        old_state = self._state
        if old_state == new_state:
            return True
        if not force and (old_state, new_state) not in _VALID_TRANSITIONS:
            logger.warning(
                "FSM: invalid transition %s → %s ignored",
                old_state.name, new_state.name)
            return False
        self._state = new_state

    self._on_exit(old_state, new_state)
    self._on_enter(new_state, old_state)
    logger.info("\033[96m🎮 Game state: %s → %s\033[0m", old_state.name, new_state.name)
    return True
```

`force=True` is used only by the manual hotkey override (pressing `u` to force `GAME_BATTLE` mid-lobby). All normal transitions use the validated path.

### Entry hooks

| Entering | Hook |
|----------|------|
| `GAME_LOBBY` | `cancel_mission()` callback; reset `_lobby_play_not_visible_since` |
| `GAME_WAITING` | reset `_waiting_since` timestamp (main loop timer) |
| `GAME_STARTING` | call `_start_game_starting_loop()` |
| `GAME_BATTLE` | reset `_health_no_digits_since`; fire `alive_event` if health known alive |
| `GAME_END_B` | nothing (click-to thread handles its own logic) |
| `GAME_STARTING_STALLED` | log warning; stall recovery timer starts |

Hooks that require `Controller` are passed in as callbacks at construction time — `GameStateAnalyzer` does not import `Controller`.

### `game_state` property

Becomes a one-liner:

```python
@property
def game_state(self) -> GameState:
    return self._state
```

The health override (moved below explicit flags in v1.6.3) is **removed entirely** — `_state` is authoritative. Health remains a sub-state within `GAME_BATTLE` only, tracked by `_game_battle_alive`.

### Thread safety

All five boolean flags were written individually without a lock (a latent race). The single `_state` field is read/written under `_state_lock` (a new `threading.Lock`). The `game_state` property acquires the lock for the read. OCR threads calling `game_state` no longer risk observing a half-written multi-flag transition.

---

## Migration

### Phase 1 — Add `transition()`, keep flags

Add `_state`, `_state_lock`, and `transition()`. Entry/exit hooks registered as no-ops. `game_state` reads `_state`. The five boolean flags remain but are set by `transition()` as aliases (for backward compatibility with external reads during migration).

### Phase 2 — Migrate call sites

Replace each of the 46 external flag mutations with `analyzer.transition(GameState.XXX)`. Prioritise `controller.py` (22 sites) then `main.py` (11 sites) then `analyzer.py` internal sites (13 sites, mostly already in transition context).

### Phase 3 — Remove boolean flags

Delete `_game_lobby`, `_game_waiting`, `_game_starting`, `_game_starting_stalled`, `_game_end_b`. Any remaining external reads of these flags (there are a few guard checks in `controller.py`) switch to `analyzer.game_state == GameState.XXX`.

### Phase 4 — Add entry/exit hooks

Wire real hook callbacks: `on_enter_game_starting=ctrl._start_game_starting_loop`, `on_enter_game_lobby=ctrl.cancel_mission`, etc.

---

## Relation to ADR 024 (py-trees BT)

This FSM refactor is a **prerequisite** for the behavior tree:

- BT root reads `game_state` every tick. With `_state` as a single atomic field protected by a lock, the read is safe and authoritative.
- `transition()` entry hooks replace the manual side-effect calls scattered across files — the same pattern the BT uses to respond to state changes.
- Once the FSM is formalised, the BT's `AnalyzerSnapshot` can include `game_state` without risk of it reflecting a half-written intermediate.

**Sequencing:** Implement ADR 025 (FSM) first, then ADR 024 (BT).

---

## Alternatives considered

### Keep booleans, add a `transition()` wrapper that sets them

Reduces mutation sites to one place but keeps the multi-flag representation and the intermediate-state race. Also keeps the `game_state` priority computation, which is where the health-override bug lived.

**Rejected:** Fixes mutation scatter but not thread safety or the override bug.

### Use an FSM library (transitions, pytransitions)

`transitions` is a mature Python FSM library with guard conditions, callbacks, and diagram export. For six states and twelve edges it is more machinery than needed. `py-trees` (ADR 024) already covers the tactical decision layer; a second framework for the game-loop layer adds dependency overhead.

**Rejected:** Hand-rolled `transition()` with a frozenset table is sufficient and dependency-free.

---

## Consequences

**Positive:**
- Single source of truth for game state — one field, one lock, one log line per transition
- Invalid transitions are caught and logged immediately at the call site
- Entry/exit hooks eliminate the "remember to call the hook" class of bugs
- Thread safety: no more half-written multi-flag transitions visible to OCR threads
- `game_state` property is a trivial read — no priority logic, no health override, no lock contention from complex computation
- 46 scattered mutations → 46 `transition()` calls, each grep-able and auditable

**Negative:**
- Migration touches all three files; risk of missing a mutation site during Phase 2
- `force=True` bypass must be used carefully — it exists for the manual hotkey override only and should not become a workaround for missing valid-transition entries

**Neutral:**
- `GameState` enum is unchanged
- `_game_battle_alive` and health sub-state remain as-is; they are sub-state within `GAME_BATTLE`, not FSM states
