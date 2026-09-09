# ADR 112 — The Orbit Pitch Is a Closed Loop

| Status | Date       | Wingman Version |
|--------|------------|-----------------|
| Draft  | 2026-09-04 | 1.8.8           |

## Context

ADR 110 D5 made the orbit a level turn by commanding `nose_up` alongside every
roll. The reasoning was sound and the measurement behind it was real: roll with
no back-pressure descends, 6709 m to 6385 m in 18 s, which dropped the aircraft
out of the band, re-triggered the climb, and stalled it into the ground four
times in five minutes.

The fix worked in the sense that the orbit stopped sinking. It then failed in
the opposite direction, measured in the 2026-09-04 18:47 hold:

```
Altitude  Speed  Nose
 5838 m     19   +40      climb
 6634 m   1782   +32
 7469 m   1508   +41      already past the 7000 m target
 8358 m   1331   +54
 9154 m   1124   +58
 9799 m    839   +62
10189 m    528   +62      3200 m above target, speed collapsing
```

Unconditional back-pressure does not hold an altitude — it commands a climb at
whatever rate the pitch pulse produces. The aircraft left the band through the
top and headed for the same stall from the other side. The next hold in that
session **entered** at 8813 m, 1800 m high, which is the previous cycle's
overshoot arriving as the next cycle's initial condition.

Holds in that session lasted 20-35 s. That is how long it takes to climb out of
a 1000 m band.

This is the same class of error as the boundary rule fixed earlier the same day:
**acting on every tick instead of acting on a measured error.**

## Decision

**D1. Roll is unconditional; pitch is a closed loop.** The circle is the point of
the orbit and always runs. Pitch is commanded from the altitude error against
the hold target.

**D2. Correct toward the target in both directions.** Below target, `nose_up`;
above target, `nose_down`. ADR 110 had only the first half, which is why it could
only fail upward.

**D3. A deadband, `orbit_deadband_m` (150 m).** Within 150 m of target the orbit
commands no pitch at all. Without it the loop corrects on every tick and
oscillates — which is the failure mode of both previous versions, once in each
direction. The deadband is what makes "hold" different from "chase".

**D4. The band and the deadband are different things.** `hysteresis_m` (500 m)
decides climb-versus-orbit; `orbit_deadband_m` (150 m) decides pitch-versus-
nothing inside the orbit. The deadband must be the smaller of the two, or the
orbit would command pitch right up to the point where the climb takes over.

**D5. The climb still owns real altitude gain.** Below the band, `climb_mode`
runs with its fuel floor, duration cap and confirmation. The orbit's pitch
pulses arrest a drift; they are not a climb.

## Consequences

The orbit should now hold a band rather than transit it. Whether it does is V4
and is not yet observed.

The overshoot in the trace above is partly the CLIMB's exit, not the orbit's —
the climb handed over at 7469 m, already past target. This ADR makes the orbit
recover from that instead of amplifying it, but it does not fix the handover.
If holds still enter high after this change, the climb exit is the next target
and it is ADR 073 / ADR 086 territory.

`nose_down` during a hold looks alarming in a log. It is bounded by the same
0.35 s pulse as the up-correction and only fires above target.

## Validation

> **V1-V3 MET LIVE 2026-09-04 21:20.** All three branches fired on the correct
> side of the deadband on the first live hold. **V4 was not met, and the cause
> is upstream of this ADR** — the climb handed over at 7113 m with the nose at
> +82 deg, so the aircraft zoomed to 8401 m, stalled and departed. The closed
> loop was correct and irrelevant. See ADR 114.

- **V1.** Below target and inside the band, the orbit rolls and pulls up.
- **V2.** Above target and inside the band, the orbit rolls and pushes down.
- **V3.** Inside the deadband, the orbit rolls and commands no pitch.
- **V4 — live.** A hold stays within roughly the deadband of 7000 m for its
  duration, with no monotonic climb out of the band. Not yet observed. This
  supersedes ADR 110 V5, which this ADR shows was met by a mechanism that could
  not keep it.

## References

- ADR 110 D5 — the unconditional back-pressure this replaces, and the sink
  measurement that justified it
- ADR 111 — the pre-emption work in the session that produced the trace above
- ADR 073 / ADR 086 — `climb_mode`, whose exit hands the orbit its initial
  altitude
- `wingman/controller.py` — `mission_loiter`
- `tests/test_mission_loiter.py` — V1-V3
