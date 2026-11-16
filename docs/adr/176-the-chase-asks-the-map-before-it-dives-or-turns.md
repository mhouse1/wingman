# ADR 176 — The Chase Asks the Map Before It Dives or Turns

| Status   | Date       | Wingman Version |
|----------|------------|-----------------|
| Draft    | 2026-11-09 | 1.9.4           |

## Decision

- **D1 — a steer guard on the pursuit's own tick.** On every steering tick, the pitch and roll the
  chase holds are projected through the terrain service's height map for `way_out_horizon_s` (5 s),
  from the last fix carried forward along its track to now, at the HUD's altitude and rate. Four
  paths are flown: as commanded, with the nose-down withheld, with the roll flipped, and with no
  command.
- **D2 — the verdict.** A command whose path meets the ground within `steer_guard.decide_s` (3 s)
  is one the guard would change: withhold the nose-down when the same roll without the push stays
  clear (or lasts 1 s longer), flip the roll when the other side does. The pitch is withheld, never
  pulled: a pull throws the target off the screen, which is why the chase's pull-out was removed
  ([Review 018](../code-review/018-2026-09.md) action item 001, operator 2026-09-26).
- **D3 — shadow first.** This revision logs: `STEERGUARD:` on every tick with a command (DEBUG),
  and one INFO line per change of verdict. It flies nothing. Actuation is the next revision, after
  the validation below.
- **D4 — gates.** No verdict on a fix older than `max_fix_age_s` (2 s), with no fix speed, or with
  no fresh altitude. The fix itself already needs `min_score` and `min_gap`.

## Why

- Every safety behaviour wingman has is a reflex that interrupts the chase after a threshold trips.
  The chase steers at a target by screen error and never asks the map before it commands a dive or
  a turn, though the map can see 5 s ahead.
- **Measured, 2026-11-08 and 11-09:**
  - Chase dives went from -15 to -90 deg inside one 1.5 s map reading, with the straight path
    reading clear 3 s before impact: Stone Forest 18:52 and 22:22, Stormbreak 19:57 and 21:53,
    Crimson 23:43.
  - A climb handed back to the chase because a target or the ring was in view was followed by a
    no-kill-card death within 20 s 13% of the time (1,369 hand-backs) against a 6% base rate.
    Nothing measured at the hand-back separates the bad ones; the danger is the chase's next
    manoeuvre.
  - 88 of 121 Crimson resupply crashes came after the ring approach was abandoned, under the
    chase or the climb.
  - Neither the dive angle at a ring nor the map's exit at the pickup predicts a ring crash
    (Design 020, 22:45 and 22:50). The reflex layer is tuned to its limit.
- The projection reuses `fly_candidate`, the same model the way-out candidates use. A negative pull
  is the chase's nose-down; the path floors at vertical.
- **Assumed, not yet measured:** that a withheld push or a flipped roll would have kept the chase
  out of the ground. The shadow pass is what measures it.

## Consequences

- Each steering tick with a command costs four short projections (about 50 steps each) from the
  carried-forward fix. No key is pressed.
- A withheld push, once it actuates, can let a target slide off the bottom of the screen. The
  arm-drawn trial decides whether that costs more kills than it saves crashes.
- Stone Forest fixes score 0.5 to 0.6; a 200 m position error turns a pillar check into noise. The
  score gate stands, and the validation reads Stone Forest apart.

## Validation

- Offline, from the shadow lines: no-kill-card deaths 3 to 15 s after a tick the guard would have
  changed, against ticks it left alone, per map, over at least 40 verdicts.
- The share of chase ticks the guard would change (its cost to the chase), per round.
- Then a revision that actuates under the ADR 174 arm draw, judged on crashes per round and kills
  per round at 40 rounds per arm.

## References

- [ADR 174](174-the-map-warning-starts-the-emergency-climb-in-half-the-rounds.md)
- [Design 020](../hldd/020-terrain-service-from-the-maps-own-geometry-hldd.md), Phase 7
