# ADR 164 — Roll Right Through the Flares

| Status | Date       | Wingman Version |
|--------|------------|-----------------|
| Draft  | 2026-10-27 | 1.9.3           |

Operator, 2026-10-27: "when incoming missile is detected it should hold ROLL_RIGHT_KEY until 2 seconds
after the flares are deployed, this used to be a feature but was removed a few months ago, add it as part of
the strict doc requirement so it doesnt get accidentally removed again, and note that the rolling doesnt
change trajectory it just does a barrel roll continuously making it harder for missiles to hit".

## Decision

**D1. Every alert rolls right through its flares.** The flare handler
(`AmmoEventsHandler.deploy_flares_on_new_incoming`) starts the burst, then calls `Controller.flare_roll` with
an event the burst sets after its last flare. The roll holds `ROLL_RIGHT_KEY` from the alert until
`behavior_tree.missile_evade.flare_roll_after_s` (2.0 s, the operator's figure) after that flare. A burst
while the roll is held extends it to the same interval after its own last flare. Through a volley the bursts
come about 1.5 s apart, so the roll lasts until the alerts stop. It runs in `GAME_BATTLE` and
`GAME_BATTLE_EJECT`.

**D2. It does not depend on the missile evade.** It runs whether or not `missile_evade.enabled` is set,
whether the tree's evade ([ADR 070](070-missile-evade-tactic.md)) or the pursuit's
([ADR 163](163-the-missile-evade-flies-through-a-pursuit.md)) flies, and in either trial arm. It has no
`enabled` switch, because that switch is how it would be removed. The roll keys are leased (ADR 163 D3), so
the evade and the flare roll can both hold roll-right, and whichever releases first does not lift the other's.

**D3. The pursuit yields pitch and roll while the roll is held,** as it does for the dive recovery
([ADR 148](148-a-dive-recovery-flies-through-a-pursuit.md)) and the evade (ADR 163 D2). A barrel roll leaves
the flight path as it is only if nothing pitches during it. Tracking pitch applied while the aircraft rotates
would fly a corkscrew. Tracking and firing carry on, and steering resumes when the roll ends. The pursuit
yields to the dive recovery first, then the missile evade, then the flare roll.

**D4. It ends where every held flight key ends.** These are manual takeover (SAF-001: the flight-writer
registry stops it, and its poll checks too), program exit and cleanup (SAF-007), an eject (ADR 070 d11), and a
hard-emergency climb (ADR 163 D5). It does not start while a hard-emergency climb flies, and it releases
within one 0.05 s poll when one starts. A fault cap, `flare_roll_max_s` (15 s, the same figure as SAF-006's
evade cap), covers a burst that never reports its last flare. `ROLL_RIGHT_KEY` gets ADR 070 d4's programmatic
bracket and release grace, so its auto-repeats do not read as a takeover.

**D5. [FR-011](../requirements/002-functional.md) states it.** `@relation(FR-011)` markers bind the
requirement to `Controller.flare_roll`. `tests/test_flare_roll.py` pins the behaviour, the requirement, the
marker and the shipped 2 s. Removing the code fails `make test`.

## Consequences

- **ADR 163's trial arms change from the first session with this code.** The control arm becomes flares,
  the afterburner ([ADR 128](128-afterburner-during-an-incoming-alert.md)) and the flare roll; before, it
  was flares and the afterburner. In the evade arm the roll can outlast the evade's 6 s limit through a
  volley. The trial still measures what the evade adds (yaw-left, and the hold until the alert clears).
  Sessions before and after this ADR are kept apart in ADR 163's results table by its Code column.
- Inside a pursuit, the chase loses its steering from each alert until 2 s after the last burst, in both
  arms. ADR 163 V3 found no chase cost from the evade's yields of up to 6 s. The same measures apply here.
- In `GAME_BATTLE`, tree tactics that steer by roll (the boundary turn, mission roll pulses) are not made to
  yield. While `missile_evade.enabled` is set, the tree selects its evade on the alert within one 1.5 s
  tick, and the evade holds roll-right too.
- ADR 132's turn guard does not apply, because the roll is not a turn. Alerts are suppressed for 10 s after
  a respawn anyway (`AmmoEventsHandler.suppress_after_respawn`).

## Why

The behaviour existed before and was lost twice:

- In March 2026, the scripted `mission_j20` rolled right while it dropped flares ("Roll right, afterburner,
  and flares at the same time", before 21a5e73). It went when the scripted missions were replaced.
- From 2026-08-11, ADR 070's evade held roll-right on every alert in `GAME_BATTLE`. Since the pursuit took
  over the fights (2026-09-27), the tree does not act in `GAME_BATTLE_EJECT`. The evade ran in 4 of 434
  alerted missile kills ([Research 019](../research/019-shoot-downs-and-the-missile-defence.md)).

A continuous barrel roll keeps the aircraft rotating about its own axis without changing its trajectory,
which the operator reports makes it harder for missiles to hit. No measurement here isolates the roll. The
control arm before and after this ADR is the nearest comparison.

One caution from ADR 163 V3: 3 of 78 evades began in a descent, and afterburner, roll-right and yaw-left
with no pitch input steepened each one to -58 or -90 degrees. The flare roll is roll alone (with ADR 128's
afterburner). D4's hard-emergency end is what covers a dive.

## Alternatives considered

- **A fixed hold from the alert** (for example 3 s). Rejected: the operator's rule is 2 s after the
  flares, and an event from the burst follows the burst's actual timing.
- **Hold the key inside the burst thread.** Rejected: the roll has to be a controller writer, so that the
  takeover and shutdown registry stops it and the pursuit can yield to it.
- **An `enabled` switch.** Rejected (D2).
- **Let the chase keep pitch while it rolls.** Rejected (D3).

## Validation

- **V1, tests.** `tests/test_flare_roll.py` covers the hold from the alert to `flare_roll_after_s` past the
  last flare, the extension, each refusal and each end, the cap, the lease with the evade, the chase's yield
  and resume, end to end from the flare handler, and the FR-011 trace and shipped 2 s. Also
  `tests/test_tick_handlers.py` (the roll starts after the flares, and a failing roll costs neither the flares
  nor the trial), `tests/test_flight_writers.py`, `tests/test_flight_precedence.py`, and `make reqs-gate`.
- **V2, the first live session.**
  - `FLARE ROLL — holding roll right` after the first `Deploying flares` of each volley.
  - `flare roll released (flares done, N s)` about 2 s after the volley's last flare.
  - Inside pursuits, `yielding pitch and roll to the flare roll (ADR 164)` and `flare roll over, steering
    resumes`.
  - No manual takeover from the roll, no `flare roll hit its` cap warning, and no stuck roll key.
- **V3, the cost.** Count crashes within 5 s of a flare roll's start, and compare engagement survival in the
  control arm with ADR 163's sessions before this ADR.

## References

- [FR-011](../requirements/002-functional.md)
- [ADR 070 — MISSILE_EVADE_MODE Behavior Tactic](070-missile-evade-tactic.md)
- [ADR 128 — Afterburner During an Incoming Alert](128-afterburner-during-an-incoming-alert.md)
- [ADR 148 — A Dive Recovery Flies Through a Pursuit](148-a-dive-recovery-flies-through-a-pursuit.md)
- [ADR 163 — The Missile Evade Flies Through a Pursuit](163-the-missile-evade-flies-through-a-pursuit.md)
- [Research 019 — Shoot-downs and the Missile Defence](../research/019-shoot-downs-and-the-missile-defence.md)
- `docs/architecture.md`, "Flight-input precedence"
