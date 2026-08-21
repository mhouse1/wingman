# ADR 083 — Lead-the-Target Climb Exit and Burner Cut Above Target

| Status | Date       | Wingman Version |
|--------|------------|-----------------|
| Accepted | 2026-08-19 | 1.8.4           |

## Context

ADR 081 set the armed sustain band to 4000–5000 m and added the 80° pitch
ceiling. The ceiling works — the 2026-08-19 18:40 session (4 h 42 min, 47
missions) logged **250 angle-driven down-pulses against 10 rate-driven**,
so the angle rule does 96% of the corrective work. But it is correcting a
symptom. Across 135 sustain climbs in that session:

| Measure | Result |
|---|---|
| Median overshoot past the 5000 m target | **2401 m** |
| Sustain climbs exiting above 6000 m | **132 / 135** |
| Flight time above 6000 m | **48%** |
| Flight time in the intended 4000–5000 band | **7%** |
| Sustained ≥88° stretches | 73 |
| …ending below 300 km/h (stalled) | **43 (59%)**, median 138 km/h at 8785 m |

The doctrine — armed aircraft hold 4000–5000 m — is met only trivially:
the aircraft is above 4000 m because it is usually at 7000–9000 m. In
practice it blows through the band, runs out of airspeed near vertical,
stalls (~9 times per hour), falls back, and climbs again.

**The cause is arithmetic, not control law.** Telemetry arrives every
~3 s; a burner climb runs ~450 m/s, so each sample is ~1350 m of
altitude; and the exit needs `confirm_reads` (2) consecutive fresh reads
at or above target. That is ~2700 m of overshoot built into the exit
test before any control decision — which matches the measured 2401 m
median. Meanwhile the afterburner stays lit for the whole overshoot,
feeding the very energy the pitch ceiling then fights.

## Decision

### d1 — Lead the target by the sample interval

`climb_mode` gains `exit_lead_s`. The exit test compares the **predicted**
altitude at the next sample against the target:

```
alt + (rate * exit_lead_s) >= target
```

Releasing at that point lets momentum carry the aircraft to the target
instead of past it. The prediction uses the same fresh-sample rate the
pulse controller already reads; when the rate is unknown the term is zero
and the test degrades to the current behaviour (freeze policy). The
`confirm_reads` debounce is unchanged and now applies to the predicted
value, so garbage reads still cannot end a climb early.

`climb.exit_lead_s` defaults to **3.0** (the measured telemetry cadence).

### d2 — The lead applies to the sustain band only

`_start_climb` passes the configured lead for sustain climbs and **0 for
emergency (terrain) climbs** — the same split already used for
`fuel_floor_pct`. Two reasons: overshooting a terrain-avoidance climb is
protective, not wasteful; and leading a 1000 m target at 450 m/s would
satisfy the exit test from below the ground, ending the climb instantly.
Terrain outranks efficiency, exactly as in ADR 075 d3.

### d3 — Cut the burner once above target

When a fresh read shows the aircraft at or above the target — one read,
no debounce — the afterburner is released for the remainder of the hold
and not re-pressed, independent of the ADR 075 fuel gate. Removing the
energy source is the physical fix for a zoom climb; fighting it with
nose-down pulses while thrust is still applied is why the stalls persist.
The fuel gate keeps its own floor/rearm behaviour below the target.

## Consequences

- Sustain climbs should terminate near 5000 m instead of ~7400 m, putting
  flight time back in the doctrine band and cutting the stall class that
  drove ADR 081.
- Less burner per climb: thrust stops at the target rather than at the
  duration cap or the fuel floor, so the ADR 075 evade reserve survives
  more engagements.
- The pitch ceiling stays as the backstop it was designed to be; its
  firing rate is now a regression signal — if it keeps firing 250 times a
  session, the overshoot has returned.
- Emergency climbs are unchanged in every respect.
- A climb whose rate reads high but whose momentum does not carry (a
  stalling aircraft) will exit slightly low. Acceptable: the band
  re-selects, and low-and-fast is a better failure than high-and-stalled.

## Verification

- Unit tests: predicted-altitude exit fires a sample early at a high rate;
  unknown rate reproduces the legacy exit; `confirm_reads` still debounces
  the predicted value; lead of 0 reproduces legacy behaviour exactly;
  burner released on the first at-or-above-target read and not re-pressed;
  emergency climbs receive lead 0 from `_start_climb` (`test_climb_mode.py`,
  `test_behavior_tree.py`).
- `make test` green; replay gates unaffected.
- Live acceptance, measured with the instruments that produced this ADR:
  (a) median sustain overshoot under ~500 m, (b) share of flight time in
  the 4000–5000 band up from 7%, (c) stall-ending high-angle stretches
  down from 43/session, (d) angle-driven down-pulses well below 250.

## References

- ADR 081 — pitch ceiling and the 4000 m sustain floor (this fixes the
  overshoot its data exposed; the ceiling remains as backstop)
- ADR 075 — fuel discipline and the sustain band; d3 precedent for
  per-call parameters split sustain vs emergency
- ADR 073 — climb tactic, `confirm_reads` debounce, pulse-and-observe
- 2026-08-19 18:40 session log — the 135-climb measurements above
