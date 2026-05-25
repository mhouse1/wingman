# ADR 039 - Reduce Orchestration Coupling First

| Status   | Date       | Wingman Version |
|----------|------------|-----------------|
| Draft    | 2026-05-25 | 1.6.10          |

## Context

The current orchestration layer in wingman/main.py coordinates capture, analyzer, and
controller behavior, but it also reaches into private analyzer and controller internals.

Examples include direct access to private fields and side effects that bypass formal
module boundaries. This creates three risks:

- brittle integration points that break during internal refactors
- reduced testability because orchestration depends on implementation details
- increased delivery risk for Phase 3 behavior work that will add more control paths

Recent architecture work has improved FSM discipline and replay-based integration
coverage, but orchestration coupling remains a scaling bottleneck.

## Decision

Prioritize orchestration decoupling before broadening tactical behavior scope.

Introduce a narrow orchestration contract between main, analyzer, and controller,
replacing private-field access with explicit APIs and events.

This refactor is sequenced ahead of major Phase 3 expansion to prevent complexity debt
from compounding.

## Scope

In scope:

- define a minimal public orchestration interface for analyzer and controller
- remove direct private member access from main.py on mission-critical paths
- route orchestration actions through explicit methods or event callbacks
- preserve current runtime behavior and hotkey semantics

Out of scope for this ADR:

- behavior policy redesign for Phase 3
- OCR model/crop tuning changes
- broad controller mission logic rewrites unrelated to coupling boundaries

## Orchestration Contract

The orchestration contract should expose:

- state query methods required by main loop decisions
- explicit transition triggers and lifecycle callbacks
- safe command methods for respawn, mission cancel, restart, and shutdown
- event payloads for replay and performance instrumentation

Contract rules:

- no main.py access to underscore-prefixed fields in analyzer or controller
- no lock ownership transfer across module boundaries
- callbacks should be idempotent where retries are possible

## Implementation Plan

1. Inventory current private-field touchpoints in main.py and group by behavior path.
2. Add public methods on analyzer/controller for each required operation.
3. Replace private-field references in main.py with contract calls.
4. Add compatibility shims only where needed for staged migration.
5. Remove obsolete shims after tests pass and behavior parity is confirmed.
6. Document the contract in module docstrings and development notes.

## Implementation Appendix - Copilot Execution Pack

### A. Private Touchpoint Inventory (Current State)

Observed underscore-prefixed coupling in wingman/main.py:

- analyzer._on_cancel_mission assignment
- analyzer._on_start_game_starting_loop assignment
- analyzer._on_lobby_play_click assignment
- analyzer._on_lobby_popup_click assignment
- analyzer._trigger calls (continue_clicked, waiting_timeout, cancel_detected, manual_reset, respawn_reset)
- analyzer._ammo_lock and analyzer._ammo_missiles reads
- analyzer._ocr_cache_lock and analyzer._ocr_cache reads
- analyzer._incoming_cache_lock and analyzer._incoming_cache reads
- analyzer._click_to_cache_lock and analyzer._click_to_cache reads
- ctrl._start_game_starting_loop reference
- ctrl._auto_respawn_restart read/write
- ctrl._eject_stop.set() call

### B. Target Public API Contract

Analyzer public API to add:

- set_on_cancel_mission(callback: Callable[[], None]) -> None
- set_on_start_game_starting_loop(callback: Callable[[], None]) -> None
- set_on_lobby_play_click(callback: Callable[[str], None]) -> None
- set_on_lobby_popup_click(callback: Callable[[str], None]) -> None
- trigger_event(name: str) -> None
- get_ammo_missiles() -> int | None
- get_respawn_cache_result() -> tuple[bool, float | None, str | None]
- get_incoming_cache_result() -> tuple[bool, float | None, str | None]
- get_incoming_cache_timestamp() -> float
- get_click_to_cache_result() -> tuple[bool, float | None, str | None]
- get_click_to_cache_timestamp() -> float

Controller public API to add:

- start_game_starting_loop() -> None
- is_auto_respawn_restart_enabled() -> bool
- set_auto_respawn_restart(enabled: bool) -> None
- stop_eject_sequence() -> None

Contract constraints:

- main.py must not directly access underscore-prefixed members on analyzer or controller
- public API methods own internal locking and return immutable snapshot values
- callback setters must be safe to call once at startup and replace existing callback explicitly

### C. Replacement Mapping (Old to New)

- analyzer._trigger(name) -> analyzer.trigger_event(name)
- ctrl._start_game_starting_loop -> ctrl.start_game_starting_loop
- ctrl._auto_respawn_restart read -> ctrl.is_auto_respawn_restart_enabled()
- ctrl._auto_respawn_restart write -> ctrl.set_auto_respawn_restart(True/False)
- ctrl._eject_stop.set() -> ctrl.stop_eject_sequence()
- analyzer._ammo_* reads -> analyzer.get_ammo_missiles()
- analyzer._ocr_cache read -> analyzer.get_respawn_cache_result()
- analyzer._incoming_cache reads -> analyzer.get_incoming_cache_result(), analyzer.get_incoming_cache_timestamp()
- analyzer._click_to_cache reads -> analyzer.get_click_to_cache_result(), analyzer.get_click_to_cache_timestamp()

### D. Test Gates (Required Commands)

Run in order:

1. make test
2. uv run pytest tests/test_analyzer.py -q
3. uv run pytest tests/test_main_game_end.py -q
4. uv run pytest tests/test_automated_levels.py -q

Optional but recommended before merge:

1. make tp

### E. Definition of Done and Exit Criteria

Step 1 exit:

- inventory list is complete and validated against main.py

Step 2 exit:

- all API methods listed in section B are implemented with tests for method behavior

Step 3 exit:

- main.py contains zero matches for analyzer._ and ctrl._ member access (except local variable names that do not dereference members)

Step 4 exit:

- any temporary compatibility shim is documented with a removal note and owner

Step 5 exit:

- all required test gates pass
- replay and mission lifecycle behavior remains unchanged in observed logs

Step 6 exit:

- module docstrings include the new orchestration contract and callback lifecycle notes

## Acceptance Criteria

- main.py no longer references analyzer/controller underscore-prefixed members
- integration replay tests pass with no regression in mission lifecycle behavior
- FSM transitions still enforce valid trigger semantics
- shutdown and cleanup remain deadlock-free under repeated start/stop cycles

## Consequences

Positive:

- clearer module boundaries and safer internal refactoring
- lower coupling cost before Phase 3 branch growth
- improved reliability of replay-driven regression testing

Trade-offs:

- short-term refactor effort before feature acceleration
- temporary API surface growth during migration

## Test Strategy

Required coverage:

- unit tests for new public orchestration methods
- lifecycle tests for respawn, restart, cancel, and shutdown sequences
- replay integration tests for end-to-end state progression
- regression checks ensuring action-intent output remains stable

## Alternatives Considered

1. Defer coupling cleanup until after Phase 3.
   - Rejected because new behavior branches will increase coupling debt and migration cost.

2. Perform a full architecture rewrite now.
   - Rejected because targeted contract extraction provides most value with less risk.

3. Keep private access but enforce stronger code review gates.
   - Rejected because process controls do not remove technical coupling or fragility.

## References

- docs/adr/024-phase3-behavior-tree-architecture.md
- docs/adr/025-formalise-game-state-machine.md
- docs/adr/033-phase3-architecture-recommendations.md
- docs/adr/037-timed-screenshot-replay-integration-testing.md
