# ADR 127 — A Swallowed Exception Hid a Dead Subsystem

| Status | Date       | Wingman Version |
|--------|------------|-----------------|
| Draft  | 2026-09-05 | 1.8.8           |

## Context

ADR 122 widened the boundary reading from `(dist, forward)` to
`(dist, forward, lateral)`. `_instrument_boundary` still destructured two:

```python
dist, forward = reading
```

Every tick with a READABLE boundary therefore raised
`ValueError: too many values to unpack (expected 2)` into the method's broad
handler, which logged at DEBUG. Measured in one 2h28m session: **1880
occurrences**.

The subsystem was dead from 11:19 onward, across 8.5 hours of soak:

| | pre-ADR-122 | post-ADR-122 |
|---|---:|---:|
| `BOUNDARY: dist=` per-tick lines | 892 | **0** |
| approaches detected | 40 | **0** |
| approach frames captured | yes | **none** |

The only symptom was a DEBUG line that stopped appearing, in a log where
`BOUNDARY: no reading` kept printing 4105 times and looked like an explanation.

**The tests could not have caught it.** `_instrument_boundary` had no test
exercising a reading at all, and ADR 122's own tests covered the detector and
the turn — the two ends of the change — while the consumer in between was never
constructed.

## What survived, and what did not

**Crossing counts are unaffected.** `detect_return_to_battle` and the RTB
confirmation run BEFORE the unpack, so ADR 106's rows and every crossings-per-
mission figure stand.

**ADR 126's numbers stand.** The turn range and bearing summaries are computed
in the behaviour-tree tick from `_b_dist` and `_b_lat`, not here. The 12 s
versus 5 s comparison, the 294-degree path measurement and the reversion are
all unaffected.

**Lost for 8.5 hours:** approach counting, approach frames, the per-tick `dist=`
series, and the contents of the crossing trace buffer (`dist`/`fwd` recorded as
`None`). Any readability figure computed from that window is unavailable — not
wrong, absent.

## Decision

**D1. Index, do not destructure.** `reading[0], reading[1]` accepts both widths,
so a future widening cannot break this consumer again.

**D2. The FIRST failure is logged at ERROR; repeats stay at DEBUG.** A per-tick
path that swallows at DEBUG converts a total outage into silence. One loud line
is enough to notice; 1880 loud lines would be their own failure.

**D3. Keep the broad `except`.** This runs on the tick path and instrumentation
must never take the aircraft down. The defect was never that it caught — it was
that it caught *quietly*.

**D4. Test the consumer with both widths**, and with a real `AnalyzerSnapshot`
rather than a stub carrying the two fields the caller happens to read.

## Consequences

Approach counting and approach frames resume, so ADR 117's corpus and ADR 107's
approach evidence start accumulating again.

Any diagnostic that goes quiet is now distinguishable from one reporting
nothing to say — which was exactly the confusion here, where 4105 "no reading"
lines read as an explanation for the missing `dist=` lines.

This does not change what any tactic does. It restores measurement.

## Also found on the way

The first version of the test helper built the snapshot with
`altitude=TelemetrySignal(...)`. The real field is a float, and the real code
path raised `TypeError: type TelemetrySignal doesn't define __round__` — caught
because the test drives the actual method. **A fixture built from the wrong
type fails loudly only when the real code runs against it**, which is the
argument for using the real object rather than a hand-shaped stub.

The tests were verified against the pre-fix code: 2 of the 4 fail there.

## Validation

- **V1.** A three-tuple reading does not break instrumentation.
- **V2.** A three-tuple reading still counts approaches.
- **V3.** A two-tuple reading still works.
- **V4.** The first failure is reported at ERROR; repeats are not.
- **V5 — live.** `BOUNDARY: dist=` lines and approach counts reappear in the
  next session. Not yet observed.

## References

- ADR 122 — the widening that broke this, and whose own tests covered both ends
  of the change but not the consumer between them
- ADR 106 — the crossing counts, computed before the unpack and therefore intact
- ADR 126 — the turn measurements, computed elsewhere and therefore intact
- ADR 117 — the approach and blind corpora this restores
- `wingman/tick_handlers.py` — `_instrument_boundary`
- `tests/test_tick_handlers.py` — `TestBoundaryInstrumentationSurvivesReadingWidth`
