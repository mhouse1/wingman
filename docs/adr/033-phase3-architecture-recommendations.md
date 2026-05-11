# ADR 033 — Phase 3 Architecture Recommendations

| Status   | Date       | Wingman Version |
|----------|------------|-----------------|
| Accepted | 2026-05-11 | 1.6.7           |

## Context

With the ADR 031 performance tracker complete and v1.6.6/1.6.7 stable, four architectural issues have been identified that are worth resolving before or during Phase 3 (behaviour trees, squadron coordination). The current architecture is correct and fully functional — none of these are blockers for starting Phase 3. They are improvements that will make Phase 3 work cleaner to write and easier to extend.

Only Recommendation 3 (coordination bus) is a hard prerequisite — and only for the squadron coordination sub-goal, not for single-instance behaviour trees. Recommendations 1, 2, and 4 can be deferred; the risk is that deferring them makes Phase 3 code messier and harder to retrofit later.

This ADR records the four recommendations so they are not lost between sessions. Each item should be resolved with its own ADR when implementation begins.

---

## Recommendation 1 — Decouple Perception from FSM Triggers

### Problem

The background OCR thread (`_ocr_loop` in `analyzer.py`) currently calls FSM trigger methods directly on the controller. Perception and game-state transitions are entangled in the same code path: a raw pixel reading produces a state transition in one function call chain. This works today because there is one OCR thread and one controller, but it has two consequences:

1. **Testability**: triggering FSM transitions requires instantiating a full `GameStateAnalyzer` with a real thread pool.
2. **Phase 3 readiness**: behaviour trees need to consume perception events independently of FSM transitions. When a behaviour tree node asks "is an enemy nearby?", it should read from a perception layer, not re-query the FSM.

### Recommendation

Introduce a thin **PerceptionState** object — a plain dataclass or dict — that the OCR thread writes to, and that the FSM (and future behaviour tree nodes) reads from. The OCR thread owns writes; everything else reads.

```
OCR thread → PerceptionState (write)
Controller  → PerceptionState (read) → fires FSM transition
BehaviourTree → PerceptionState (read) → decides action
```

The perception state becomes the single source of truth for what the game currently looks like. The FSM becomes a consumer of that state, not a direct recipient of OCR callbacks.

### Phase 3 impact

**Not a blocker.** Behaviour tree nodes can read from the analyzer's existing cached values (`_cached_*` attributes) the same way `controller.py` already does. It works; it's just not a clean contract. The risk of deferring: every tree node author must understand which values are in the return dict and which require a direct cache read, and there is no single place to look for current game perception.

**Resolve with**: a new ADR covering PerceptionState schema, writer ownership, and reader access pattern. Best addressed together with Recommendation 4.

---

## Recommendation 2 — Extract MissionCoordinator from main.py

### Problem

`main.py` currently owns mission logic (J20 loop, loiter, respawn restart, flare deploy, manual override detection) alongside application startup and hotkey wiring. This is a single 500+ line file where mission strategy, concurrency setup, and entry-point logic are mixed.

Two specific symptoms:

- Adding a new mission type requires editing `main.py` directly, touching startup code.
- Manual override logic (`_manual_override_active`, `_cancel_active_mission`, `_deploy_flares_on_new_incoming`) is spread across `main.py` rather than owned by a focused component.

### Recommendation

Extract a **MissionCoordinator** class that owns:
- Active mission reference and lifecycle (start, cancel, restart)
- Manual override state
- Flare deployment callback
- Round-end hooks (currently wired directly in main)

`main.py` becomes a thin entry point: parse config, construct components (analyzer, tracker, coordinator), wire hotkeys, run the main loop. The coordinator becomes independently testable without the full application stack.

### Phase 3 impact

**Not a hard blocker, but the most likely to become painful mid-implementation.** Behaviour trees can call the same mission start/cancel functions `main.py` currently calls directly. `main.py` gets larger and harder to follow, but nothing breaks immediately. The risk: as soon as a behaviour tree needs to switch missions dynamically (the core Phase 3 use case), the lack of a clean owner becomes a real problem — mission lifecycle is scattered and the tree has no single object to talk to.

Of the non-blocking recommendations, this is the one most worth addressing before Phase 3 gets underway.

**Resolve with**: a new ADR covering MissionCoordinator interface, ownership of mission lifecycle, and interaction with the FSM.

---

## Recommendation 3 — Multi-Instance Coordination Bus

### Problem

The multi-instance (squadron) goal requires instances to coordinate: queue together, avoid targeting the same enemy, signal when one instance is dead and another should adjust. Today each Wingman instance is fully isolated — no IPC, no shared state, no coordination protocol.

The architecture document notes multi-instance as a capability (one instance per emulator window), but this refers to independent operation, not coordination. The long-term vision of a squadron requires a coordination layer.

### Recommendation

Design a lightweight **coordination bus** — a local message-passing layer between instances. The simplest form is a shared file or named pipe; a more robust form is a local ZeroMQ or similar. Key messages:

| Message         | Sender   | Consumers       |
|-----------------|----------|-----------------|
| `I_AM_ALIVE`    | instance | all instances   |
| `TARGET_LOCKED` | instance | all instances   |
| `I_AM_DEAD`     | instance | all instances   |
| `MATCH_PHASE`   | instance | all instances   |

