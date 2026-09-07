# ADR 131 — The Survival Hold Must Still Fire

| Status | Date       | Wingman Version |
|--------|------------|-----------------|
| Accepted | 2026-09-06 | 1.8.8         |

## Context

`mission_loiter` is the survival hold: climb to a holding altitude and orbit
there, on the objective that survival needs exactly two things — be high, and
keep turning. It is invoked by the operator with `'y'`, and re-invoked across
respawns by `restart_last_mission`.

It never pressed a weapon key.

`mission_j20` starts the search-and-destroy loops — padlock plus weapon fire —
and holds the mission state while the behaviour tree owns tactics.
`mission_loiter` starts neither. Both take `_mission_lock`, so they are mutually
exclusive: for as long as a hold owns the aircraft, nothing is firing.

Measured 2026-09-06 in the 1h07m session ending 04:28: **`'y'` pressed six
times**, each press parking an aircraft with missiles aboard. The hold is not a
rare emergency control; it is in routine use, and every use suspended the
aircraft's contribution until the round ended or the operator intervened.

The rack never emptying has a second consequence. `is_missiles_empty` is the
Eject trigger, so an aircraft that never fires never ejects, never dies, and
never respawns rearmed. The hold does not merely pause the mission — it removes
the airframe from the round while keeping it alive, which is the least useful of
the available outcomes.

## The alternative that was rejected

The other route to the same end is to **defer the hold** — let `mission_j20` run
until the rack is dry and only then loiter. It is rejected, and the reasons are
already written down as ADRs:

- **ADR 111.** An eject already in progress kept diving through a loiter request
  and hit the ground eleven seconds later. "The operator asked to stay alive; a
  manoeuvre whose purpose is to end the aircraft cannot outrank that."
- **ADR 123.** A hold started at 139 m in a -74 degree dive at 2652 KPH needed a
  blocking pull-up *before the loop, before the tree, before anything else gets a
  vote*. It hit the ground in five seconds otherwise.

Both exist because a survival hold that does not act **immediately** is not one.
`'y'` means now. Answering it with "finish your missiles first" reintroduces
precisely the delay those two ADRs were written to remove, and it does so on the
path where the aircraft is by definition already in trouble.

## Decision

**D1. `mission_loiter` runs the search-and-destroy loops, exactly as
`mission_j20` does.** The hold keeps its flight behaviour unchanged; it gains
the padlock and weapon-fire loops for as long as it owns the aircraft.

**D2. They start inside the mission runner, not before the thread.** ADR 123's
entry pull-up is the one place the hold simply holds the stick back, and it is
blocking. Starting the loops in the runner guarantees the pull-up has returned
before any padlock press can interleave with it.

**D3. They stop in `finally`, not beside the cancel log.** `mission_j20` stops
them on its normal and exception paths; the hold has more ways out — cancel,
exception, respawn, exit request — and a weapon loop that outlives the mission
owning it fires into the next one.

## Why this does not fight the hold

The two never contend for a control. Search-and-destroy presses exactly two
keys — `PADLOCK_CAMERA` and `FIRE_ACTIVE_WEAPON`. The hold commands pitch, roll
and afterburner. There is no shared axis.

The lifecycle needs no new machinery either: both loops already terminate on
`_mission_cancel`, which the hold sets and clears exactly as `mission_j20` does,
and it clears it *before* the runner starts.

Padlock during a telemetry-driven tactic is not new. `mission_j20` runs it
continuously while the behaviour tree reads the same altitude and nose-direction
crops the hold reads.

## What this deliberately does not fix

**The empty-rack terminal state.** ADR 109 makes `is_missiles_empty` return False
during a survival hold, on measured grounds: Eject dove a loitering aircraft at
-71 degrees, suppressed its climb for four seconds and killed it. So once this
change empties the rack, the aircraft orbits with nothing left to fire and no
route to rearm.

This ADR turns "never fires" into "fires, then orbits empty". Whether an empty
hold should be allowed to eject once it is demonstrably safe — high, level, no
incoming — is a separate decision needing its own evidence, and is not taken
here.

## Consequences

A hold now expends its missiles. Sessions where `'y'` is used should show weapon
activity during the hold and, in time, holds that reach an empty rack — a state
that did not previously occur and has no exit under ADR 109.

Nothing about the hold's flight behaviour changes, which is what the existing
loiter tests assert and continue to assert.

## Validation

- V1. Unit: the hold starts the weapon loops.
- V2. Unit: the hold stops them when it ends.
- V3. Unit: they stop even when the loop raises, and the ADR 109 hold flag clears
  with them.
- V4. Unit: they start **after** the ADR 123 entry pull-up, asserted on call
  order rather than on the fact of the call.
- V5. Unit: the orbit still runs with the loops up — the regression that matters.
- V6. Live: **satisfied 2026-09-06.** Two holds in the 3h52m session, both firing.

The second is the one that mattered, because it entered nose-down and so ran
ADR 123's blocking pull-up for real:

```
14:14:24,267 mission_loiter - holding to stay alive (target 5000 m)
14:14:24,267 mission_loiter - cancelling the eject in progress
14:14:24,267 mission_loiter - entry with the nose DOWN — holding nose up
14:14:29,306 mission_loiter - entry pull-up held 5.04s
14:14:29,307 search_and_destroy padlock loop started
14:14:29,307 search_and_destroy weapon loop started
```

The pull-up held the stick back for 5.04 s and the loops started **one
millisecond after it returned** — D2 proven on the path it was written for, with
no padlock press interleaved with the emergency recovery. The first hold
(11:23:11) needed no pull-up and started its loops in the same millisecond as the
hold itself.

Covered by `tests/test_mission_loiter.py`.

## References

- ADR 109 — Eject yields to the survival hold; the source of the terminal state above
- ADR 111 — the hold stops an eject already in progress
- ADR 123 — the blocking entry pull-up this must not interleave with
- ADR 075 — `mission_j20`, whose search-and-destroy lifecycle this mirrors
- Evidence: session ending 2026-09-06 04:28, `logs/wingman_20260906_042814.log`
