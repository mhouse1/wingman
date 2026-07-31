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

The descent-rate check is evaluated **before** the missing-telemetry bail-out,
not after it: `pitch_band()` returns `None` whenever the *speed* signal is
stale, and bailing on `band is None` first skipped the descent-rate path in
exactly the case it exists to survive (measured: 9 confirm-grade samples lost
to stale speed in the 2026-07-30 18:51 session).

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

**8. Clamp the plausibility gate's `dt` to the design tick — SPEED GATE ONLY.**

Every delta gate is a per-second rate multiplied by the real inter-sample gap, so
slowing the sampler silently widens the envelope. `ocr_every_n_ticks: 2`
(v1.6.27, added after ADR 038 sized these bounds at the 1.5 s tick) halved the
cadence to ~3.0 s and thereby doubled every allowance — a 1114 → 8 mph collapse
passed the filter on 2026-07-30. `dt` is capped at `max_gate_dt_s` (1.5 s).

*Revised after the 2026-07-30 18:51 session:* the clamp is correct for the
**speed** gate (an acceleration envelope tuned at the design tick) but was a
serious defect on the **altitude** gate, whose bound is physics — vertical speed
cannot exceed total speed — and therefore scales correctly with the real gap.
Clamped, the altitude allowance shrank to `margin × clamp / gap = 0.75 × speed`,
structurally rejecting every dive steeper than sin 0.75 while the confirm band
starts at 0.8. Verified by replay: a steady sin-0.85 dive lost 8 of 10 samples,
`altitude.ts` froze (starving the distinct-sample dedup of decision 2), and the
loop was fed stale level bands that drove 50 blind nose-down re-issues — the
single mechanism behind that session's 0-of-26 confirmations. The clamp now
applies per-gate: `max_gate_dt_s` on speed, the real gap (bounded by the
`stale_after_s` seed-usability check) on altitude.

**9. First post-gap reading never fabricates a rate.**

The stale-seed bypass (decision 7) accepts the first reading after a gap
unconditionally, but the rate history still held pre-gap entries — so a single
bogus post-gap read could pair across the gap into a confirm-grade fake steep
dive (observed in simulation: 3950 vs true 8900 after an 8 s outage → −625 ft/s)
and then delta-block the true series. The history is cleared on the bypass path;
the first post-gap sample carries `rate=None` and the next real sample restores
the rate honestly.

**10. Confirmation continues post-release (observation only).**

The eject typically fires from climbing flight and takes ~12 s to rotate, so the
deep dive often establishes only after nose-down is released (recovered
2026-07-30 data: 63 confirm-eligible samples post-release vs 4 in-phase). The
afterburner-hold loop now runs the same two-distinct-sample check — no key
input — logging `dive confirmed post-release` and enabling the decay re-entry
of decision 5, which is granted only when nose-down budget headroom remains.
`total_nose_budget_s` stays at 20 s: the recovered in-phase data shows a ~12 s
porpoise oscillation, not slow convergence, so holding longer buys loop exposure
rather than confirmation.

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
- `test_descent_rate_confirms_even_when_speed_is_stale` — decision 1 (evaluation order)
- `test_stale_speed_and_frozen_altitude_never_corrects` — decision 1 + ADR 038
  "never correct against missing data" on the combined missing-evidence path
- `test_confirmation_requires_distinct_samples_not_repeated_polls` — decisions 2, 6
- `test_two_distinct_samples_do_confirm` — decision 2 (guards over-strict dedup)
- `test_descending_and_worsening_reverses_regardless_of_speed_trend` — decision 3
- `test_climbing_never_reverses_to_nose_up` — decision 3 (backflip regression)
- `test_total_nose_budget_caps_the_hold` — decision 4
- `test_nose_budget_ignores_time_when_nose_is_up` — decision 4 (held, not wall clock)
- `test_throttled_cadence_does_not_widen_the_speed_gate` — decision 8 (speed gate)
- `test_steep_dive_is_accepted_at_throttled_cadence` — decision 8 (altitude gate)
- `test_gate_is_skipped_once_the_seed_is_stale` — decision 7
- `test_stale_seed_bypass_does_not_fabricate_a_rate_across_the_gap` — decision 9

Live-flight confirmation of the descent-rate threshold is still outstanding; this
ADR stays `Draft` until a production session shows a confirmed dive.
