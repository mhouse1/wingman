# ADR 060 — Tick-Loop Handler Objects and Typed Event Registry

| Status | Date       | Wingman Version |
|--------|------------|-----------------|
| Draft  | 2026-08-02 | 1.6.29          |

Extends [ADR 039](039-reduce-orchestration-coupling-first.md) (Accepted).
ADR 039 established the `set_on_*` orchestration API to decouple the analyzer
from the controller; this ADR supersedes ADR 039 only on the *mechanism* of
that API (single-slot setters become a typed multi-subscriber registry) and
keeps its direction-of-dependency decision intact. Also builds on the
consolidation precedent set by [ADR 059](059-health-gated-immediate-mission-restart.md).

## Status of this document

**Draft — awaiting review. No implementation has been started.** This ADR
exists so the refactor can be evaluated and approved (or rejected/deferred)
before any code changes. Each phase below is independently shippable and
independently abandonable.

**Revised 2026-08-02** after the ADR 061-064 respawn-detection arc landed.
All measurements below are re-taken against current code; the arc supplied
four new pieces of evidence (see "What the 061-064 arc added"), none of
which change the proposed design — they sharpen the case for it and shift
which file is under the most pressure.

## Context

Three structural pressure points have accumulated, re-measured 2026-08-02:

| File | v1.6.23 (CR-013) | 2026-08-01 | 2026-08-02 | Growth since CR-013 |
|------|------------------|------------|------------|---------------------|
| `wingman/main.py` | 1195 | 1228 | 1292 | +8% |
| `wingman/analyzer.py` | 2076 | 2361 | **2746** | **+32%** |
| `wingman/controller.py` | 1851 | 2555 | 2572 | +39% |

`analyzer.py` gained 385 lines in the two days of the 061-064 arc alone and
is now the fastest-growing file in the repo — a reordering versus the
original draft, where `controller.py` was the outlier.

**1. `main()` is a single ~1140-line function** (`wingman/main.py:148`) whose
tick loop coordinates every runtime concern through roughly 25 loop-local
variables (`main.py:503-529`) and 10 nested closure handlers
(`_handle_no_missiles`, `_handle_alive_transition`, `_handle_low_flares`,
`_deploy_flares_on_new_incoming`, etc.), 6 of which mutate shared state via
`nonlocal`. Concerns that must not interact still share one namespace:

- **CR-013-4** (code review 013): the eject-interrupt call sat inside the
  respawn-restart dedup cooldown branch, so a respawn within 10 s of a prior
  one never released afterburner — two unrelated concerns coupled by
  accidental block nesting.
- **ADR 059's motivating failure**: three overlapping restart mechanisms in
  the same function, unaware of each other's gating assumptions, produced an
  uncommanded aircraft in production (2026-07-31 07:42). The fix — delete the
  competing paths and give one mechanism sole ownership — is the pattern this
  ADR generalizes.

**2. The analyzer's orchestration hooks are 7 single-slot setters**
(`analyzer.py:1010-1043`, from ADR 039) plus 28 more informal `self._on_*`
attributes — both counts unchanged through the arc. Two concrete costs, both
live today:

- `set_on_fsm_transition` holds **one** callback, so replay assertions, live
  capture, and `MissionStatsTracker` are wired as mutually exclusive
  `if/elif/else` branches (`main.py:480/487/497`). Mission statistics are
  silently not recorded during replay or capture runs — not by design, but
  because the slot cannot multiplex.
- Registration is stringly and unchecked: a typo'd hook name or a
  double-registration overwriting an earlier subscriber fails silently at
  runtime (the same defect class as CR-013-6's crop-key typo and CR-013's
  duplicate lobby-stall/escape loops).

**3. `controller.py` grew 39% in two weeks** (ADR 058 closed-loop eject).
`eject_and_dive` is now a multi-phase stateful sequence (nose-down hold,
telemetry confirmation, correction budget, post-release observation, decay
re-entry, and since ADR 058 decision 11 a mid-streak deadline grace)
implemented as a nested thread function inside a 2572-line class.

