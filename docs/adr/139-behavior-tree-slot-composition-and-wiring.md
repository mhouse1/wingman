# ADR 139 — Behavior Tree: Slot-Based Composition, Inspectable Hysteresis State, and Consolidated Wiring

| Status   | Date       | Wingman Version |
|----------|------------|-----------------|
| Accepted | 2026-09-13 | 1.8.9           |

## Context

`docs/research/012-behavior-tree-composition-and-wiring-review.md` reviewed
how the ADR 024 behavior tree (`wingman/behavior_tree.py`) is composed and
wired, ahead of ACS Mode (`docs/hldd/011-acs-mode-hldd.md`) adding new
tactics and a per-airframe branch to the same tree. The priority-selector
paradigm itself is not in question — this ADR covers five structural
findings from that review, all zero-behavior-change by construction. A
sixth finding (Climb's `emergency` flag going stale mid-hold) is a real
behavior change and is tracked separately as a new decision on
[ADR 137](137-emergency-climb-airbrake-and-crash-instrument.md), not here.

## Decision

**D1. Replace `build_tree()`'s imperative `children.insert(...)` calls with
a declared, ordered slot table.** `build_tree()`
(`wingman/behavior_tree.py:581-757`) builds a flat list and inserts opt-in
leaves (Climb, BoundaryTurn, Regroup) by searching for a named neighbor's
index — done that way specifically because an earlier offset-based version
(`len(children) - 2`) silently broke priority order when Regroup was added.
Replacing it with a table of `(name, build_fn)` pairs, each `build_fn(ctx) ->
Behaviour | None`, makes the ordering a single piece of declared data instead
of an imperative sequence of list mutations, and gives ACS Mode a place to
put a same-slot conditional (`Engage` vs. a future `BoresightEngage` on
`has_padlock`) that "insert before X" cannot express. `_slot_engage` itself
is unchanged in this ADR — it stays `ConditionTactic(TACTIC_ENGAGE,
has_contacts)` unconditionally; only the shape that could later support a
branch is established here.

**Correction during implementation**: the original draft of this decision
stated that the slot table's order would define both priority rank and
build order together. Reconstructing the actual current dependency showed
that claim is false as stated: `BoundaryTurn` outranks `Climb` in priority
(it sits above Evade; Climb sits just above Engage) but its condition reads
`Climb`'s `emergency_active` closure, so `Climb` must be *built* before
`BoundaryTurn` regardless of their priority order. Priority order and build
order are therefore declared **separately**: `_PRIORITY_ORDER`
(`wingman/behavior_tree.py`) is the single flat list defining selector rank;
`_build_slots` builds leaves in whatever fixed sequence satisfies real data
dependencies (Climb before BoundaryTurn today), and `build_tree` assembles
the final children list by walking `_PRIORITY_ORDER` and looking each name
up in the built set. The hard rule is on the dependency, not on the
ordering: a slot may only read another slot's build-time output if that
slot is built first in `_build_slots`, independent of where either sits in
`_PRIORITY_ORDER`.

Verified behavior-preserving by a golden-master test
(`test_build_tree_child_order_across_flag_matrix`,
`tests/test_behavior_tree.py`) capturing the pre-refactor selector's child
order across the full `climb_enabled × boundary_configured ×
regroup_enabled × sustain_enabled` flag matrix (16 cases), landed and green
against the *old* implementation before D1's code changed, and required to
stay green afterward with zero edits.

**D2. Consolidate the Climb and BoundaryTurn enablement check into one
predicate each, shared between tree-insertion and actuator-wiring.**
Getting Climb or BoundaryTurn to actuate live today requires two
independently written boolean expressions to agree — one in `build_tree()`
deciding whether to insert the leaf at all, one in
`BehaviorTreeHandler.__init__` (`tick_handlers.py:1106-1136`) deciding
whether to wire its actuator. `climb_tactic_enabled(bt_cfg)` and
`boundary_tactic_enabled(bt_cfg)`, two small module-level pure functions in
`wingman/behavior_tree.py`, replace both call sites for these two tactics
only.

**Scoped to Climb and BoundaryTurn — not Eject, Disengage, or
MissileEvade.** Those three leaves are unconditionally present in the tree
(so shadow mode can log their selection while disabled) and separately
gated on `self.active` for actuation; there is no second "is this tactic in
the tree" check for them to consolidate against, and applying D2's pattern
there would delete their shadow-logging-while-disabled capability. That is
an explicit non-goal, not an oversight.

**D3. Promote `make_climb_condition`/`make_boundary_condition`'s hysteresis
state from closure-captured dicts to instance attributes on plain Python
classes**, each exposing the relevant fields as read-only `@property`
(e.g. `ClimbCondition.active`, `ClimbCondition.emergency_active`). The
factory functions (`make_climb_condition`, `make_boundary_condition`) keep
their exact current signatures and return the new class instances, so every
existing call site — production and test — is unchanged.

**Deliberately plain classes, not `py_trees.behaviour.Behaviour`
subclasses.** `Behaviour`'s lifecycle (`update(self) -> Status`, no
arguments, meant to be ticked as a composited tree child) does not match
these objects' actual contract (`__call__(snapshot) -> bool`, called
directly by `ConditionTactic`, never composited into the selector).
Subclassing would add an unused inheritance chain without making anything
visible to py-trees' own introspection tooling, since these objects are
never tree nodes. The property name `emergency_active` is chosen to match
exactly what `_climb_emergency_fn`'s `getattr(_e, "emergency_active",
False)` (`behavior_tree.py:702-703`) already reads — that closure needs no
change.

Verified behavior-preserving by the full existing `tests/test_behavior_tree.py`
suite (`TestClimbDebounce`, `TestTimeToGroundRecovery`,
`TestDiveRecoveryRespawnGuard`, every boundary-condition test —
representing the ADR 073/086/107/113/120/122/125/126/132/138 tuning) passing
with zero test edits.

**D4. Introduce `Controller._may_hold_key(key, requester) -> bool`** as the
single named point where AFTERBURNER_KEY/AIRBRAKE_KEY arbitration logic
lives, consolidating five currently-independent inline conditions
(`note_afterburner_cruise`, `_run_climb_hold`'s fuel logic,
`_run_missile_evade_hold`, `_start_afterburner_evade`, `eject_and_dive`).

**Discipline: replicate, do not fix.** Direct audit found these five sites
do not agree today — only `note_afterburner_cruise` checks
`_manual_takeover_active()`/`self._climb_emergency_active`;
`_run_climb_hold`'s fuel logic has no manual-takeover check at all;
`_run_missile_evade_hold` has its own independent fuel gate;
`_start_afterburner_evade` has no may-hold condition beyond its own
deadline/cap; `eject_and_dive`'s press is unconditional. `_may_hold_key`'s
first version reproduces each site's current effective behavior exactly,
as its own case. Any of these inconsistencies being worth fixing (e.g.
giving Climb's fuel logic a manual-takeover check it lacks today) is a
**separate, future decision requiring its own live validation** — never
folded into this consolidation. `_climb_key` (`controller.py:2976`, which
has no gating today) is intentionally left as-is; `_may_hold_key` is called
at each of the five sites explicitly, not injected underneath it.

**D5 (implemented 2026-09-12). Extracted boundary perception and
instrumentation out of `BehaviorTreeHandler.tick()` into a new
`BoundaryPerceptionHandler`**, matching the file's existing
`EnemyPresenceHandler`/`UnknownAnomalyRecorder`/`HealthDropoutRecorder`/
`RespawnHealthStallRecorder` naming precedent. Owns detection, respawn-settle
suppression, median-filtering, blind-frame capture, crossing/approach
instrumentation, and per-turn bearing/range analytics — six methods total
(`perceive`, `_median_boundary`, `instrument_boundary`, `maybe_capture_blind`,
`_capture_boundary_frame`/`_capture_rtb_frame`/`_on_rtb_confirmed`,
`record_turn_tick`), all of the moved state (`_boundary_reading`,
`_boundary_recent`, `_boundary_trace`, `_rtb_*`, `_blind_*`, `_turn_dists`,
`_turn_bearings`, `_session_start`, …), and its own `analyzer` reference.

**One deviation from the original plan, found during implementation**: rather
than keeping raw detection in `tick()` and moving only the median filter,
the whole read → respawn-settle-suppress → median-filter → blind-capture
pipeline consolidated into one `perceive(frame, now, is_respawning)` method
returning `(dist, forward, lateral, near)`. Splitting it across the
class boundary would have meant `tick()` still owning the respawn-settle
state (`_respawn_settle_until`) while the median filter it feeds lived
elsewhere — two collaborators sharing one hysteresis state machine, the
exact anti-pattern D1's slot table exists to avoid on the tree side. `_start_boundary_turn`
reads the current reading via a new `BoundaryPerceptionHandler.reading`
property rather than reaching into a private attribute directly.
`BehaviorTreeHandler.tick()` now keeps only snapshot assembly,
`self._tree.tick()`, and `_may_fly`/`stop_boundary_turn()` actuation gating,
calling into the collaborator at exactly two points: `perceive(...)`
pre-selection and `instrument_boundary(...)` / `record_turn_tick(...)`
post-selection.

`perceive`, `instrument_boundary`, `maybe_capture_blind`, and
`record_turn_tick` are public (no leading underscore) since they now cross
an object boundary — matching this file's existing convention for
cross-object calls (`tick`, `on_state_change`, `arm_absence_clock`).
`_median_boundary`, `_capture_boundary_frame`, `_capture_rtb_frame`, and
`_on_rtb_confirmed` stay private: they are only ever called from within
`BoundaryPerceptionHandler` itself.

The 34 `tests/test_tick_handlers.py` call sites that constructed
`BehaviorTreeHandler.__new__(...)` and touched boundary state directly (one
shared `_boundary_handler`/`_capture_handler` fixture plus three test
classes' own `_h()` helpers) now construct
`BoundaryPerceptionHandler.__new__(...)` instead, same hand-set-attribute
idiom, same assertions — confirmed as the bulk of the actual work, exactly
as anticipated. Two structural tests
(`test_boundary_readings_are_suppressed_after_a_respawn`,
`test_boundary_turn_stays_live_during_a_hold`) that asserted on
`inspect.getsource(BehaviorTreeHandler)` needed their target class (or, in
the second case, the specific substring — the turn-recording code they
originally anchored on moved to `record_turn_tick`, while the actuation-gating
code the test actually intends to guard stayed in `BehaviorTreeHandler`)
updated to match.

## Non-Goals

1. `BoresightEngage`, `WaypointObjective`, or any `jet_profile` branching
   (ACS Mode / Design 011) — D1 only proves the slot shape can express a
   same-slot conditional later; it does not add one.
2. Fixing Climb's mid-hold `emergency` staleness (ADR 137's "Third Live
   Trial" gap) — tracked as a new decision on ADR 137, which already
   anticipates this exact fix and accumulates its own D-numbered decisions.
   That change alone requires shadow-first live validation; nothing in this
   ADR does.
3. Fixing any of the safety-gating inconsistencies D4's audit surfaced.
4. Consolidating the four hand-copied capture implementations
   (`_capture_boundary_frame`, `UnknownAnomalyRecorder`,
   `HealthDropoutRecorder`, `_capture_crash_frame`) — already flagged and
   deliberately deferred in ADR 137's own D5 code review.
5. Merging `ConditionTactic` and the D3 condition objects into one real
   py-trees-composited `Behaviour` subclass per tactic (genuine native
   tree-introspection) — a separate, larger future decision if ever wanted.

## Rollout Plan (completed 2026-09-13)

D1–D5 were each independently shippable and behavior-preserving by
construction; none required a live trial, per this ADR's own bar — landed in
order (D1's golden-master test first, as a preparatory non-refactoring
commit, then D2, D3, D4, D5), each gated by `make lint && make test` and the
ADR 044/045 replay gates (`make rr-path1-gate` / `make rr-live-path1-gate`),
full suite green throughout with zero regressions outside one pre-existing,
unrelated corpus-threshold failure (`test_corroborated_span.py`). D5 also got
an unplanned bonus: a real 34-minute live session confirmed every extracted
method (`perceive`, `_median_boundary`, `instrument_boundary`,
`record_turn_tick`) firing correctly with zero exceptions — stronger
evidence than the "no live trial needed" bar this ADR set for itself.
`docs/architecture.md`'s "Behavior Tree" section was updated to match once
D1 and D3 landed.

## Related Documents

- `docs/research/012-behavior-tree-composition-and-wiring-review.md` — the
  review this ADR implements.
- `docs/adr/024-phase3-behavior-tree-architecture.md` — the priority
  selector this ADR extends, not replaces (Accepted; not modified).
- `docs/adr/070-missile-evade-tactic.md`, `docs/adr/073-*-climb-tactic*.md`,
  `docs/adr/107-boundary-turn-tactic.md` — source of the hysteresis tuning
  D3's characterization tests protect.
- `docs/adr/134-cruise-afterburner-above-fuel-floor.md` — D9's "no
  exceptions" rule; D4's motivating example of the exception-by-exception
  pattern it replaces the *mechanism* for (not the policy).
- `docs/adr/137-emergency-climb-airbrake-and-crash-instrument.md` — carries
  the one real behavior change (Climb's mid-hold emergency refresh) this
  ADR deliberately excludes.
- `docs/hldd/011-acs-mode-hldd.md` — the in-flight design D1's slot shape is
  built to accommodate.
