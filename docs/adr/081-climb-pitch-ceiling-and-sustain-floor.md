# ADR 081 — Climb Pitch Ceiling at 80 Degrees and the 4000 m Armed Sustain Floor

| Status | Date       | Wingman Version |
|--------|------------|-----------------|
| Accepted | 2026-08-18 | 1.8.4           |

## Context

Two J20 mission doctrine changes, both operator-directed from live
observation of the 2026-08-17/18 sessions.

**The climb still over-rotates.** The ADR 076/078 work bounded the spawn
guard's contribution, and the ADR 076 d3 rate ceiling corrects excessive
climb *rate* — but sessions still log `Nose: +90° (steep_climb)` stretches
and the operator observed the terminal failure mode directly: past
vertical, the aircraft comes out heading reversed and flies out of the
map. The mechanism is an inversion in the rate-based pulse logic: as pitch
approaches vertical, speed bleeds (observed: speed 26 at 9250 m — a stall
at apex) and the climb RATE decays below `min_climb_rate` — so the
controller pulses **more nose-up** precisely when the aircraft is already
over-rotated. Rate is the wrong variable at the extreme; the flight-path
angle is the direct one, and it is already computed every fresh telemetry
sample (`pitch_angle_deg()`, the value in the session log's `Nose:` lines,
saturating at ±90°).

**The sustain band climbs too high for too long.** ADR 075 d5 set the
armed operating band at 6000–7000 m. In practice that keeps the aircraft
in long steep climbs (the 90 s sustain cap fired on telemetry gaps at
altitude), spends fuel, and creates the stall-at-apex situations above.
The operator's doctrine: the tactical requirement is only that an armed
aircraft stays **above 4000 m** — the emergency band (ADR 073, 500/1000)
continues to own terrain avoidance regardless of armament.

## Decision

### d1 — Pitch ceiling: nose-down above 80 degrees, outranking the rate floor

`climb.max_pitch_deg` (default **80**). In the climb hold's pulse
decision, a fresh flight-path angle at or above the ceiling commands a
NOSE_DOWN pulse, and this check runs **before** the rate-floor check —
the whole point is that at extreme pitch the rate floor gives the wrong
answer. Below the ceiling, the existing logic is unchanged (nose-up below
`min_climb_rate`, nose-down above `max_climb_rate`, nothing between the
bands). An unreadable angle changes nothing (freeze policy); the rate
bands still apply. Keeping ~10° of forward margin below vertical
preserves the forward velocity component — the aircraft climbs steeply
but can never trade through vertical into reversed flight.

### d2 — Armed sustain floor: 4000 m

`climb.sustain.enter_below_alt`: 6000 → **4000**;
`climb.sustain.exit_above_alt`: 7000 → **5000** (the same 1000 m
hysteresis). The ADR 075 d5 gating is untouched: the band still requires
missiles > 0 and a running mission, still shares the leaf with the
emergency band, still debounces with `confirm_reads`. The aircraft now
holds a 4000–5000 m operating layer instead of 6000–7000 — shorter
climbs, less burner, fewer high-altitude stalls, and the doctrine reads
as stated: while armed, stay above 4000.

## Consequences

- The over-rotation loop closes from both ends: the spawn guard can no
  longer pre-load a loop (ADR 078) and the climb controller can no longer
  pitch through vertical chasing a decaying rate (this ADR).
- Climbs terminate ~2000 m sooner; expect fewer `max_climb` cap firings
  and a lower fuel-floor release rate in the session log.
- Engage geometry gets more trigger time at the lower operating layer
  (Climb pre-empts Engage less often). If kill rates move, the band is
  the tuning knob — same note as ADR 075.
- The pitch value is the velocity-vector angle, not nose attitude (ADR
  067 caveats), and compresses near vertical — but 80° sits where the
  measurement is still ordinal-correct, and the failure it guards against
  saturates the value at 90°, comfortably past the threshold.

## Verification

- Unit tests: over-angle commands nose-down even with the rate below the
  floor (the inversion case); below-ceiling angles leave rate logic
  unchanged; unset ceiling reproduces legacy behavior; sustain-band tests
  keep their own fixture values (`test_climb_mode.py`).
- `make test` green; replay gates unaffected.
- Live validation: (a) no `Nose: +90°` stretches during commanded climbs,
  (b) no backwards/out-of-map flight (operator observation), (c) sustain
  climbs top out ~5000 m with fewer cap firings, (d) survival split and
  spawn-crash count hold.

## References

- ADR 073/075 — climb tactic and armed sustain band (operating values
  revised here)
- ADR 076/078 — over-rotation history: rate ceiling, pulsed spawn guard
- ADR 067 — metric telemetry units and the pitch-angle measurement caveats
- 2026-08-18 04:21 session — stall-at-apex evidence (speed 26 at 9250 m)
