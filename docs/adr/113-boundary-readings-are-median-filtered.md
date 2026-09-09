# ADR 113 — Boundary Readings Are Median-Filtered

| Status | Date       | Wingman Version |
|--------|------------|-----------------|
| Draft  | 2026-09-04 | 1.8.8           |

## Context

ADR 108 made the boundary detectable on the post-update minimap and ADR 107
built a tactic on it. Neither asked how noisy a single reading is. The answer,
measured over 636 readings in the 2026-09-04 evening session:

| tick-to-tick move | share of ticks |
|---|---:|
| >0.15R | 16% |
| >0.25R | 8% |
| >0.40R | 3% |

The median move is 0.042R, so the detector is mostly tracking something real.
The tail is not survivable. At the 1179-1455 KPH recorded in those traces, a
0.40R step in 1.5 s is not reachable at any plausible minimap scale — that is
the detector changing its mind between frames, not the aircraft moving:

```
20:57:33  dist=0.496
20:57:34  dist=0.071      0.42R in 1.5 s
20:57:48  dist=0.571
20:57:49  dist=0.061      0.51R in 1.5 s
```

**BoundaryTurn consumed these raw.** Its entry gate, its release rule and its
range summary all act on one tick. That is how the turn logged
`0.50R -> 0.57R (closest 0.05R, 11 ticks, receded)` — declaring it had pulled
away — ten seconds before a confirmed crossing, and how the 20:29 approach read
`fwd` negative through five seconds of monotonic closure, so the entry gate saw
nothing until 3.5 s before the crossing.

Two earlier hypotheses were tested against the data and **disproved**, which is
worth recording because both looked convincing:

- *The forward sign is inverted.* No. Readings that say "ahead" close 71% of the
  time; readings that say "behind" close 23%. The convention is right.
- *The 79% of readings lying on the fore-aft axis are a static UI artefact.* No.
  `detect_map_boundary`'s own reference frames are on-axis too (0.59/-0.59,
  0.10/+0.09, 0.62/+0.61). Flying roughly perpendicular to a boundary segment
  puts the nearest point on the nose axis; that is the expected signature.

The defect is not the sign and not the geometry. It is that a single frame was
treated as evidence.

## Decision

**D1. Median of the last three readings.** Applied where the reading enters the
tick, so every consumer — the tactic, the loiter orbit, the instrumentation —
sees the same filtered value.

**D2. Median by DISTANCE, returning that reading's own pair.** Averaging `dist`
and `fwd` independently would synthesise a bearing no frame reported. The filter
must only ever emit a tuple some frame actually produced.

**D3. Bounded by age as well as length (`boundary_median_age_s`, 5 s).** A
reading either side of a 30 s blind gap says nothing about the same approach.

**D4. Fewer than three fresh readings passes the raw value through.** The
alternative is going blind for three ticks after every gap, which is worse than
the noise. This makes the change never worse than the previous behaviour; it
simply is not better for the 23% of ticks that follow a gap.

**D5. A median, not an EMA.** The failure is single-frame outliers, and a median
rejects them outright while an average is dragged by them. A real approach is
monotonic over many ticks, so the median tracks it with one tick of lag.

## Consequences

Measured on the same 636 readings, replaying the filter offline:

| | raw | median-of-3 |
|---|---:|---:|
| median tick-to-tick | 0.042R | 0.015R |
| jumps >0.15R | 16% | 6% |
| jumps >0.25R | 8% | 2% |
| jumps >0.40R | 3% | 1% |

Outliers cut by roughly two thirds; the tail is reduced, not removed. Anything
still moving 0.40R between filtered ticks is a detector fault that filtering
cannot reach, and would need ADR 108 work.

**The tactic gains one tick of lag** — about 1.5 s — on a genuine approach. At
the 0.25R entry threshold and the closure rates seen here that is roughly 0.05R
of warning traded for the false-trigger reduction. This is a deliberate trade
and it is the thing to re-examine first if crossings do not fall.

This does not touch the detector. Every reading it produces is still whatever
ADR 108's component selection found; this only stops one bad frame reaching the
tactic alone.

Nor does it address the upstream cause. ADR 107 named Engage's long-ring
pursuit as what carries the aircraft to the edge, and that remains untouched.

## Validation

- **V1.** A single spike between two consistent readings is rejected.
- **V2.** A monotonic approach survives, tracking with one tick of lag.
- **V3.** The emitted pair is always one some frame reported.
- **V4.** Fewer than three fresh readings passes the raw value.
- **V5.** Readings older than `boundary_median_age_s` are dropped.
- **V6 — live.** Crossings per mission below the 0.10-0.34 band the metric has
  occupied all week, sustained over enough missions to mean something. **Not yet
  observed, and one session will not show it** — the band was produced by
  unchanged code, so anything under 40 missions is noise (ADR 106 D3).

## References

- ADR 106 — the crossing-rate table this is measured against
- ADR 107 — BoundaryTurn, the consumer, and the long-ring pursuit still open
- ADR 108 — the detector, unchanged here
- `wingman/tick_handlers.py` — `_median_boundary`
- `tests/test_tick_handlers.py` — `TestBoundaryMedianFilter`, V1-V5
