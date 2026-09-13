# Research 013 — Behavior Tree Tooling and Composability

| Status | Date       | Wingman Version |
|--------|------------|-----------------|
| Draft  | 2026-09-13 | 1.8.9           |

## Question

The ADR 024 tree's own shape is sound (Research 012) — but the operator's
mental model of how it composes lives mostly in his head plus a hand-
maintained section of `docs/architecture.md`. A new developer (or a future
session) reconstructing it has to read a large document rather than see it.
Are there tools for working with behavior trees that let someone view the
whole tree at a glance, and add a new behavior by branching off an existing
one — closer to a binary-tree copy-and-rewire workflow than to reading prose?

## Summary of Findings

The GUI editors people usually mean by "behavior tree tooling" — Groot /
Groot2 — pair with BehaviorTree.CPP, a different (C++) library, not
`py_trees`. There is no mainstream drag-and-drop editor for `py_trees`
outside the ROS ecosystem, and that ecosystem's own viewer
(`py_trees_ros_viewer`) needs a ROS graph this project doesn't have.
`py_trees` is code-first by design; migrating frameworks purely to gain an
editor would discard ADR 024's live-validated foundation for a UI
convenience, which is not a trade worth making.

What closes most of the actual gap, without a framework change:

1. `py_trees.display` already ships a structural renderer capable of
   walking the real, constructed tree — so "does the diagram match the
   code" becomes a question you answer by running a command, not by
   re-reading two documents side by side.
2. The codebase's existing `_build_slots` pattern (ADR 139 D1) — one
   self-contained function per priority slot, returning a subtree — already
   *is* the copy-a-branch workflow being asked for. It was simply never
   written down as a recipe separate from the ADR's own decision record.
3. Groot2's headline differentiator over a static diagram — watching node
   status live while the tree runs — turned out to be a `py_trees` built-in
   (`show_status=True`, reading each node's persisted `.status`), not a
   capability the framework lacks. Wiring that in (Finding 7) closes the
   specific "harder to track as it grows" pain the migration question was
   really about, at a fraction of the cost — see "Cost of Migrating" below
   for the full comparison.

## Findings

**1. No GUI editor exists for `py_trees` comparable to Groot.** Groot/Groot2
are built against BehaviorTree.CPP's XML tree format and node-registration
model; they don't consume `py_trees` objects and there is no maintained
bridge between the two projects. The only widely-used `py_trees`
visualization tooling is `py_trees.display` (`ascii_tree`, `unicode_tree`,
`render_dot_tree`) and `py_trees_ros_viewer`, a Qt tool built for ROS's
pub/sub blackboard — not applicable without ROS.

**2. `render_dot_tree`'s PNG/SVG output needs the graphviz `dot` binary,
which is absent on this machine** (`which dot` → not found; `pydot` itself
is installed, 4.0.1, but can only serialize `.dot` text, not rasterize it
without the binary). Generating Mermaid directly from the tree sidesteps
the missing dependency entirely and matches this project's own
documentation convention (`CLAUDE.md`: "Always use Mermaid for diagrams").

**3. `build_tree(bt_cfg, clock=time.time, actuators=None,
regroup_enabled=False)` (`wingman/behavior_tree.py:960`) is a pure
construction function** — no analyzer reference, no live game, no actuators
required for a selection-only structural build. Standalone rendering is
therefore trivial: load `config.yaml`, call `build_tree`, walk `tree.root`.

**4. The tree already nests composites, not just flat leaves.** Running the
new renderer (Finding 6) shows three of the eleven top-level slots
(`BoundaryTurn`, `Evade`, `Disengage`) are decorators wrapping a named
condition leaf, not bare leaves. A "branch" in this tree is not always one
flat node — a slot builder can already return an arbitrarily deep subtree,
which is exactly the shape ACS Mode's planned `BoresightEngage` ternary will
need when it lands.

**5. The `_build_slots` / `_PRIORITY_ORDER` pattern from ADR 139 D1 already
is a copy-a-branch workflow — it just has no cookbook.** Each slot
(`_build_climb_slot`, `_build_boundary_slot`, etc., all in
`wingman/behavior_tree.py`) is a self-contained function; adding a tactic
is: copy the nearest similar slot function, adjust its condition/actuator,
register its name in `_PRIORITY_ORDER`. Nothing about this requires
understanding the rest of the file. This has been true since ADR 139
landed but was never written down separately from that ADR's own decision
record, which is written for "why," not "how do I add one."

**6. A prototype renderer (`scripts/render-behavior-tree.py`) confirms both
directions work.** `py_trees.display.ascii_tree(tree.root)` against the
live `build_tree()` output produces a correct structural dump (decorators
render with the `-^-` marker, composites nest correctly); a short recursive
walk over `.children` emits a valid Mermaid `flowchart TD` from the same
object. Wired to `make tree`.

**7. Live per-node status is a `py_trees` primitive, not a Groot2
exclusive — implemented.** `py_trees.display.ascii_tree(root,
show_status=True)` reads each node's own persisted `.status` attribute; no
`SnapshotVisitor` or extra bookkeeping is needed. `wingman/behavior_tree.py`
now exposes `tree_status_text(tree)`, and `BehaviorTreeHandler.tick()`
(`wingman/tick_handlers.py`) logs it at DEBUG on the same edge that already
logs the `BT[...]: tactic X → Y` selection change — not every tick, only
when the selection actually moves, matching this codebase's existing
"log the transition, not the steady state" convention (e.g. ADR 137 D9).
Verified output, ring_long=1 (Engage should win over the two decorator-
wrapped slots and the unreached fallback):

```
[o] TacticSelector [*]
    --> Idle [x]
    --> RespawnWait [x]
    --> Eject [x]
    --> MissileEvade [x]
    -^- Evade [x]
        --> EvadeCondition [x]
    -^- Disengage [x]
        --> DisengageCondition [x]
    --> Engage [*]
    --> AttackSupport [-]
```

`[*]` RUNNING, `[x]` FAILURE, `[-]` INVALID (never reached) — the fallback
correctly shows INVALID rather than FAILURE, because the selector
short-circuited before ticking it. Covered by
`test_tree_status_text_names_the_running_leaf` and
`test_tree_status_text_before_any_tick_does_not_raise` in
`tests/test_behavior_tree.py`.

## Cost of Migrating to BehaviorTree.CPP / Groot2

Raised directly: does the tree's anticipated growth (ACS Mode,
`docs/hldd/011-acs-mode-hldd.md` — three new pieces plus a capability flag,
landing on today's eleven slots) justify adopting BT.CPP/Groot2 for its
more mature editing and monitoring tools, given that easier
troubleshooting pays off compounding as the tree grows? Weighed directly:

**What Groot2 actually offers over today's `py_trees` tooling.** A mature
visual editor (drag-and-drop composition, subtree reuse) and live
monitoring with replay. Finding 7 shows the monitoring half is not unique
to it — `py_trees` has the same primitive. What remains genuinely Groot2-
only is the graphical editing surface itself.

**What adopting it would cost:**

- Rewrite all eleven tactics, the `ClimbCondition`/`BoundaryCondition`
  hysteresis classes, `MinimumHold`, `ConditionTactic`, and the
  `_may_hold_key` shared-key arbiter (ADR 139 D4) — every piece currently
  validated in Python against a Python `AnalyzerSnapshot`/`Controller`.
- BT.CPP is C++; wingman's OCR/analyzer/controller stack is entirely
  Python. Closing that gap means either BT.CPP's Python bindings (far less
  mature and far less used than the C++ core — a new, thinly-proven
  dependency for a system that flies unattended for hours) or a
  cross-process bridge, which adds IPC and a new failure mode into a
  1.5-second real-time control loop that the house's whole rollout
  discipline (shadow-mode first, then a live trial) exists to avoid
  destabilizing.