Each instance reads the bus to build a squadron state view; the behaviour tree uses this to make coordinated decisions (e.g., suppress firing if another instance already has a lock).

### Phase 3 impact

**Hard blocker — for squadron coordination only.** Single-instance behaviour trees can be fully implemented without this. If Phase 3 starts with single-instance adaptive tactics (the natural starting point), this is not needed yet. It becomes required the moment squadron coordination work begins: instances have no way to signal each other without a bus.

The sequencing recommendation: implement single-instance behaviour trees first, then design the bus before starting any multi-instance coordination work.

**Resolve with**: a new ADR covering bus topology (file vs pipe vs socket), message schema, failure handling (instance crash/disconnect), and how the behaviour tree consumes the squadron state.

---

## Recommendation 4 — Resolve analyze_frame API Inconsistency

### Problem

`GameStateAnalyzer.analyze_frame()` has a mixed return pattern: it returns a dict of OCR results for some callers, while other consumers (`controller.py`) read perception state directly from the analyzer's internal cache (`_last_result`, `_cached_*` attributes) rather than from the return value.

This means the public API of `analyze_frame` is not the actual interface — callers use a mixture of the return value and direct cache reads. Adding a new consumer requires understanding which values come from the return dict and which require a direct cache attribute read.

### Recommendation

Standardise on one pattern:

**Option A — Return dict is authoritative**: `analyze_frame()` returns a complete dict of all perception values. All callers read from the return value only. Cache attributes become private implementation details.

**Option B — PerceptionState is authoritative** (pairs with Recommendation 1): `analyze_frame()` populates a `PerceptionState` object; the return value is dropped or kept as a compatibility alias. All callers read from the `PerceptionState`.

Option B is preferred because it aligns with the perception/FSM decoupling in Recommendation 1. Option A is the low-risk short-term fix if Recommendation 1 is deferred.

### Phase 3 impact

**Not a blocker.** The current mixed pattern works — `controller.py` already reads cached values directly and functions correctly. A behaviour tree node can follow the same pattern. The risk of deferring: the inconsistency becomes more visible as more consumers are added, and each new consumer has to rediscover which access pattern applies to which value.

This is the lowest-effort item and a good candidate for a standalone cleanup PR before Phase 3 begins.

**Resolve with**: a new ADR (or combined with the Recommendation 1 ADR) covering the chosen pattern and migration of existing `controller.py` direct cache reads.

---

## Summary

| # | Recommendation | Phase 3 Blocker? | Complexity | When to address |
|---|---------------|-----------------|------------|-----------------|
| 1 | Decouple perception from FSM triggers (PerceptionState) | No | Medium | Before or during Phase 3 |
| 2 | Extract MissionCoordinator from main.py | No — but most likely to hurt mid-implementation | Medium | Before starting behaviour tree work |
| 3 | Multi-instance coordination bus | Yes — for squadron coordination only | High | Before any multi-instance coordination work |
| 4 | Resolve analyze_frame API inconsistency | No | Low | Cleanup PR before Phase 3 |

Phase 3 can start without any of these. The practical order: do Rec 4 as a quick cleanup, address Rec 2 before writing the first behaviour tree node, tackle Recs 1 and 3 as needed once the Phase 3 scope becomes concrete.

---

## Implementation Timing Trade-off

### Cost of doing all four upfront

| Rec | Upfront cost | Retrofit cost if deferred | Net savings from upfront |
|-----|-------------|--------------------------|--------------------------|
| 4 — API cleanup | ~2h | ~5–8h cumulative friction | ~3–6h |
| 2 — MissionCoordinator | ~6h | ~6h extraction + 4–8h node rework | ~4–8h |
| 1 — PerceptionState | ~10h | ~10h + 3–5h retrofit of scattered cache reads | ~3–5h |
| 3 — Coordination bus | ~20h | ~20h (same cost whenever you do it) | ~0h |
| **Total** | **~38h** | | **~10–20h saved** |

Doing all four upfront costs ~38 hours before a single behaviour tree node runs, to save an estimated 10–20 hours later. That is a poor trade: real time spent now against speculative savings, while delaying the actual feature work.

### Why Rec 3 saves nothing upfront

The coordination bus costs the same ~20 hours regardless of when it is built. Single-instance behaviour tree nodes written before the bus can be designed with a `squadron_state` slot that is initially empty — when the bus is added later, the slot is filled and the nodes require minimal rework. There is no efficiency argument for building the bus before the single-instance tree is working.

### Recommended approach

Do **Rec 4** (2h) and **Rec 2** (6h) first — 8 hours total, captures most of the available savings:

- Rec 4 eliminates the mixed API before any tree nodes are written, preventing cumulative confusion.
- Rec 2 gives behaviour trees a clean mission interface before the first node exists, preventing the most painful retrofit scenario.

Defer **Rec 1** until the tree is growing and the scattered cache reads become visibly painful. Defer **Rec 3** until squadron coordination work actually begins.

## References

- [ADR 024 — Phase 3 Behaviour Tree Architecture](024-phase3-behavior-tree-architecture.md)
- [ADR 015 — Game State Machine](015-game-state-machine.md)
- [ADR 025 — Formalise Game State Machine](025-formalise-game-state-machine.md)
- [ADR 031 — Round-End Histogram Reporting](031-round-end-histogram-reporting.md)
- [docs/architecture.md](../architecture.md)