The condition stated when this refactor was first proposed — "only pays off if
more features are planned in this area" — is now met: ADR 055, 056, 058, 059,
and the 061-064 arc all landed in this code within six weeks.

### What the 061-064 arc added (2026-08-02)

The respawn-detection arc (061 Accepted, 062 Rejected, 063 Accepted, 064
Accepted) was developed *without* this refactor and is therefore a clean
natural experiment on whether the problem it describes is real. Four
findings, all from production evidence:

**A. A shared-mutable-state collision cost five missed detections and was
fixed with a duplicate field.** The OCR respawn path calls
`reset_health_for_respawn()`, which zeroes `_health_no_digits_since`. The
health-respawn detector depended on that same field as its evidence clock,
so the OCR plumbing silently wiped the evidence of the detector it was being
measured against — five structural misses in one session. The fix
(`analyzer.py:843`) was to add a **private duplicate clock** the OCR path
does not touch. That is precisely the failure mode Phase 2 rule 2 prohibits,
and the workaround is precisely what "each concern owns its own state" would
have made unnecessary.

**B. Two concerns interacting through unnamed shared state needed an FSM
state gate as a discriminator.** Eject sequencing thrashes health state
(synthetic dead reset, garbage-zero dips at missiles-empty); the respawn
detector read that thrash as death-then-recovery and produced six false
fires, all 1-2 s into `GAME_BATTLE_EJECT` (ADR 064 amendment 2). The fix
gates weak fires on FSM state. It works, but it is a *coordinate check*
between two concerns that have no explicit interface — the shape Phase 2
rule 2 replaces with a named event.

**C. The one place the Phase 2 pattern was applied, it worked.** ADR 061
needed the alive-event disposition logic to be testable, so it was extracted
as a module-level pure function, `_alive_transition_disposition`
(`main.py:129`), taking state in and returning a decision — no `nonlocal`,
no closure capture. It is now covered by four unit tests that run without
booting the loop, and it made ADR 061 rule 3 ("no silent consumption")
mechanically checkable. This is a micro-instance of the Phase 2 handler
extraction, and it is the only part of the arc's main-loop logic that is
unit-tested.

**D. The respawn plumbing now has two entry points, in the exact block
CR-013-4 lived in.** `main.py:1088-1092` gates on
`game_state.get('is_respawning') or health_fallback_respawn`, with the
fallback arriving via a `threading.Event` reached across module boundaries
(`analyzer.health_respawn_event`). That event is an ad-hoc, single-purpose
instance of the cross-module signal Phase 1's registry generalizes — the
second such signal (after `alive_event`) to be added by hand.

None of this argues the arc should have waited for the refactor: the bugs
were found in shadow mode at zero operational cost, and the fixes are
correct. It argues that the *rate* of these coupling questions is not
declining, and that each is currently answered with a bespoke workaround
rather than a structural rule.

## Decision

Two changes, ordered cheapest-first. Phase 1 does not depend on Phase 2.

### Phase 1 — Typed event registry (analyzer orchestration)

Replace the single-slot `set_on_*` setters with one small registry owned by
the analyzer:

```python
class GameEvent(Enum):
    CANCEL_MISSION = auto()
    START_GAME_STARTING_LOOP = auto()
    LOBBY_PLAY_CLICK = auto()
    LOBBY_POPUP_CLICK = auto()
    LOBBY_STALL = auto()
    FSM_TRANSITION = auto()
    RESPAWN_DETECTED = auto()

# analyzer side
def subscribe(self, event: GameEvent, callback, *, name: str) -> None: ...
def emit(self, event: GameEvent, *args) -> None: ...
```

Properties the current mechanism lacks:

