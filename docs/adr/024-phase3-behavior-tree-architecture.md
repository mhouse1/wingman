# ADR 024 — Phase 3: Behavior Tree Architecture for Tactical Decision-Making

| Status   | Date       | Wingman Version |
|----------|------------|-----------------|
| Draft    | 2026-04-19 | 1.6.6           |

## Context

Phase 2 is complete as of v1.6.3. The bot can now perceive:

| Signal | Source | Action today |
|--------|--------|--------------|
| Health (0–300+) | OCR digit read | Restart mission on death (`False→True` transition) |
| Missiles remaining | OCR digit read | `eject_and_dive()` on 0 |
| Flares remaining | OCR digit read | Log warning at 2 |
| Enemy close-by (red pixel) | `ENEMY_CLOSE_BY` region | `disengage_roll_right()` after 30s no detection |
| Respawn text | EasyOCR | Cancel + restart |
| Incoming missile text | EasyOCR | Deploy flares |

The perception layer is solid. The problem is what happens with these signals. Decision-making is currently scattered across three files as a mix of reactive event handlers and ad-hoc timers:

- `main.py` — `_handle_no_missiles()`, `_handle_low_flares()`, `_handle_alive_transition()`, enemy proximity check
- `controller.py` — `mission_j20()` hardcodes a linear sequence (nose_up → afterburner → roll → S&D loop) with cancellation as the only branch
- Both files — separate timers and flags that interact in ways that are hard to reason about

The result: the bot has one tactic (J20 attack sequence) that it runs regardless of game state, interrupting only on death or eject conditions. Adding a new tactic (e.g. "evade when health is low") means modifying three files and reasoning about shared state across threads.

Two tactics already require persistent timers that survive across ticks:

- **DISENGAGE** — only triggers after enemy absent for 30s (the `enemy_last_seen_ts` variable in `main.py`)
- **EVADE** — once entered, should not flip back to ATTACK the moment health recovers slightly (needs a hold timer)

Without a framework, these timers accumulate as additional `_xxx_ts` variables in `main.py` — the exact pattern Phase 3 is trying to eliminate.

**Phase 3 goal:** Replace the scattered if/else logic with a structured behavior tree using `py-trees`, adopted now so the scaffolding is in place for Phase 4 RL.

---

## Decision

Adopt **`py-trees`** as the behavior tree framework in Phase 3.

Reasons to adopt now rather than defer to Phase 4:

1. **Cooldown/hold decorators needed immediately.** The DISENGAGE 30s timer and EVADE hold are `py-trees` `Cooldown` and `EternalGuard` decorators — without the framework these go back into `main.py` as ad-hoc timestamps.
2. **No migration cost later.** Controller tactic wrappers written as py-trees `Action` nodes in Phase 3 require no rewrite for Phase 4. Deferring means writing them twice.
3. **Blackboard available from day one.** Phase 4 RL will need shared state between nodes (e.g. reward accumulation, episode context). The py-trees blackboard can carry this; adding it later requires retrofitting every node.
4. **Glue code is small and written once.** The only Phase 3 cost unique to py-trees (vs. a hand-rolled selector) is the thread-liveness → `RUNNING/SUCCESS/FAILURE` wrapper — approximately 20 lines, written once, reused by every leaf.

---

## Architecture

### Layer separation

```
┌─────────────────────────────────┐
│  GameStateAnalyzer (perception) │  OCR, pixel reads, state flags
└────────────────┬────────────────┘
                 │  AnalyzerSnapshot (frozen dataclass, taken once per tick)
┌────────────────▼────────────────┐
│  py-trees BehaviorTree          │  tick() → RUNNING / SUCCESS / FAILURE
│  (Selector root)                │
│  ├─ IdleCondition               │
│  ├─ RespawnWaitAction           │
│  ├─ EjectAction                 │
│  ├─ Cooldown(EvadeAction, 10s)  │
│  ├─ Cooldown(DisengageAction)   │
│  └─ AttackAction                │
└────────────────┬────────────────┘
                 │  start / stop tactic
┌────────────────▼────────────────┐
│  Controller (action)            │  mission_j20, eject_and_dive, etc.
└─────────────────────────────────┘
```

### AnalyzerSnapshot

Taken once at the start of each tick and written to the py-trees blackboard. All nodes read from the blackboard — no node holds a reference to the live `Analyzer`.

```python
@dataclass(frozen=True)
class AnalyzerSnapshot:
    health: int | None          # last OCR reading, None if unseen
    missiles: int | None
    flares: int | None
    enemy_visible: bool         # red pixel in ENEMY_CLOSE_BY
    enemy_absent_seconds: float # seconds since last red pixel
    is_respawning: bool
    incoming_detected: bool
    game_state: GameState
```

### Node types used

| py-trees type | Used for |
|---------------|----------|
| `py_trees.composites.Selector` | Root — first SUCCESS child wins |
| `py_trees.behaviour.Behaviour` | Each tactic leaf (Idle, Attack, Eject, …) |
| `py_trees.decorators.Cooldown` | Prevent EVADE flapping; DISENGAGE hold |
| `py_trees.blackboard.Blackboard` | Share `AnalyzerSnapshot` across nodes |

### Thread-liveness glue

Controller tactics run in background threads. py-trees nodes return synchronous `RUNNING/SUCCESS/FAILURE`. The bridge:

```python
class TacticAction(py_trees.behaviour.Behaviour):
    def __init__(self, name, start_fn, is_running_fn):
        super().__init__(name)
        self._start_fn = start_fn
        self._is_running_fn = is_running_fn

    def update(self):
        snapshot = self.blackboard.get("snapshot")
        if not self._should_run(snapshot):
            return py_trees.common.Status.FAILURE
        if not self._is_running_fn():
            self._start_fn()
        return py_trees.common.Status.RUNNING

    def terminate(self, new_status):
        if new_status == py_trees.common.Status.INVALID:
            ctrl.cancel_mission()  # BT switched away — stop this tactic
```

