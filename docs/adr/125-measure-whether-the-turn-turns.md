# ADR 125 — Measure Whether the Turn Turns

| Status | Date       | Wingman Version |
|--------|------------|-----------------|
| Draft  | 2026-09-05 | 1.8.8           |

## Context

`BoundaryTurn` has been tuned across ADRs 101, 107, 120 and 122 and its median
range gain is **+0.000R over 440 turns** with attitude and range both recorded.
Two hypotheses for why were tested on 2026-09-05 and both failed:

**"The turn stalls the aircraft."** The sampler shows the nose reaching +90
degrees and speed falling to 61 KPH, which looks conclusive. It is not: turns
whose nose reached +60 degrees or more had a median gain of +0.120R and receded
2 of 2, against +0.000R and 2 of 5 for shallower ones. Steeper was better.

**"The turn needs a minimum speed."** In one session, every turn that receded
had a minimum speed at or above 384 KPH and every turn that did not was at or
below 312 — a clean separation at n=7. Pooled over all sessions it inverts:

| | n | median min-speed |
|---|---:|---:|
| receded | 343 | 537 KPH |
| did not recede | 97 | **660 KPH** |

Failed turns were *faster*, and the recede rate is 77-80% both above and below
every threshold tried. The n=7 separation was chance.

Both hypotheses were reaching for the same missing fact. **A turn's job is to
change heading, and nothing in this system measures heading.** The attitude
sampler reports PITCH, derived from altitude rate over speed (ADR 038). The
range summary reports where the aircraft ended up. Neither can distinguish:

- the aircraft rotated and the range did not follow,
- the aircraft did not rotate at all,
- the aircraft rotated the wrong way.

Guessing between those is what produced two disproved hypotheses, and the
project's own rule is to instrument rather than tune when two explanations look
identical in the log.

## Decision

**D1. Log the bearing to the boundary across each turn.** The minimap is
heading-up, so the bearing to a fixed boundary point rotates with the aircraft:
`atan2(lateral, forward)` is a heading-change proxy that needs no new sensor.
The lateral component became available only with ADR 122.

**D2. Report NET and PATH separately.** Net is where the heading ended up; path
is total rotation travelled. An aircraft rocking between two headings has a
large path and a net of zero, and that is a specific, recognisable failure that
net alone would hide.

**D3. Unwrap the +/-180 seam.** A turn across it must read as 20 degrees, not
340 in the opposite direction — a wrapped sample would fabricate exactly the
large swing this is meant to detect.

**D4. Instrumentation only.** Nothing steers on this. Three tuning rounds have
now been argued from quantities that could not settle the question; the next
change to the turn should be argued from this one.

## Consequences

The next session yields, for every turn, both what the aircraft did (bearing)
and what it achieved (range) — so "the turn does not work" finally splits into
answerable cases.

The proxy is not a compass. It measures rotation of the aircraft **relative to
the nearest boundary point**, which also moves as the aircraft translates. Over
a 12-second turn at these speeds the rotation term should dominate, but a large
net bearing change with no range gain would be ambiguous between "turned but
kept closing" and "flew far enough along the edge to change the bearing".

It is also only as good as the detector: bearing needs a reading, and readings
exist on about 56% of battle ticks. Turns that run blind will report few
samples, and the sample count is logged so a thin one can be discounted.

## Validation

- **V1.** A steady turn accumulates bearing in one direction.
- **V2.** The +/-180 seam is not read as a reverse turn.
- **V3.** An oscillation shows a large path and a small net.
- **V4.** A single sample yields no bearing rather than a fabricated zero.
- **V5 — live.** Turns report a bearing change, and the range gain can be
  cross-tabulated against it. Not yet observed.

## References

- ADR 107 — the +0.00R finding and the attitude sampler that measures pitch
- ADR 122 — the lateral component this depends on
- ADR 038 — why the "nose" figure is a flight-path angle, not an attitude
- `wingman/tick_handlers.py` — the turn range and bearing summaries
- `tests/test_tick_handlers.py` — `TestTurnBearingTracking`, V1-V4
