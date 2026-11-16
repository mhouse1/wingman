# ADR 175 — The Rearm Climb-Out Brakes in a Steep or Fast Dive

| Status   | Date       | Wingman Version |
|----------|------------|-----------------|
| Draft    | 2026-11-09 | 1.9.4           |

## Decision

- **D1.** The pursuit's rearm climb-out takes the emergency climb's throttle rule
  ([ADR 159](159-the-emergency-climbs-nose-up-carries-the-afterburner.md)).
  - In a dive the emergency climb would brake, it holds the airbrake, not the afterburner. That is
    a path steeper than `behavior_tree.climb.emergency_airbrake_below_deg` (-40 deg), or a path
    below level at `emergency_airbrake_above_kph` (900) or more, held until
    `emergency_airbrake_release_kph` (750).
  - Otherwise it keeps the afterburner, as before. An unknown angle keeps the current state.
- **D2.** The burner comes down before the airbrake goes up (ADR 137: the two cancel each other).
  The airbrake is held under the `rearm_climb` owner, which yields to a takeover and to the
  emergency climb. It is released with the climb-out and with `release_tracking_holds`.

## Why

- **The numbers.** The climb-out lit the afterburner whatever the attitude. Climb-outs logged
  2026-11-01 to 11-08 (`HUD nose` before the climb-out; a no-kill-card death 3 to 15 s after):

  | Started at the pass, from | Climb-outs | Crash after |
  |---|---|---|
  | shallower than -20 deg | 269 | 12 (4%) |
  | -20 to -40 deg | 62 | 11 (18%) |
  | steeper than -40 deg | 18 | 5 (28%) |

  - Climb-outs started by the count from a dive: 11% and 8%.
  - After a pass climb-out, the nose rose 5 deg or more in only 54 of 349. Where it fell 5 deg or
    more, 18% crashed (n = 56).
- **The case.** Stormbreak, 2026-11-09 00:19:28.9.
  - The climb-out started at -42 deg and 826 kph, with the approach brake just let go.
  - Under nose-up and afterburner the nose went on to -61 deg by 00:19:30.5.
  - The emergency climb then braked ("steep dive: airbrake held, afterburner suppressed"), too late.
- **The rule itself was measured on emergency climbs** (ADR 159 and its amendment). The burner in a
  dive added speed (1,067 to 1,459 kph). The nose came up 21 deg/s at 400 to 599 kph, against 3 to
  5 deg/s at 1,000 to 1,399.
- **Assumed, not yet measured:** that the burner is what made the climb-out fail. The climb-out
  logs hold no contrast, because every climb-out lit it. The live check below is what tests it.

## Consequences

- A climb-out from a steep or fast dive decelerates instead of accelerating. In a shallow dive or a
  climb it is unchanged.
- A new INFO line marks each brake: `RESUPPLY: rearm climb-out in a steep or fast dive: airbrake
  on, afterburner off (ADR 159)`.

## Validation

- Live: crash 3 to 15 s after pass climb-outs started steeper than -20 deg, against the table
  above, over at least 40 such climb-outs.
- Stall prevention trips after a braked climb-out (ADR 159's stall), counted.

## References

- [ADR 137](137-emergency-climb-airbrake-and-crash-instrument.md),
  [ADR 159](159-the-emergency-climbs-nose-up-carries-the-afterburner.md)
- [Design 020](../hldd/020-terrain-service-from-the-maps-own-geometry-hldd.md) (resupply runs)
