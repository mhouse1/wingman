# ADR 058 — Eject Dive Confirmation via Raw Descent Rate

| Status | Date       | Wingman Version |
|--------|------------|-----------------|
| Draft  | 2026-07-30 | 1.6.29          |

Extends [ADR 038](038-game-battle-altitude-speed-signals-for-phase3-and-eject-dive.md)
(Accepted). ADR 038 is not modified; this ADR adds a second confirmation path and
revises the correction gate, and supersedes ADR 038 only on those two points.

## Context

ADR 038 confirms the eject dive from a sine-ratio band: descent rate divided by
speed approximates `sin(flight-path angle)`, and `steep_dive_min_sin: 0.8`
(approx 53 degrees) marks a confirmed steep dive. The 2026-07-28 flight test
raised that threshold from 0.5 to 0.8 because a 30-degree dive was counting as
steep by definition.

The 2026-07-30 16:27 production session (30m16s, 4 ejects) showed the resulting
loop never confirms anything:

```
steep dive confirmed        : 0
correction budget exhausted : 7   (6 band=level, 1 band=dive)
corrective nose-down re-issue: 24
corrective nose-up re-issue : 0
```

Replaying all 255 logged telemetry samples through the real `TelemetryProcessor`
puts the maximum observed `|alt_rate / speed_fps|` at **0.346** — including a
terminal dive that ended in ground impact. Against a 0.8 threshold, the positive
branch of the closed loop is unreachable in practice, so every eject fell through
to `correction budget exhausted` or the legacy timer.

Two further defects compounded it:

- **`confirm_consecutive` was a no-op.** The loop polls every
  `check_interval_s` (1.5 s) but telemetry refreshes every ~3.0 s
  (`ocr_every_n_ticks: 2`, added in v1.6.27 after ADR 038 was written). The
  streak counted polls, so one physical reading satisfied "two consecutive" by
  being read twice — defeating exactly the low-speed-transient protection ADR 038
  added it for.
- **The correction gate could not reverse.** An earlier same-day fix required
  `speed.trend == rising` before a nose-up reversal. Climbing trades speed for
  altitude, so "descent got shallower" and "speed rising" are anti-correlated by
  conservation of energy: the gate blocked the reversal in all 8 of the 8
  corrections where the rate-worsened test passed.

## Decision

**1. Confirm on either the sine band or a raw sustained descent rate.**

`steep_dive_min_sin` is left at 0.8 and the ADR 038 band path is unchanged. A
second, independent condition also confirms:

```
alt_rate <= -confirm_descent_fps          (default 250 ft/s)
```

The raw rate is unit-independent of the speed reading, so it still works if HUD
speed and HUD altitude are on different scales — which is what a 0.346 ceiling
across a full 30-minute session, ground impact included, implies. Whether that
ceiling is a units mismatch or a genuine flight-envelope limit is left open; the
descent-rate path makes the loop functional either way without re-litigating the
0.8 threshold that ADR 038 derived from flight data.

**2. Count distinct telemetry samples, not polls.**

The confirmation streak advances only when `snapshot.altitude.ts` differs from
the sample that produced the previous increment. Re-reading one sample is not new
evidence.

**3. Gate the reversal on the over-rotation signature, not on speed trend.**

Reverse to nose-up only when the aircraft is **already descending** and the last
nose-down made the descent **shallower**:

- `rate < 0` — still descending
- `rate > rate_before_correction` — descent got shallower

Past vertical, further nose-down rotation pulls the velocity vector back toward
horizontal, so descending-and-worsening is the over-rotation signature. When the
aircraft is climbing (`rate > 0`) nose-down is unambiguously correct however many
times it has failed, and a nose-up tap there pitches into a loop — observed at
2026-07-30 06:34:31 (`band=level, alt rate +153 ft/s -> corrective nose-up`)
after an earlier fix dropped the descending-only condition.

**4. Cap cumulative nose-down hold per eject.**

