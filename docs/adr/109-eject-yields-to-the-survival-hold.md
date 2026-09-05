# ADR 109 — Eject Yields to the Survival Hold

| Status | Date       | Wingman Version |
|--------|------------|-----------------|
| Draft  | 2026-09-04 | 1.8.8           |

> Implemented and CONFIRMED LIVE 2026-09-04. V1-V4 covered by tests; V5 met —
> see *Live confirmation*.

## Context

`mission_loiter` exists to keep the aircraft alive: climb to a holding altitude
and orbit there. It has one objective and no tactical goal.

On 2026-09-04 it could not achieve that objective, because Eject kept taking the
aircraft away from it and flying it into the ground:

```
09:58:45  eject_and_dive — dive established (nose -71deg, -660 m/s, 4 pulse(s))
09:58:45  climb suppressed — eject in progress
09:58:46  climb suppressed — eject in progress
09:58:47  climb suppressed — eject in progress
09:58:48  climb suppressed — eject in progress
09:58:49  RESPAWN DETECTED - Cancelling active missions
09:58:49  eject_and_dive — cancelled during descent (reason=respawn_detected)
09:58:49  Controller: mission_loiter - ended
```

The session log shows this as a cycle: loiter starts, climbs, orbits, is ejected
into the ground, respawns, restarts, repeats. Four `mission_loiter - ended`
lines in twelve minutes, each preceded by an eject.

Eject fires on an empty missile rack (ADR 024), and it sits at priority 3 in the
selector — above everything except `Idle` and `RespawnWait`. That ranking is
right for a combat mission: an aircraft with no missiles is worth trading for a
rearmed one, and the dive is how the trade is made.

It is exactly wrong for a survival hold. An empty rack says nothing about
survival, and the dive is the single most reliable way to end it.

## Decision

**D1. Eject yields while a survival hold owns the aircraft.** `mission_loiter`
sets a flag for its lifetime; the snapshot carries it as `survival_hold`; both
eject conditions return False while it is set.

The two tactics encode opposite trades. Eject spends the airframe to get a
rearmed one. Loiter spends everything else to keep the airframe. When both apply,
the mission the operator started is the one that decides.

**D2. Gate BOTH eject conditions, not just the raw read.**
`is_missiles_empty` is what the selection-only build uses;
`is_eject_confirmed` — the debounced verdict — is what runs once the leaf
actuates, which is every live session. Gating only the first would have left the
behaviour unchanged in exactly the case that produced the log above.

**D3. Yielding is specific to Eject.** `RespawnWait` and `MissileEvade` still
outrank the hold. They protect the aircraft rather than spend it, so they serve
the same objective loiter does and there is nothing to resolve. A survival hold
that ignored an inbound missile would be a worse survival hold.

**D4. The flag clears in `finally`.** Every exit path — cancel, exception,
respawn, operator stop — must drop it. Clearing at the end of the loop body
would leave Eject suppressed for an aircraft loiter no longer owns, which is a
quieter failure than the one being fixed and harder to attribute.

**D5. A Controller-side flag, not a mission-name check.** `_last_mission` records
what was last *started*, not what is *running*, and survives past the end of the
mission it names. The flag is set and cleared by the loiter loop itself, so it
describes the present.

## Consequences

A loitering aircraft with an empty rack now stays airborne instead of diving.
That is the intent, and it means wingman will hold an unarmed aircraft rather
than trade it — correct for a survival hold and wrong for anything else, which
is why the gate is keyed on the hold rather than on the ammunition.

`mission_loiter` becomes usable. It has been in the codebase since its rewrite
without a working live run; this ADR and the `AttributeError` fix below are what
that took.

Eject is unchanged for every other mission. The gate is one term in a condition
that is otherwise identical.

## Also fixed, found on the way

`mission_loiter` raised `AttributeError` on its first live tick and had done
since the rewrite:

- `snap.altitude.stable` — the field is `stable_value`
- `snap.altitude_fresh` without the call — it is a METHOD, so the expression was
  a bound method and always truthy, and the stale-read guard immediately below it
  never ran

Nine tests passed against this. The fake defined `altitude.stable` and made
`altitude_fresh` a property, mirroring the caller's assumptions rather than
`TelemetrySnapshot`. A fake built from the code it tests proves only that the
code agrees with itself. It now builds a real `TelemetrySignal`.

## Validation

- **V1.** With missiles at 0 and `survival_hold` set, the selector does not
  choose Eject; without it, it does.
- **V2.** Both `is_missiles_empty` and `is_eject_confirmed` yield.
- **V3.** `RespawnWait` and `MissileEvade` still outrank the hold.
- **V4.** The flag is cleared on every exit path from the loiter loop.
- **V5 — live. MET 2026-09-04.** See below.

## Live confirmation (2026-09-04)

The rack emptied during a hold and the aircraft kept flying:

```
12:22:53  mission_loiter - holding to stay alive (target 7000 m)
12:23:28  mission_loiter - holding at 6709 m, orbiting right
12:23:50  Ammo missiles: 0                      <- empty DURING the hold
12:24:21  mission_loiter - ended
```

No `MISSILES EMPTY — cancelling mission and ejecting`, no `eject_and_dive`, no
dive. Thirty-one seconds of orbiting on an empty rack, where the 09:58 log has a
dive at -71 degrees and a dead aircraft.

The same log confirms Eject is still armed rather than merely disabled: at
12:22:17, OUTSIDE a hold, `Ammo missiles: 0` produced
`MISSILES EMPTY — cancelling mission and ejecting` as it always has. D1 and D3
in one session.

It also confirms the `AttributeError` fix is live — `holding at 7812 m, orbiting
right` is past the line that raised, so that path could not run at all the day
before.

## References

- ADR 024 — the selector and Eject's priority, which this narrows rather than
  reorders
- ADR 070 — MissileEvade, which D3 leaves above the hold
- ADR 073 / ADR 086 — `climb_mode`, which loiter delegates to and which the
  eject suppression was blocking
- `wingman/controller.py` — `mission_loiter`, `is_survival_hold`
- `tests/test_mission_loiter.py` — the fake rebuilt on the real signal type
