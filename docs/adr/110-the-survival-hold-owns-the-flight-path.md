# ADR 110 — The Survival Hold Owns the Flight Path

| Status | Date       | Wingman Version |
|--------|------------|-----------------|
| Draft  | 2026-09-04 | 1.8.8           |

## Context

ADR 109 stopped Eject from diving a loitering aircraft into the ground. It fixed
one instance of a general problem it did not name: **the behaviour tree owns
tactics regardless of which mission is running**, so every combat tactic keeps
flying the aircraft during a mission whose objective is to stay out of combat.

Reviewing the 2026-09-04 session after the Eject fix, loiter still could not hold:

- **203 EngageNav commands in a five-minute hold.** `_may_fly` asks only whether
  a mission is running, and loiter is one. So the navigator steered at the enemy
  throughout — toward the long-ring contacts that sit at the arena edge, which is
  the same pursuit ADR 107 identified as the upstream cause of boundary
  crossings.
- **Disengage would cancel the mission outright.** `disengage_roll_right` opens
  with `cancel_mission()`, and its condition is 30 s without an enemy — precisely
  the state a survival hold produces. Loiter was built to eventually cancel
  itself. It survived this session only because Engage kept seeing distant
  contacts.
- **The orbit sank.** It commanded roll and no pitch. A banked turn without
  back-pressure descends: 6709 m to 6385 m in 18 s, which dropped the aircraft
  out of the hold band, re-triggered the climb, overshot to +90 degrees at
  67 KPH, stalled, and dived 5600 m into the ground. Four times in five minutes.

The boundary crossing in that session was a consequence of the last one — a
ballistic dive across the edge, with `BoundaryTurn` correctly suppressed by ADR
107 D4 because the ground was 19 seconds away.

## Decision

**D1. The combat branch yields to a survival hold.** Engage, Regroup and
Disengage do not steer while `survival_hold` is set.

**D2. Gate at the ACTUATION, not in the conditions.** The tree still selects and
still logs what it would have chosen. That is the shadow pattern used elsewhere
here, it keeps the log diagnosable, and it avoids editing three conditions to
express one policy.

**D3. Regroup is gated too.** It is the arguable case: it steers toward
friendlies, which is inward and broadly safer. But it steers to *rejoin the
fight*, which is the one thing a hold exists to avoid, and it would be a second
writer on the roll axis the orbit is using. Staying in bounds is BoundaryTurn's
job, and BoundaryTurn stays live — so nothing is lost by gating it.

**D4. BoundaryTurn, MissileEvade and RespawnWait stay live.** They serve the same
objective the hold does. A survival hold that ignored an inbound missile, or flew
out of the arena, would be a worse survival hold. This is ADR 109 D3 extended to
its natural boundary: tactics that PROTECT the aircraft stay; tactics that SPEND
it, or that fly it somewhere for a tactical reason, yield.

**D5. The orbit is a level turn.** Pitch is commanded with the roll
(`orbit_pitch_hold_s`, 0.35 s against the roll's 0.6 s). Both are short and
non-blocking, so this is a coordinated nudge rather than a sustained pull — the
climb still owns any real altitude gain, with its own fuel floor, duration cap
and confirmation.

> **Superseded by ADR 112 (2026-09-04).** Unconditional back-pressure does not
> hold an altitude, it commands a climb: the 18:47 hold went 6634 m to 10189 m,
> 3200 m above target, with speed decaying 1782 to 528 KPH. The sink measurement
> that motivated D5 stands; the remedy was one-sided. Pitch is now a closed loop
> on altitude error with a deadband. **V5 is superseded by ADR 112 V4.**

## Consequences

During a survival hold the aircraft is flown by the mission, plus BoundaryTurn
and the defensive tactics. That is what a survival hold should mean, and it is
what it did not mean before.

The combat tactics still SELECT during a hold and appear in the logs. A reader
seeing `selected=Engage` during a loiter now has to know it did not actuate —
that is the cost of D2, and the alternative was three conditions carrying the
same term.

Wingman will hold an aircraft out of a fight it might have won. That is the
mission the operator started; anything else makes `y` mean something other than
what it says.

This does not fix the long-ring pursuit itself. Outside a hold, Engage still
chases distant contacts to the arena edge — ADR 107's finding, and still open.

## Validation

- **V1.** With `survival_hold` set, `_actuate_engage` is not called for Engage or
  Regroup; without it, it is.
- **V2.** `_start_disengage` does not call `disengage_roll_right` during a hold,
  and still does outside one.
- **V3.** BoundaryTurn's actuation is not gated on the hold.
- **V4.** The orbit commands pitch on every roll — equal call counts, not a lag.
- **V5 — live.** A loiter session holds its altitude band without the
  climb/stall/dive cycle, takes no EngageNav commands, and is not cancelled by
  Disengage. Not yet observed.

## References

- ADR 109 — Eject yielding to the hold; this generalises that decision
- ADR 024 — the selector, whose priorities this does not change
- ADR 107 — the long-ring pursuit that Engage performs, unfixed outside a hold
- ADR 107 D4 — why BoundaryTurn was suppressed during the dive
- `wingman/controller.py` — `mission_loiter`, `is_survival_hold`
- `wingman/tick_handlers.py` — the actuation dispatch and `_start_disengage`
