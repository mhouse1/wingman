# Research 012 — Behavior Tree Composition and Wiring: A Review

| Status | Date       | Wingman Version |
|--------|------------|-----------------|
| Draft  | 2026-09-12 | 1.8.9           |

## Question

Wingman selects tactics through a py-trees priority Selector (ADR 024).
ACS Mode (`docs/hldd/011-acs-mode-hldd.md`) is about to add new tactics
(`BoresightEngage`, `WaypointObjective`) and a per-airframe branch
(`has_padlock`) to that same tree. Is there a better way of *managing* the
tree — building it, wiring its actuators, and reasoning about its
hysteresis state — before that work lands, or is the current approach
already sufficient?

## Summary of Findings

The paradigm is right and does not need to change: a py-trees priority
Selector reading one frozen `AnalyzerSnapshot` per tick, live-validated
across roughly twenty follow-on ADRs, is the correct shape for a
reactive, safety-first flight controller. ADR 024 already considered and
correctly rejected both a hand-rolled selector and an FSM.

What has drifted is the *composition and wiring* layer around it — six
concrete problems, one of them not hypothetical: ADR 137's "Third Live
Trial" section documents a currently-open bug that is a direct symptom of
finding 3 below. A sequenced remedy for all six is recorded in
`docs/adr/139-*.md` (structural items) and as ADR 137 D9 (the one real
behavior change); see "Recommended Plan" below.

## Findings

**1. The tree is built by imperative list surgery, not declared
structure.** `build_tree()` (`wingman/behavior_tree.py:581-757`) assembles
a flat `children` list, then does `children.insert(pos, leaf)` for each
opt-in tactic, finding `pos` by name specifically because an earlier
offset-based version (`len(children) - 2`) already broke priority order
once — Regroup's addition silently pushed Climb below Engage. ACS Mode
needs a composition shape this doesn't support: branching between two
leaves (`Engage` vs. a future `BoresightEngage`) at the *same* priority
slot, based on `has_padlock` read once at startup. "Insert before X"
cannot express "replace X conditionally."

**2. Hysteresis state lives in private closures, invisible to any
tooling.** `make_boundary_condition`/`make_climb_condition`
(`wingman/behavior_tree.py:245-559`) each close over a mutable `state`
dict (latch flags, blind-tick counters, min-distance-this-turn). None of
it is inspectable through py-trees' own tooling. `ConditionTactic.terminate()`
is a deliberate no-op (`behavior_tree.py:165-172`) — all real logic runs
inside `update()` via closures. `BehaviorTreeHandler` hand-rolls parallel
instrumentation (`_turn_dists`, `_turn_bearings`, `_boundary_trace`) just
to reconstruct what a leaf is already tracking internally.

**3. This has already caused a live-documented, currently-open bug.**
ADR 137's "Third Live Trial" section found Climb's `emergency` flag is
read exactly once, at leaf-selection time inside
`BehaviorTreeHandler._start_climb()` (`tick_handlers.py:1154-1182`),
which bakes the boolean into a `climb_mode(..., emergency=emergency)`
call. `climb_mode` (`controller.py:2720-2789`) spawns a thread that
freezes `emergency` as a plain local; `_run_climb_hold`'s poll loop
(`controller.py:3154-3446`) re-reads many things live every 0.25s (fuel,
incoming, telemetry, `self`-owned instance attributes) but not
`emergency` — because `ConditionTactic.update()`
(`behavior_tree.py:156-163`) only calls `start_fn` on the
FAILURE→RUNNING edge, never again while `RUNNING`. If the tree's verdict
flips mid-hold, the running actuator thread never learns about it.
Measured live twice (2026-09-10 08:15/08:16). The ADR flags this as "a
real architectural hole" needing "its own design pass."

**4. Actuator wiring is a second, parallel scatter, mirroring what
ADR 024 already fixed on the condition side.** For a tactic to actuate
live requires three independently written checks to agree: `mode:
active`, a per-tactic config flag, and a hand-written
`actuators[TACTIC_X] = (start_fn, is_running_fn)` block inside
`BehaviorTreeHandler.__init__` (`tick_handlers.py:1106-1136`). `build_tree()`
separately re-derives the same "is this tactic enabled" boolean from
`bt_cfg` to decide whether to insert the leaf at all. Nothing enforces
the two independently-computed checks agree — confirmed concretely only
for Climb and BoundaryTurn; Eject/Disengage/MissileEvade are an
intentionally different, asymmetric shape (unconditional leaf for
shadow-mode logging, separately gated actuation) and are not part of
this duplication.

