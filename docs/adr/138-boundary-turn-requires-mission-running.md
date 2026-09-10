# ADR 138 — BoundaryTurn Requires a Running Mission, Not Just a Cleared Respawn

| Status | Date       | Wingman Version |
|--------|------------|-----------------|
| Draft  | 2026-09-10 | 1.8.9           |

## Context

Operator report: the aircraft did not fly the spawn heading straight after a
respawn, despite ADR 132's turn guard existing precisely to guarantee that.
Measured directly from `wingman.log` for the last respawn of the 2026-09-10
02:xx session:

```
03:03:22,430  RESPAWN DETECTED — mission cancelled
03:03:26,522  respawn=False in the BT snapshot — BoundaryTurn selected AND
              starts actuating immediately: "boundary turn rolling right"
                          ↑ mission=False here
03:03:28,026  HEALTH ALIVE — mission_j20 restarts — TURN GUARD arms (10s)
03:03:38,534  boundary turn completes its full 12s cap:
              "nose -90..+90deg (swing 180)"
```

The boundary turn started **1.5 seconds before the turn guard armed**, ran
its full 12-second cap regardless of the guard arming shortly after, and
delivered a 180-degree nose swing — the exact "circling instead of joining
the fight" ADR 132 exists to prevent, via a timing hole ADR 132 did not
close.

Root cause: ADR 132 D2 arms the guard inside `mission_j20`
(`Controller.arm_turn_guard()`, `controller.py:4028`), on the explicit
reasoning that "battle entry and every respawn restart both run through
it... one arm point covers both cases and cannot drift apart from them."
That's true for *when the mission restarts* — but `make_boundary_condition`
(`behavior_tree.py`) does not wait for the mission to restart. It already
has one respawn-safety check, added earlier (2026-09-04, same file,
`behavior_tree.py:305-314`) after a turn held through a respawn and
re-selected a second later: it blocks while `is_respawning` is true. That
check does its job — but `is_respawning` clears (`False`) as soon as the
respawn *screen* clears, which is measurably earlier than the mission
actually restarting: `RespawnHandler`'s own ADR 059 stability window
(confirmed clear for `respawn_clear_stability_s`, default ~1.5s) deliberately
waits before declaring the life "alive" and calling `restart_last_mission()`.
`BoundaryTurn` has no reason to know about that window — nothing in its
condition reads `mission_running` at all — so it walks straight through the
gap between "screen cleared" and "mission actually running again."

`make_sustain_climb_condition` already gates on `snapshot.mission_running`
(`behavior_tree.py:556`, "sustain is mission doctrine") — this is not a new
pattern for this codebase, just one `BoundaryTurn` never picked up.

## Decision

**D1. `make_boundary_condition`'s existing respawn-safety check
(`behavior_tree.py:311-314`) now also requires `snapshot.mission_running`.**
One-line extension of an existing, already-tested guard — not a new
mechanism:

```python
if getattr(snapshot, "is_respawning", False) or \
        snapshot.game_state != GameState.GAME_BATTLE or \
        not snapshot.mission_running:
    _reset()
    return False
```

`mission_running` is a required (non-optional) field on `AnalyzerSnapshot`,
populated every tick from `Controller.is_mission_running()`, so this needed
no new plumbing. The existing `_reset()` call means the same guarantee
`test_a_respawn_clears_a_held_turn` already covers for `is_respawning`
(state doesn't merely mask for one tick, it's actually cleared) now also
holds for the mission-not-running case.

**D2. Not a change to `arm_turn_guard()`'s own timing.** ADR 132 D2's "one
arm point" reasoning is intact — the guard still arms exactly where it did.
This fix closes the gap from the other side: `BoundaryTurn` now can't
select or actuate *at all* before a mission is running, so by the time it's
ever eligible, `mission_j20` (and therefore the guard) has already started.
The 1.5s window still exists; nothing can turn the aircraft during it
anymore, boundary-turn or otherwise, which is a strictly larger guarantee
than the turn guard alone provided.

## Non-Goals

1. **Not a fix to any other tactic that might have the same gap.**
   `BoundaryTurn` is the one measured firing in this window; this ADR does
   not audit `Engage`, `Regroup`, `AttackSupport`, or any other leaf for the
   same missing check. `MissileEvade`'s exemption from `mission_running`
   gating is deliberate (ADR 070/SAF-001, survival response) and untouched.
2. **Not a change to the ADR 059 respawn-clear stability window.** That
   window is a valuable, deliberate anti-flap protection (avoid restarting
   into a flickering respawn overlay); this ADR does not shorten or remove
   it, it just ensures nothing can command a turn while it's open.
3. **Not a retroactive audit of how many prior "circling" reports this
   explains.** Plausible that some fraction of ADR 132's own "184 boundary
   turns suppressed" soak data, and any turn-guard-adjacent complaints since,
   trace back to this same gap — not quantified here.

## Open Questions

1. Should `mission_running` become part of `BoundaryTurn`'s condition
   signature more visibly (a documented precondition) rather than folded
   into the same `if` as the respawn/game-state checks, now that it's doing
   meaningfully different work (closing a *timing* gap, not a *stale-state*
   one)? Left as a single `if`, matching the existing code's own style,
   until there's a reason to split it.
2. Is there a broader principle here — should EVERY leaf below `RespawnWait`
   require `mission_running` by default, with `MissileEvade`-style
   exemptions opt-in rather than opt-out? That would be a larger refactor
   than this ADR's single-line fix and is not attempted here.

## Validation

- New unit test `test_mission_not_running_clears_the_turn`
  (`tests/test_behavior_tree.py`), mirroring
  `test_a_respawn_clears_a_held_turn`'s exact shape for the new condition.
- Full existing `make_boundary_condition`/`_bcond` test suite
  (`tests/test_behavior_tree.py`) passes unmodified — every existing
  snapshot fixture (`make_snap`) already defaults `mission_running=True`,
  so this fix has zero effect on any test that didn't already exercise the
  new branch.
- Live re-validation (post-fix): not yet run. The next session with this
  deployed is the actual test of whether the 1.5s gap stops producing
  unguarded turns.

## References

- `docs/adr/132-fly-the-spawn-heading-first.md` — the turn guard this gap
  was found while investigating; D2's "one arm point" reasoning this ADR
  closes the last mile of, from the tactic side rather than the arm-timing
  side.
- `docs/adr/107-boundary-turn-tactic.md` — `BoundaryTurn`'s own design;
  `make_boundary_condition` lives in its lineage.
- `behavior_tree.py:305-310` (2026-09-04 respawn-latch fix) — the existing
  guard this ADR extends rather than replaces.
- `behavior_tree.py:556` (`make_sustain_climb_condition`) — prior art for
  gating a tactic's selection on `mission_running`.