- **Multi-subscriber**: `FSM_TRANSITION` fans out to replay assertions, live
  capture, and stats simultaneously — the `if/elif/else` exclusivity in
  `main.py:480/487/497` disappears, and `MissionStatsTracker` records during
  replay/capture lanes too.
- **Registration-time failure**: subscribing to a nonexistent event is an
  `Enum` attribute error at wiring time, not a silent no-op at runtime.
  Duplicate `name` registration raises immediately instead of silently
  replacing the earlier subscriber.
- **Uniform dispatch guard**: `emit()` wraps each callback in the
  try/except-log pattern currently copy-pasted at every `_on_*` call site,
  and is the single place to enforce the existing rule that callbacks fired
  from background threads must not block.

The 7 ADR 039 setters migrate first; the ~28 informal `self._on_*` attributes
migrate opportunistically as their call sites are next touched (no big-bang).
The old setters remain as thin deprecated shims delegating to `subscribe()`
until the last caller is gone, so the change is non-breaking.

### Phase 2 — Per-concern tick handlers (main loop)

Extract the tick-loop body into small handler objects, one per concern, each
owning the state that today lives as loop-locals and `nonlocal`s. `main()`
keeps setup, wiring, and the loop skeleton; each tick becomes a fixed
sequence of handler calls.

```mermaid
flowchart TD
    A[main tick loop] --> B[capture frame and analyze]
    B --> C[RespawnHandler]
    C --> D[AmmoEventsHandler]
    D --> E[EnemyPresenceHandler]
    E --> F[WaitingFallbackHandler]
    F --> G[TrackingHudHandler]
    G --> A
```

Each handler receives a per-tick context (frame, game state, timestamp) and
holds its own private state:

| Handler | Absorbs today's loop state | Absorbs today's closures |
|---------|---------------------------|--------------------------|
| `RespawnHandler` | `respawn_state`, `respawn_cooldown_until`, `respawn_clear_since`, `missile_ignore_until` | respawn-detection block (both OCR and ADR 064 fallback entry points), `_handle_alive_transition` — which keeps calling the already-extracted `_alive_transition_disposition` |
| `AmmoEventsHandler` | `no_missiles_zero_streak`, `last_flare_reload_ts`, `battle_started_ts`, padlock spread counters | `_handle_no_missiles`, `_handle_low_flares` |
| `EnemyPresenceHandler` | `enemy_last_seen_ts` | 30 s disengage block |
| `WaitingFallbackHandler` | `waiting_fallback_score`, `waiting_fallback_consecutive`, `game_waiting_since`, PLAY re-click state | `_update_waiting_fallback` wiring, re-click block |
| `TrackingHudHandler` | tracker/HUD instances, missiles snapshot reuse | tracker update plus HUD render block |

Rules that make this safe rather than cosmetic:

1. **Behavior-preserving by construction**: handlers are extracted one at a
   time, each as a pure code move (same statements, same order), with
   `make tp` (ADR 044 replay gate + ADR 045 live gate) green after every
   single extraction before the next begins.
2. **Cross-handler communication only via the Phase 1 registry or explicit
   return values** — never via shared mutable variables. Where two concerns
   genuinely interact (respawn must interrupt eject), the interaction becomes
   a visible, named event instead of block nesting. CR-013-4 could not have
   been written under this rule.
3. **Handler order is fixed and documented** in the tick loop; the current
   implicit ordering (flares before respawn, respawn `continue` short-circuit)
   is preserved and stated in each handler's docstring.

`eject_and_dive`'s extraction from `controller.py` into its own module is
explicitly **out of scope** here — ADR 058 is still `Draft` and stabilizing
in production (its decision 11 streak-grace amendment landed 2026-08-02 and
has one session of evidence), so refactoring it now would confound that
validation. Revisit after ADR 058 is Accepted.

