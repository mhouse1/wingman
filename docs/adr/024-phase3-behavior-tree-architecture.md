# ADR 024 — Phase 3: Behavior Tree Architecture for Tactical Decision-Making

| Status   | Date       | Wingman Version |
|----------|------------|-----------------|
| Accepted | 2026-04-19 | 1.6.3           |

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

**Phase 3 goal:** Replace the scattered if/else logic with a structured `BehaviorTree` that selects the correct tactic each tick based on the full perceived state.

---

## Decision

Implement a lightweight **behavior tree (BT)** as the tactical decision layer between `GameStateAnalyzer` (perception) and `Controller` (action execution).

The BT runs on each perception tick (every ~1s) and selects one of a small set of predefined **leaf tactics**. Each tactic is a `Controller` method. The BT does not execute the tactic itself — it decides which tactic *should* be running, and signals the `Controller` to start or stop accordingly.

---

## Architecture

### Layer separation

```
┌─────────────────────────────────┐
│  GameStateAnalyzer (perception) │  OCR, pixel reads, state flags
└────────────────┬────────────────┘
                 │  AnalyzerSnapshot (read-only struct)
┌────────────────▼────────────────┐
│  BehaviorTree (decision)        │  tick() → TacticId
└────────────────┬────────────────┘
                 │  tactic selection
┌────────────────▼────────────────┐
│  Controller (action)            │  start/stop tactic methods
└─────────────────────────────────┘
```

The BT is **read-only** with respect to game state. It reads an `AnalyzerSnapshot` (a frozen dataclass) and returns a `TacticId`. The `Controller` is the only component that presses keys or moves the mouse.

### AnalyzerSnapshot

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

Snapshot is taken once per tick. The BT sees a consistent view of state — no risk of flags changing mid-evaluation.

### Tactic IDs

```python
class Tactic(enum.Enum):
    IDLE          = "idle"           # do nothing (lobby, waiting)
    ATTACK        = "attack"         # standard J20 sequence + S&D loop
    EVADE         = "evade"          # evasive maneuver (health critical)
    EJECT         = "eject"          # eject_and_dive (no missiles)
    DISENGAGE     = "disengage"      # roll right away from area (enemy gone 30s)
    RESPAWN_WAIT  = "respawn_wait"   # wait for respawn signal
```

### Behavior tree structure

Evaluated top-to-bottom; first matching condition wins.

```
Root (Selector)
├── IDLE          — if game_state not GAME_BATTLE
├── RESPAWN_WAIT  — if is_respawning
├── EJECT         — if missiles == 0
├── EVADE         — if health is not None and health < 25  (future: threshold TBD)
├── DISENGAGE     — if enemy_absent_seconds >= 30 and no mission running
└── ATTACK        — default (healthy, armed, enemy present or unknown)
```

This is a pure **priority selector** — no memory, no partial trees, no accumulators. Each tick is evaluated independently. This makes it easy to reason about: given any snapshot, you can determine exactly which tactic will be selected by reading down the list.

### Tick integration

The BT tick runs in the main loop after perception events are processed:

```python
# main loop (after OCR event handlers)
snapshot = analyzer.snapshot()
tactic = behavior_tree.tick(snapshot)
ctrl.set_tactic(tactic)
```

`Controller.set_tactic(tactic)` compares the new tactic to the currently-running tactic. If unchanged, it's a no-op. If different, it stops the current tactic (cancel_mission / stop_sdl_loop) and starts the new one.

---

## Migration from current architecture

### What stays

- `GameStateAnalyzer` — unchanged, still produces events and state flags
- `Controller` tactic methods — `mission_j20`, `eject_and_dive`, `disengage_roll_right`, `start_search_and_destroy_loop` — become BT leaf actions, otherwise unchanged
- Event handlers in `main.py` — `_handle_alive_transition`, `_deploy_flares_on_new_incoming` — stay as immediate reactive handlers (they bypass the BT because they need sub-second response)

### What changes