- Reopens two settled, live-validated decisions — ADR 024 (the original
  selector choice) and the just-finished ADR 139 (D1-D5, full replay-gate
  coverage, a live trial) — for a tooling reason, not a functional one.
- Groot2's fuller monitoring/replay feature set sits behind a paid tier,
  on top of the engineering cost above.

**Verdict.** The specific pain driving the question — "harder to track as
it grows," i.e. not being able to see what the tree is doing — is closed by
Finding 7 for about an hour of work, zero new dependencies, and zero risk
to the validated core. ACS Mode's actual growth (three pieces, one flag) is
linear addition via the existing slot pattern (Job Aid 012), not a
structural change in kind that would make the tree qualitatively harder to
reason about. The migration's remaining unique value — graphical editing —
is a convenience, not a capability gap, and it does not clear the cost
above for a solo-maintained project already carrying a validated
architecture. Re-evaluate only if the tree's *editing* workflow (not its
visibility) becomes the actual bottleneck — e.g., if slot functions stop
being able to express something ACS Mode or a later design needs, which
Finding 4 (decorators nesting inside slots) suggests won't be the case for
`BoresightEngage`.

## Out of Scope

- Installing graphviz for `render_dot_tree` PNG/SVG output — the new
  script's Mermaid output supersedes the need for it on a project that
  already standardizes on Mermaid.

## Recommended Plan

1. Ship `scripts/render-behavior-tree.py` and `make tree` (done) so the
   diagram question is answerable by running a command instead of
   re-reading `docs/architecture.md` and `behavior_tree.py` side by side.
2. Add `docs/job-aids/012-add-a-new-behavior-tree-tactic.md` (done): copy
   the nearest slot function, rewire its condition/actuator, insert its
   name into `_PRIORITY_ORDER`, regenerate the diagram with `make tree` to
   confirm placement, run the golden-master matrix test
   (`tests/test_behavior_tree.py`) to confirm nothing else moved.
3. Wire the live-status view (done — Finding 7): `tree_status_text()` in
   `wingman/behavior_tree.py`, logged at DEBUG on the selection-change edge
   in `BehaviorTreeHandler.tick()`.
4. Do not migrate to BehaviorTree.CPP/Groot2 — see "Cost of Migrating"
   above. No architecture change, no new runtime dependency, no framework
   migration.

## Related Documents

- `docs/research/012-behavior-tree-composition-and-wiring-review.md` — the
  composition/wiring review this one continues from.
- `docs/adr/139-behavior-tree-slot-composition-and-wiring.md` — the
  `_build_slots`/`_PRIORITY_ORDER` decision this doc's recipe documents how
  to use.
- `docs/adr/024-phase3-behavior-tree-architecture.md` — the original
  py-trees selector decision.
- `docs/architecture.md` (Behavior Tree section) — the hand-maintained
  narrative this tooling complements rather than replaces.
