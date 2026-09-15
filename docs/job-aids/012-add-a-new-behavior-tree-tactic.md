# Job Aid 012 — Add a New Behavior Tree Tactic

| Status | Date       | Wingman Version |
|--------|------------|-----------------|
| Draft  | 2026-09-13 | 1.8.9           |

## Overview

The ADR 024 selector (`wingman/behavior_tree.py`) is built from one
self-contained function per priority slot — `_build_climb_slot`,
`_build_boundary_slot`, and so on — registered by name in a single ordered
tuple, `_PRIORITY_ORDER` (ADR 139 D1). Adding a tactic is copy-a-slot,
rewire, register — not an edit to `build_tree()` itself, and not something
that requires reading the whole file. This job aid is that recipe, written
down separately from ADR 139's own decision record (which explains *why*
the slot shape exists, not *how* to use it — see Research 013).

Run `make tree` before and after your change — it renders the tree
`build_tree()` actually produces, as a Mermaid diagram and an ASCII dump, so
you can see where your new slot landed without reading code to reconstruct
it.

## Step 1 — Pick the nearest existing slot to copy

Two shapes exist today:

- **Plain leaf**, no hysteresis, no hold — `_build_idle_slot`,
  `_build_engage_slot`, `_build_attack_support_slot`. Copy one of these if
  your tactic is a single condition function with no state.
- **Opt-in leaf with a config flag, an actuator, and/or a hold** —
  `_build_climb_slot` (config-gated, actuator, hysteresis condition object),
  `_build_disengage_slot` (`MinimumHold` wrapper, actuator), `_build_regroup_slot`
  (config-gated, no actuator). Copy the one that matches most of what you
  need — it is easier to delete an unneeded piece (the `MinimumHold` wrapper,
  the actuator kwargs) than to reconstruct one from a bare leaf.

If your tactic needs a real sub-branch (more than one condition composed
together — e.g. ACS Mode's planned Engage-vs-BoresightEngage choice), note
that a slot function can return **any** `py_trees` subtree, not just one
leaf — `_build_evade_slot`/`_build_disengage_slot` already return a
`MinimumHold` wrapping a `ConditionTactic`, and `make tree`'s ASCII dump
shows this nesting today (the `-^-` decorator marker under `BoundaryTurn`,
`Evade`, `Disengage`). A `py_trees.composites.Selector` or `Sequence` is
equally legal as a slot's return value.

## Step 2 — Write the slot function

```python
def _build_my_tactic_slot(ctx: "_BuildContext"):
    if not my_tactic_enabled(ctx.bt_cfg):   # skip this line for an always-on leaf
        return None
    my_cfg = ctx.bt_cfg.get("my_tactic", {}) or {}
    my_fns = ctx.actuators.get(TACTIC_MY_TACTIC)
    kwargs = {}
    if my_fns is not None:
        kwargs = {"start_fn": my_fns[0], "is_running_fn": my_fns[1]}
    return ConditionTactic(TACTIC_MY_TACTIC, my_condition_fn, **kwargs)
```

- Add `TACTIC_MY_TACTIC = "MyTactic"` alongside the other `TACTIC_*`
  constants near the top of `wingman/behavior_tree.py`.
- If your tactic is config-gated, add a small pure predicate function next
  to `climb_tactic_enabled`/`boundary_tactic_enabled` (ADR 139 D2) — one
  function, used both here and at the `tick_handlers.py` actuator-wiring
  site, so the enablement check is never written twice.
- **Cross-slot dependencies are the one sharp edge.** If your condition
  needs to read another slot's live state (the way `_build_boundary_slot`
  reads `ctx.climb_emergency_fn`, set by `_build_climb_slot`), add a field
  to `_BuildContext` for it and set it as a side effect of the *producing*
  slot's build function — then make sure `_build_slots` (Step 4) builds the
  producer before the consumer. Priority order and build order are allowed
  to differ (they already do, for Climb/BoundaryTurn) — but a dependency
  that isn't reflected in `_build_slots`'s explicit build sequence will read
  a stale or `None` value. Do not take a dependency that isn't already
  exposed via `_BuildContext` without adding the field for it.

## Step 3 — Register the priority slot

Add your tactic's name to `_PRIORITY_ORDER` at the rank you want:

```python
_PRIORITY_ORDER = (
    TACTIC_IDLE, TACTIC_RESPAWN_WAIT, TACTIC_EJECT, TACTIC_MISSILE_EVADE,
    TACTIC_BOUNDARY_TURN, TACTIC_EVADE, TACTIC_DISENGAGE, TACTIC_CLIMB,
    TACTIC_MY_TACTIC,  # <-- your new rank, between Climb and Engage
    TACTIC_ENGAGE, TACTIC_REGROUP, TACTIC_ATTACK_SUPPORT,
)
```

This is the **only** place priority rank is declared. Do not also try to
express rank via build order in `_build_slots` — they are intentionally
independent (see the `_BuildContext` docstring).

## Step 4 — Wire the build call

Add your slot to `_build_slots`, in a position satisfying any dependency
from Step 2 — not necessarily the same position as `_PRIORITY_ORDER`:

```python
my_leaf = _build_my_tactic_slot(ctx)
if my_leaf is not None:
    built[TACTIC_MY_TACTIC] = my_leaf
```

If your tactic has no cross-slot dependency, it can simply join the first
dict literal alongside `TACTIC_IDLE`, `TACTIC_ENGAGE`, etc.

## Step 5 — Wire the actuator (if any)

If your tactic actuates a `Controller` method, add it to the `actuators`
dict `BehaviorTreeHandler.__init__` builds in `wingman/tick_handlers.py`,
following the existing `actuators[TACTIC_CLIMB] = (self._start_climb,
ctrl.is_climbing, self._update_climb)` pattern — a 2-tuple
(`start_fn`, `is_running_fn`) or, if your tactic needs to react to state
changes while it's already `RUNNING` (ADR 137 D9's `update_fn` channel), a
3-tuple with the update function as the third element.

## Step 6 — Verify

```bash
make tree   # confirm your leaf appears at the intended rank
make test   # golden-master matrix test + everything else
```

`tests/test_behavior_tree.py::test_build_tree_child_order_across_flag_matrix`
is the specific regression gate for "did this change move anyone else's
priority rank" — it hardcodes expected child order across a
climb/boundary/regroup/sustain flag matrix. It will need a new expected-order
entry for your addition; that edit is expected, not a sign something broke.
If it fails anywhere you didn't touch, something moved that shouldn't have.

Once the tactic actuates something real, follow the house's shadow-first
discipline from there (shadow-mode logging before actuation, then a live
trial) — this job aid only covers getting the leaf correctly composed and
wired into the tree.