| Current | Phase 3 |
|---------|---------|
| `_handle_no_missiles()` in `main.py` calls `eject_and_dive()` directly | BT selects `EJECT` tactic; `Controller.set_tactic` calls `eject_and_dive()` |
| `mission_j20()` hardcodes linear sequence | `ATTACK` tactic starts `mission_j20()`; BT can switch away mid-mission |
| Enemy no-detection timer in `main.py` | `enemy_absent_seconds` in snapshot; `DISENGAGE` leaf in BT |
| No low-health behaviour | `EVADE` leaf added when health perception threshold is calibrated |

### What is deferred to Phase 4

- Learning which tactic performs best (RL)
- Per-enemy-type tactic variation
- Formation awareness
- Altitude/speed as decision inputs

---

## Alternatives considered

### 1. Continue ad-hoc if/else in main loop

Already the current approach. Works for simple cases, but adding Phase 3 tactics means adding more interleaved flags and timers to `main.py`. The 30s enemy-absent disengage check is a preview of how this scales: it required a `enemy_last_seen_ts` variable threaded through the loop, a check, and a controller call — all in the main loop body. A second and third tactic follow the same pattern and the loop becomes unmanageable.

**Rejected:** Does not scale.

### 2. Finite State Machine (FSM)

An FSM with states `ATTACKING`, `EVADING`, `EJECTING`, etc. and explicit transitions. Appropriate when transitions have side effects or guards that depend on *how* a state was entered (history). For Wingman, tactic selection is purely a function of current snapshot — no history needed. An FSM adds transition declarations without adding clarity.

**Rejected:** Unnecessary complexity for a memoryless selector.

### 3. Full py-trees library

`py-trees` provides composites, decorators, blackboard, and tick lifecycle. It would be a natural fit for Phase 4 when tactics become sub-trees with memory. For Phase 3 the tree is a flat priority selector — implementing it with `py-trees` adds a dependency and learning curve for what is currently a 30-line class.

**Deferred to Phase 4:** Adopt `py-trees` when the tree grows beyond a flat selector and needs decorators (cooldowns, retry) or blackboard sharing between nodes.

### 4. Skip behavior trees, go straight to Phase 4 RL

Phase 4 RL needs a training environment, a reward function, and a policy network. It also needs all the Phase 2 perception signals to be reliable — which they now are. However, RL requires 1000s of training missions. A behavior tree serves as a **strong baseline policy** that can be used to bootstrap RL (imitation learning) and also validates that the perception signals are actionable before committing to RL infrastructure.

**Rejected as Phase 3 skip:** BT is a prerequisite for RL, not an alternative.

---

## Implementation plan

1. **`wingman/behavior_tree.py`** — `AnalyzerSnapshot`, `Tactic` enum, `BehaviorTree.tick()`
2. **`wingman/controller.py`** — `set_tactic(tactic: Tactic)` — stops current, starts new
3. **`wingman/main.py`** — replace per-tactic event handlers with single `behavior_tree.tick()` call; keep immediate reactive handlers (`_deploy_flares_on_new_incoming`, `_handle_alive_transition`)
4. **`tests/test_behavior_tree.py`** — unit tests: given snapshot X → assert tactic Y (no Controller, no OCR, pure logic)

The EVADE tactic is stubbed as ATTACK until a health threshold is calibrated on real gameplay data. `Tactic.EVADE` is defined in the enum from day one so the BT structure is complete even before the threshold is known.

---

## Consequences

**Positive:**
- Adding a new tactic is one new `Tactic` enum value + one new leaf node + one `Controller` method — isolated, testable
- BT is unit-testable without mocking OCR or Controller; snapshot is a plain frozen dataclass
- Tactic selection logic is in one place and readable top-to-bottom
- Enables Phase 4 RL: the BT becomes the baseline policy; RL can shadow or replace individual leaves

**Negative:**
- `set_tactic` introduces a new source of `cancel_mission` calls; must be careful not to double-cancel in the same tick as a keyboard hotkey cancel
- The BT tick rate is bounded by OCR cycle time (~1s); truly reactive behaviour (flares on incoming) must remain as direct event handlers, not BT leaves

**Neutral:**
- Existing `mission_j20` and `eject_and_dive` are not rewritten — they become leaf targets. Phase 3 is an architecture layer over existing actions, not a replacement of them.