**5. Shared hardware keys are arbitrated by five independent, ungoverned
inline conditions.** Direct code audit of every AFTERBURNER_KEY/
AIRBRAKE_KEY press/release site: `note_afterburner_cruise`
(`controller.py:2913-2925`) is the only one checking
`_manual_takeover_active()`/`self._climb_emergency_active`;
`_run_climb_hold`'s fuel logic has no manual-takeover check of its own;
`_run_missile_evade_hold` has its own independent fuel gate; `_start_afterburner_evade`
has no may-hold condition beyond its own deadline/cap; `eject_and_dive`'s
afterburner press is unconditional. Each new interaction between two of
these has been patched with a bespoke, narrowly-scoped exception (ADR 134
D9, then ADR 137 D3). ACS Mode's planned `BoresightEngage` (continuous
pitch+roll) will add more shared-axis contention on top of this with no
general model to extend.

**6. `BehaviorTreeHandler.tick()` mixes five concerns in ~300 lines**
(`tick_handlers.py:1231-1493+`): boundary perception/instrumentation,
snapshot assembly, tree ticking, turn bearing/range analytics, and
post-selection safety gating. This is where the next feature's
complexity will land if left as-is.

## Recommended Plan

Six independently-shippable steps: five zero-behavior-change structural
refactors, landed first, then one isolated, real behavior-change fix
(finding 3), given the house's full shadow-first / live-trial treatment
on its own.

- **Structural (fixes 1, 4, 6)** — recorded in a new umbrella ADR
  (`docs/adr/139-*.md`): replace `build_tree()`'s imperative inserts with
  a declared, ordered slot table (fixes 1); consolidate the Climb/
  BoundaryTurn enablement check into one shared predicate each fixes (4);
  extract boundary perception/instrumentation into a dedicated
  `BoundaryPerceptionHandler`, leaving `tick()` to snapshot assembly,
  tree-tick, and actuation gating (fixes 6).
- **Inspectable hysteresis state (fixes 2)** — promote
  `make_climb_condition`/`make_boundary_condition`'s closures to plain
  Python classes with named instance attributes and read-only properties,
  keeping the existing factory-function call signature so no call site
  changes.
- **Shared-key arbiter (fixes 5)** — one `Controller._may_hold_key(key,
  requester)` helper that, in its first version, exactly replicates each
  of the five sites' current inline condition — consolidation only, no
  behavior fix folded in.
- **RUNNING-leaf update channel (fixes 3)** — recorded as ADR 137 D9: add
  an optional `update_fn` to the actuator wiring, called every tick a
  tactic is already `RUNNING`; wire Climb's to push the current tick's
  `climb.emergency_active` verdict into a new Controller-owned,
  loop-readable attribute, so `_run_climb_hold` can react to an
  escalation or de-escalation mid-hold. The only step here requiring a
  live trial before being trusted at the same level as the rest of
  `climb_mode`.

Full per-step design, file/function targets, and test plan:
`docs/adr/139-*.md` (structural steps) and ADR 137 D9 (the behavior
change).

## Out of Scope

- `BoresightEngage`, `WaypointObjective`, `jet_profile` branching (ACS
  Mode / Design 011) — this review and plan only make the substrate ready
  for them.
- Consolidating the four hand-copied capture implementations
  (`_capture_boundary_frame`, `UnknownAnomalyRecorder`,
  `HealthDropoutRecorder`, `_capture_crash_frame`) — already flagged and
  deliberately deferred in ADR 137's own D5 code review.
- Fixing the safety-gating inconsistencies the shared-key audit surfaces
  (e.g. Climb's fuel logic never checking manual takeover) — a separate,
  explicitly-flagged follow-up decision, not folded into this
  consolidation.
- Merging `ConditionTactic` and the condition objects into one real
  py-trees-composited `Behaviour` subclass per tactic (genuine native
  tree-introspection) — real scope, a separate future decision if ever
  wanted.

## Related Documents

- `docs/adr/024-phase3-behavior-tree-architecture.md` — the priority
  selector this review does not change.
- `docs/adr/070-missile-evade-tactic.md`, `docs/adr/073-*-climb-tactic*.md`,
  `docs/adr/107-boundary-turn-tactic.md` — the rollout template
  (shadow-first) this plan's one behavior-changing step follows.
- `docs/adr/134-cruise-afterburner-above-fuel-floor.md` — D9's "no
  exceptions" rule, source of finding 5's growing-by-exception pattern.
- `docs/adr/137-emergency-climb-airbrake-and-crash-instrument.md` — the
  live trial that surfaced finding 3 directly; D9 is this review's fix.
- `docs/hldd/011-acs-mode-hldd.md` — the in-flight design whose needs
  motivated this review.