The analyzer's health-respawn detector state (13 fields added by ADR
062-064: mark tier/timestamp, confirmed-read anchor and history, gap
instrumentation, fire log, OCR edge log, `health_respawn_event`) is likewise
**out of scope for Phase 2** — it lives in `analyzer.py`, not the tick loop.
It is called out here because it is the strongest candidate for a *third*
phase (a `RespawnDetector` collaborator object inside the analyzer), and
because finding B above shows it already interacts with eject sequencing
through state rather than interface. Phase 3 is deliberately not specified
in this ADR; it should be its own decision once Phases 1-2 have shown their
cost/benefit in practice.

## What this ADR does not change

- FSM states, transitions, and the `transitions` library usage (ADR 025).
- The analyzer/controller dependency direction (ADR 039's core decision).
- Any timing, threshold, or config value.
- Replay/capture engine interfaces (they become registry subscribers with
  identical callback signatures).

## Consequences

Positive:

- Cross-concern coupling bugs (CR-013-4, ADR 059 class) become structurally
  hard to write: a handler cannot reach another handler's state.
- Mission statistics work in replay/capture lanes for free (multi-subscriber
  FSM hook), improving ADR 044 gate observability.
- New features in this area (the active development zone per ADR 055-059)
  get an obvious, small home instead of growing `main()`.
- Each handler becomes unit-testable without booting the full loop — today
  only `_update_waiting_fallback` and `_alive_transition_disposition` are
  testable, because they are the only module-level pure functions of the
  group (the latter extracted ad hoc by ADR 061 for exactly this reason —
  finding C).

Negative / risks:

- `main.py` is the riskiest file in the repo; even pure code moves can
  perturb timing-sensitive behavior. Mitigated by per-extraction gate runs
  (rule 1) and by doing Phase 1 (low risk) first to de-risk the wiring.
- Roughly +150-250 lines of scaffolding (handler classes, context object,
  registry) against the deletion of the closure/nonlocal tangle.
- Two-phase migration leaves a window where both `set_on_*` shims and the
  registry coexist; bounded by migrating all 7 ADR 039 setters in Phase 1
  itself.

## Implementation plan (for review — not started)

| Step | Scope | Gate |
|------|-------|------|
| 1.1 | Add `GameEvent` + registry to analyzer; `set_on_*` become shims | `make test` |
| 1.2 | Migrate `main.py` wiring to `subscribe()`; stats subscribes alongside replay/capture | `make tp` |
| 1.3 | Delete dead `if/elif/else` exclusivity; assert stats JSON written in a replay run | `make tp` |
| 2.1 | Extract `WaitingFallbackHandler` (lowest coupling, partially pure already) | `make tp` |
| 2.2 | Extract `EnemyPresenceHandler` | `make tp` |
| 2.3 | Extract `AmmoEventsHandler` | `make tp` |
| 2.4 | Extract `RespawnHandler` (highest risk — ADR 059 flow; last) | `make tp` + `make tp-full` |
| 2.5 | Extract `TrackingHudHandler`; delete residual loop-locals | `make tp-full` + live session |

Estimated diff size: Phase 1 ~200 lines net; Phase 2 ~600 lines moved,
~100 net new. Each step is a separate commit; any step can be the stopping
point with the codebase left consistent.

## Validation

- `make tp` green after every step (per-step column above); `make tp-full`
  before declaring Phase 2 complete.
- New unit tests: registry semantics (multi-subscriber fan-out, duplicate-name
  rejection, exception isolation per subscriber) and one behavioral test per
  extracted handler pinning its current observable behavior.
- One full live session after Phase 2 with the session summary compared
  against a pre-refactor baseline (mission count, outcomes, respawn count,
  eject episode structure, and the `respawn_shadow` block). The reference is
  now the 2026-08-02 13:29 dual-mode session — 17 respawns, 16 matched,
  zero incorrect fallback fires — because it exercises the ADR 064 fallback
  path that Phase 2's `RespawnHandler` must preserve.
- Acceptance criterion for flipping this ADR to Accepted: all steps landed,
  gates green, and one clean production session with no behavioral deltas
  attributable to the refactor.