`corrections` resets on every entry to the nose phase, and the phase re-enters up
to `dive_reentries` times, so nose-down could stay held ~75 s continuously —
long enough to fly a full loop. `total_nose_budget_s` (default 20 s) now bounds
the whole sequence.

The budget measures time NOSE_DOWN is **actually held**, accumulated across
phases, not wall clock since the eject began. The afterburner hold phase runs up
to 120 s with no key down; charging that to the nose budget would make every
dive-decay re-entry an instant no-op.

**5. Re-enter the decay check only after a real confirmation.**

The hold loop tracks the phase exit reason (`confirmed` / `budget_exhausted` /
`nose_budget_exhausted` / `no_telemetry` / `cancelled`). Re-entry after a give-up
re-ran the same failing loop and logged "dive decayed after confirmation" when
nothing was ever confirmed.

**6. Treat a sensor that has stopped refreshing as missing telemetry.**

Because confirmation now requires a *new* sample, a frozen-but-not-yet-stale
reading would otherwise spin the loop until `stale_after_s`, silently stretching
the nose-down hold to the staleness horizon. The dedup path falls back to the
same legacy timer that missing telemetry uses.

**7. Skip the plausibility delta gate once the seed is stale.**

Decision 8 below clamps `dt`, which removes the old self-relaxing behaviour where
a growing gap widened the envelope. Without a companion rule, the first good
reading after any telemetry gap would be rejected against a seed that no longer
describes the aircraft — costing a multi-second reseed exactly when the dive
confirmation needs the signal back. A seed older than `stale_after_s` is now
treated as no seed.

**8. Clamp the plausibility gate's `dt` to the design tick.**

Every delta gate is a per-second rate multiplied by the real inter-sample gap, so
slowing the sampler silently widens the envelope. `ocr_every_n_ticks: 2`
(v1.6.27, added after ADR 038 sized these bounds at the 1.5 s tick) halved the
cadence to ~3.0 s and thereby doubled every allowance — a 1114 → 8 mph collapse
passed the filter on 2026-07-30. `dt` is now capped at `max_gate_dt_s` (1.5 s).

## Configuration

```yaml
telemetry:
  max_gate_dt_s: 1.5                # cap on the dt multiplier in the delta gates
  eject_closed_loop:
    confirm_descent_fps: 250        # raw sustained descent that also confirms a dive
    total_nose_budget_s: 20.0       # cap on cumulative NOSE_DOWN hold per eject
```

## Consequences

- The closed loop can now reach a positive outcome; it is no longer a
  correction-only loop that always terminates in a give-up.
- Confirmation is honest: two distinct sensor readings, not one read twice.
- Nose-up can fire again, but only in the attitude where it is physically
  correct.
- Nose-down hold is bounded, so a failed eject degrades to a plain dive attempt
  rather than an unbounded pitch input.
- The 0.8 sine threshold stays exactly as ADR 038 set it. If later flight data
  shows the ratio is systematically compressed by a units mismatch, that is a
  separate calibration decision and warrants its own ADR.

## Validation

`make test` (eject closed-loop suite) covers each decision:

- `test_sustained_descent_confirms_without_steep_band` — decision 1
- `test_confirmation_requires_distinct_samples_not_repeated_polls` — decisions 2, 6
- `test_two_distinct_samples_do_confirm` — decision 2 (guards over-strict dedup)
- `test_descending_and_worsening_reverses_regardless_of_speed_trend` — decision 3
- `test_climbing_never_reverses_to_nose_up` — decision 3 (backflip regression)
- `test_total_nose_budget_caps_the_hold` — decision 4
- `test_nose_budget_ignores_time_when_nose_is_up` — decision 4 (held, not wall clock)
- `test_throttled_cadence_does_not_widen_the_speed_gate` — decision 8
- `test_gate_is_skipped_once_the_seed_is_stale` — decision 7

Live-flight confirmation of the descent-rate threshold is still outstanding; this
ADR stays `Draft` until a production session shows a confirmed dive.