`is_running_fn` checks thread liveness (`thread.is_alive()`). `start_fn` calls the existing `Controller` method unchanged.

### Behavior tree structure

```
Selector (root)
├── Idle          — SUCCESS if game_state != GAME_BATTLE  (no action)
├── RespawnWait   — SUCCESS if is_respawning              (hold, wait for alive event)
├── Eject         — SUCCESS if missiles == 0              (eject_and_dive)
├── Cooldown(10s)
│   └── Evade     — SUCCESS if health < EVADE_THRESHOLD   (evasive maneuver; threshold TBD)
├── Cooldown(30s)
│   └── Disengage — SUCCESS if enemy_absent_seconds >= 30 (disengage_roll_right)
└── Attack        — always SUCCESS                        (mission_j20 + S&D loop)
```

Priority order: top node wins. `Cooldown` wraps EVADE and DISENGAGE so that once selected, the tactic holds for its minimum duration before the selector re-evaluates. ATTACK is the unconditional fallback.

### Tick integration

```python
# main loop — after OCR event handlers
snapshot = analyzer.snapshot()
blackboard = py_trees.blackboard.Blackboard()
blackboard.set("snapshot", snapshot)
behavior_tree.tick()
```

Immediate reactive handlers (`_deploy_flares_on_new_incoming`, `_handle_alive_transition`) remain outside the BT — they need sub-100ms response, faster than the ~1s OCR tick rate.

---

## Migration from current architecture

### What stays

- `GameStateAnalyzer` — unchanged
- `Controller` tactic methods (`mission_j20`, `eject_and_dive`, `disengage_roll_right`) — become `start_fn` targets in `TacticAction` wrappers, otherwise unchanged
- `_deploy_flares_on_new_incoming`, `_handle_alive_transition` — stay as direct event handlers

### What changes

| Current | Phase 3 |
|---------|---------|
| `_handle_no_missiles()` calls `eject_and_dive()` directly | `EjectAction` node calls it; `_handle_no_missiles` removed |
| Enemy 30s timer as `enemy_last_seen_ts` in main loop | `enemy_absent_seconds` in snapshot; `Cooldown(DisengageAction)` node |
| `mission_j20()` linear sequence, cancel-only branching | `AttackAction` leaf starts it; BT switches away cleanly via `terminate()` |
| No EVADE behaviour | `Cooldown(EvadeAction)` node, threshold stubbed as ATTACK until calibrated |
| `_handle_low_flares()` logs warning | Low flares stays a log; Phase 4 can add a `LowAmmoAction` leaf |

### What is deferred to Phase 4

- RL policy replacing or shadowing individual leaf nodes
- Per-enemy-type subtrees
- `py_trees.decorators.Retry` on failed attacks
- Formation and altitude as blackboard inputs

---

## Alternatives considered

### 1. Hand-rolled flat priority selector (original plan)

30-line class, no dependency. Adequate for a memoryless selector. Rejected because DISENGAGE and EVADE require persistent timers — without `py-trees` decorators those timers return to `main.py` as ad-hoc timestamp variables, which is the problem Phase 3 exists to solve. Also requires a full rewrite when Phase 4 adopts py-trees anyway.

**Rejected:** Defers the migration cost without eliminating it; reintroduces timer scatter.

### 2. Finite State Machine (FSM)

Explicit states with named transitions. Appropriate when transition guards depend on history. Tactic selection in Wingman is a function of the current snapshot only — the FSM's transition table adds declarations without adding clarity over a priority selector.

**Rejected:** Unnecessary complexity for a priority-based selector.

### 3. Skip to Phase 4 RL directly

RL requires a training loop, reward function, and policy network, plus thousands of missions to converge. The BT provides the baseline policy that RL can use for imitation learning and validates that perception signals are reliable before committing to RL infrastructure.

**Rejected:** BT is a prerequisite, not an alternative.

---

## Implementation plan

1. **`pyproject.toml`** — add `py-trees` dependency
2. **`wingman/behavior_tree.py`** — `AnalyzerSnapshot`, `TacticAction` base class, full tree construction
3. **`wingman/controller.py`** — remove `set_tactic()`; `TacticAction.terminate()` calls `cancel_mission()` directly
4. **`wingman/main.py`** — replace per-tactic handlers with `blackboard.set` + `behavior_tree.tick()`; keep reactive handlers
5. **`tests/test_behavior_tree.py`** — unit tests: given snapshot X → assert node status Y (no Controller threads, no OCR)

`Tactic.EVADE` threshold is set to `None` (disabled) until calibrated on real gameplay data. The node exists in the tree; it simply returns `FAILURE` immediately when threshold is unset.

---

## Consequences

**Positive:**
- Timer/cooldown logic lives in py-trees decorators, not in `main.py` variables
- Adding a new tactic = one `TacticAction` subclass + one leaf in the tree
- Unit-testable at the node level with no threading or OCR
- Phase 4 RL can replace any leaf with a learned policy node; no structural change needed
- Blackboard available for Phase 4 reward/episode state from day one

**Negative:**
- `py-trees` is a new runtime dependency (~500 lines, pure Python, no native extensions)
- `RUNNING/SUCCESS/FAILURE` semantics require the thread-liveness glue layer (~20 lines)
- py-trees tick model is synchronous; care needed that `behavior_tree.tick()` does not block the main loop (it won't — `update()` only checks thread liveness, never waits)

**Neutral:**
- Existing Controller tactic methods are not rewritten — they become `start_fn` arguments
- Immediate reactive handlers stay outside the BT and are unaffected
