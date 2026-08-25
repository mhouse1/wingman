# ADR 088 — Eject Abort on Rearm, and Incoming-Priority Afterburner

| Status | Date       | Wingman Version |
|--------|------------|-----------------|
| Draft  | 2026-08-22 | 1.8.5           |

## Context

Two defects observed in the 2026-08-22 01:45 session. They are separate
mechanisms and are decided separately below, but they share a shape worth
naming: **a decision made correctly in one context kept being applied after
that context had changed.**

### The aircraft flew a usable missile into the ground

The eject-and-dive is a deliberate tactic — with an empty rack, diving to
respawn is faster than flying home. It fired correctly:

```
01:51:20  🚀 MISSILES EMPTY — cancelling mission and ejecting
01:51:20  BT[active]: selected=Eject missiles=0 ... alt=5508
01:51:20  eject_and_dive — descent control engaged (impulse rotation, target 100 m/s)
01:51:33  BT[active]: selected=Idle missiles=1 ... alt=None
01:51:39  eject_and_dive complete
```

MetalStorm rearms missiles on a timer, and the dive outlives that timer. Thirteen
seconds in, the rack refilled — and nothing re-read it. The aircraft continued
into the ground carrying a weapon it could have used.

The premise (`missiles == 0`) was checked once, at entry, for an action lasting
roughly 30 seconds.

### The afterburner was cut with a missile inbound

```
01:47:32  🌀 MISSILE EVADE — holding afterburner + roll right + yaw left
01:47:38  missile_evade — manoeuvre limit (6.0s) reached, releasing while incoming is still present
01:47:39  ⬆️  CLIMB — holding nose up + afterburner
01:47:39  climb — reached target alt 5000 — afterburner cut
```

The ADR 070 d12 manoeuvre limit released the evade with the alert still on
screen — itself deliberate, to stop an unbounded manoeuvre. Climb took
selection, and 1.7 s later cut the burner because it had reached its target
altitude (ADR 083 d3).

Two policies switch the burner off while a missile is tracking:

| Policy | Rule | Written for |
|--------|------|-------------|
| ADR 075 | release at the fuel reserve floor | preserving a reserve for a *future* evade |
| ADR 083 d3 | cut at target altitude | stopping a zoom climb stalling itself |

Both are correct in the situation they were written for. Neither was written
with an inbound missile in mind, and both outrank the one manoeuvre where
thrust matters most.

## Decision

### d1 — The eject re-checks its premise while acting on it

`_eject_descent_control` re-reads the missile count each poll cycle. If the
count is a readable integer greater than zero, the descent aborts with
`reason=rearmed` and the aircraft is handed back to the behaviour tree.

The check is deliberately narrow:

- Only a **readable integer > 0** aborts. An OCR dropout returns `None` and is
  not treated as a rearm — absence of a reading is not evidence of ammunition.
- It runs in the descent loop only, so the entry decision is untouched. This
  adds an exit condition; it does not re-litigate whether to eject.

Configurable via `telemetry.eject_closed_loop.abort_on_rearm` (default `true`)
so it can be switched off without a code change if the rearm timer turns out to
interact badly with mission pacing.

### d2 — An inbound missile outranks every afterburner reserve policy

While `incoming` is detected, the climb tactic will not release the burner at
the ADR 075 fuel floor, and will not cut it at the ADR 083 d3 target altitude.

**The ADR 075 empty-tank release still stands.** At 0% the burner produces no
thrust *and* a held key blocks the game's recharge, so holding it there does
not help this evade and actively degrades the next one. The override runs down
to, but does not include, empty.

This does not supersede ADR 075 or ADR 083 d3. Both keep their behaviour in
every situation they were written for; d2 adds a higher-priority case neither
considered.

Implemented through one `Controller._incoming_now()` predicate consulted by
both burner sites, so the two cannot drift apart — the ADR 086 d7 pattern.

## Consequences

The eject now has a second way to end, which the ADR 044/045 replay validators
see as a new `reason` value. `rearmed` is a *successful* outcome, not an
anomaly, and is recorded as such.

d2 spends fuel that ADR 075 reserved. That is the intended trade: the reserve
exists to make a future evade possible, and an evade in progress is worth more
than a hypothetical one. A session under sustained missile pressure will run
its tank lower than before, and the empty-tank release is what bounds it.

Neither change addresses the ADR 070 d12 manoeuvre limit releasing the evade
while the alert is still present. That remains deliberate — an unbounded
manoeuvre is its own hazard — but it means the aircraft can still be *out of
the evade* while a missile tracks it. d2 makes that window less dangerous
rather than closing it.

## Validation

**V1 — no dive completes with missiles aboard.** Grep a session for
`eject_and_dive complete` and check the missile count in the surrounding
`BT[active]` lines. Expect zero completions with `missiles>0`; expect
`ABORT, N missile(s) rearmed mid-descent` where the old behaviour would have
continued.

**V2 — the burner survives a handoff with incoming present.** Find a
`manoeuvre limit ... releasing while incoming is still present` followed by a
climb, and confirm no `afterburner cut` inside the incoming window; expect
`reached target alt ... but INCOMING — holding afterburner` instead.

**V3 — the empty-tank release is intact.** No burner press while `fuel=0`, even
with incoming detected.

**V4 — evade survival rate.** The reason d2 exists. Compare
`Missile engagements: with evade` across sessions before and after. Baselines
so far: 90% (n=72), 74% (n=49), 100% (n=2) — the spread across accounts and
small samples means this needs several sessions to read, and it is the weakest
of the four criteria.

Unit coverage: `test_incoming_overrides_the_fuel_reserve_floor`,
`test_incoming_does_not_force_burner_on_an_empty_tank`,
`test_descent_aborts_when_missiles_rearm`,
`test_descent_continues_while_rack_stays_empty`.

## Alternatives considered

**Abort the eject from the behaviour tree instead of the descent loop.** The
tree already re-evaluates every tick and would notice the rearm. Rejected: the
descent is owned by a Controller thread that the tree does not preempt
mid-action, so the tree would signal a stop the descent might not honour until
its next poll anyway. Checking where the action runs is more direct.

**Hold the burner whenever incoming is detected, in every tactic.** Simpler to
state, but the evade already holds it, and the eject deliberately manages
descent rate with it. A blanket rule would fight the eject's rate control.
Restricting d2 to the climb targets the observed defect without disturbing
tactics that have their own reasons.

**Raise the ADR 070 d12 manoeuvre limit instead.** Would keep the evade running
rather than handing off mid-threat, addressing the root of the V2 case. Not
taken here: d12 was set from live evidence about unbounded manoeuvres, and
changing it needs its own evidence rather than being altered as a side effect.

## References

- ADR 070 — missile evade tactic; d12 manoeuvre limit
- ADR 075 — fuel-gated afterburner and the reserve floor
- ADR 083 d3 — thrust cut at target altitude
- ADR 086 d7 — the single-predicate pattern reused for `_incoming_now()`
- ADR 069 — eject rotation control, whose descent loop d1 extends
