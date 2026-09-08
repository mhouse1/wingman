# ADR 133 — A Shorter Fragment, Corroborated by the Void

| Status | Date       | Wingman Version |
|--------|------------|-----------------|
| Draft  | 2026-09-07 | 1.8.8           |

## Context

ADR 117 established where the boundary detector fails, and it is not where it was
assumed to be:

- The hue window is **correct**. Measured across 88 banner-confirmed crossings,
  the line sits at hue 16-18, the centre of the configured 8-28.
- On **90%** of the frames the detector calls blind, the boundary is genuinely in
  view.
- Every rejection is a **span** rejection. Across 3,660 rejected components the
  thickness test never fired once.

The span gate requires the boundary to arrive as **one connected component** of
`boundary_min_span_frac` (0.5) times the minimap radius — 79 px. Since the
2026-09-02 minimap update the line is thin and antialiased and does not reliably
arrive whole. ADR 108 added a morphological closing pass to reconnect it; on this
corpus that is not enough, and the longest surviving fragment on a blind frame
runs 20-76 px against the 79 px bar.

Two obvious repairs were measured and **rejected** before this one:

- **Add red to the hue window.** It locks onto the compass rim arc, which is red
  on every frame regardless of position, and would corroborate everything.
- **Aggregate the fragments.** The union of thin components spans 238-293 px on
  blind frames and 248-279 px on successful ones — no separation at all.

## The signal that does separate

Out of bounds renders as a large dark, desaturated region (V approx 51, S approx
0 — not black; an early `V<45` test found nothing). It is an **area** measure, so
the fragmentation that breaks the span gate cannot break it.

Its share of the minimap disc orders as the geometry requires: 0.076 on blind
frames, 0.117 on approaches, 0.272 on confirmed crossings.

Critically, it is safe as a **veto**. Across all 88 banner-confirmed crossings the
lowest void reading is **0.0187**, so a threshold of 0.01 rejects **none** of
them. A corroboration that could veto a real crossing would turn a recall fix into
a recall regression; this one cannot.

## Decision

**D1. Accept a shorter fragment when the void corroborates it.** A component
passes if `span >= boundary_min_span_frac * radius` (unchanged), **or** if
`span >= boundary_relaxed_span_frac * radius` (0.30) **and** the void fraction
exceeds `boundary_void_min_frac` (0.01).

**D2. The relaxed path only ever lowers the bar.** The strict gate stands on its
own and is evaluated first, so a frame that passed before still passes. Asserted
frame-by-frame, not on totals: an aggregate can improve while individual frames
regress.

**D3. Sample the void inside 0.78 of the disc radius.** The compass rim is a dark
ring at the edge; sampling the whole disc reads it as void on every frame and
would corroborate everything everywhere. A first pass did exactly that and
reported hue 112 — the blue rim — as the boundary colour.

**D4. `boundary_relaxed_span_frac: 0` restores pure ADR 108 behaviour.** One
config value reverts the decision, so a regression can be bisected without
reverting code.

**D5. Corroboration, not replacement.** The void is not used as a detector. It
gates a relaxation of an existing gate, and the reason is honest: the "boundary in
view" labels on blind frames are themselves derived from the void, so a
void-based detector cannot be scored against them without circularity. Recall on
the 88 crossings is independent — that label is the RETURN TO BATTLE banner — and
is the number this decision rests on.

## Measured effect

Through the real `detect_map_boundary`, over the archived corpus:

| | Strict gate (ADR 108) | Corroborated (this ADR) |
|---|---:|---:|
| Confirmed crossings detected | 76/88 (**86%**) | 80/88 (**91%**) |
| Previously-blind frames now reading | 0/509 | **161/509 (32%)** |

Against a bare relaxation to the same 0.30 span with no corroboration, which
recovers the same frames:

| Rule | Recall | Fires, boundary in view | Fires, nothing in view |
|---|---:|---:|---:|
| span >= 0.50 (current) | 86% | 0% | 0% |
| span >= 0.30 alone | 91% | 46% | **20%** |
| span >= 0.30 **and void** | 91% | 46% | **4%** |

Identical recovery at a fifth of the spurious firing. That is what the
corroboration buys.

**The 4% is optimistic and must not be quoted as validated.** The "nothing in
view" label is itself a void threshold (0.02) close to the gate's (0.01), so the
two are not independent. The honest live check is the spurious-turn rate: turns
per mission against crossings per mission in ADR 106, which should not rise
sharply without the crossing rate falling.

## Consequences

The detector reads on frames it previously called blind, so `BoundaryTurn` is fed
earlier and more often. ADR 107 measured that turn as break-even (+0.00R median
over 61 turns) — but on triggers that fired late, because the line was only seen
when it happened to arrive whole. Whether an earlier trigger makes the turn
effective is now testable for the first time, and is the reason to make this
change before touching the tactic.

`tests/test_minimap_bearing.py::test_the_boundary_is_found_at_the_centre_on_crossing_frames`
had been failing since 2026-09-06 at 87% against its 90% bar, recorded in ADR 117
as deliberately red until the detector was fixed. It passes at 91%. The suite is
green.

Turns per mission will rise. That is the intended effect and also the risk: if
crossings do not fall with it, the extra turns are noise and D4 is the revert.

## Validation

- V1. Corpus: more confirmed crossings detected than the strict gate, and at
  least 90% of them.
- V2. Corpus: previously-blind frames now produce readings.
- V3. Corpus: the void corroboration vetoes **no** real crossing (D1's premise).
- V4. Corpus: no frame detected under the strict gate is lost (D2), checked per
  frame.
- V5. Unit: `boundary_relaxed_span_frac: 0` restores the strict gate (D4).
- V6. Unit: the void sample excludes the compass rim (D3), and an all-dark frame
  reads as fully void.
- V7. Live: **re-specified 2026-09-07 — crossings per mission cannot answer this.**

At the pooled 0.132 per mission, an eleven-hour soak yields about fifteen
crossings and a 95% interval of 0.065-0.199. The smallest change that metric can
resolve is a **halving**; a 24% effect would need roughly forty-eight hours of
flying. Judging this ADR on an ADR 106 row would be judging it on noise, and the
original V7 asked for exactly that.

The turn's own outcome trace is 13x denser from the same flying — **173 turns
against 13 crossings** in the 2026-09-06 overnight soak — and it measures what a
detector change actually acts on: not whether the aircraft left the arena, but
whether the turn, once triggered, moved it away from the edge.

**Baseline, measured on that soak (ADR 132 live, ADR 133 not):**

| | |
|---|---:|
| Turns measured | **173** |
| Median range gained | **+0.000R** |
| Mean range gained | +0.017R |
| Gaining 0.10R or more | 33 (19%) |
| Losing 0.10R or more | 35 (20%) |
| Median heading swing | **136 deg** |

This reproduces ADR 107's break-even finding on nearly three times its 61 turns,
and sharpens it: the aircraft rotates **136 degrees** and the range does not
respond. Whatever is wrong is not a small effect.

**V7 procedure.** Run a soak of comparable length, then `make turn-outcome
LOG=<session>` and compare against the table above. The decision rule, fixed here
BEFORE the run so it cannot be chosen after seeing the data:

- **Median range gained clearly positive (>= +0.05R)** — the earlier trigger
  fixed the turn, and ADR 107's verdict was an artefact of late detection.
- **Still ~0.00R** — the turn is inert regardless of when it fires. The defect is
  in the TACTIC, not in the detector, and the next work is ADR 107's, not this
  one's.
- **Turns per mission well above 2.9 with range gained unchanged** — the relaxed
  path is firing on noise. Revert with `boundary_relaxed_span_frac: 0` (D4).

Note for whoever runs it: the log's own "receded" verdict fired on 74% of those
173 turns while the median net range gain was 0.000R. Those two cannot both be
describing the same thing, and "receded" should not be read as success until that
is reconciled.

Tooling: `scripts/turn-outcome.py`, `make turn-outcome`.

Covered by `tests/test_corroborated_span.py` (7 tests).

## References

- ADR 117 — where the detector fails, and the two repairs rejected before this
- ADR 108 — the closing pass this supplements
- ADR 107 — `BoundaryTurn`, measured break-even on late triggers
- ADR 106 — the series this must be judged against

## V7, first attempt — suggestive, not demonstrated (2026-09-07)

A 2h51m session, 29 missions, 62 measured turns, run with ADR 133 live.

| | Baseline (173 turns) | This session (62 turns) |
|---|---:|---:|
| Median range gained | +0.000R | **+0.020R** |
| Mean range gained | +0.017R | **+0.069R** |
| Gaining 0.10R or more | 33 (19%) | 21 (**34%**) |
| Losing 0.10R or more | 35 (20%) | 10 (**16%**) |
| Median heading swing | 136 deg | **75 deg** |
| Turns per mission | 2.9 | **4.1** |

**The pre-registered threshold was not met.** V7 fixed "median >= +0.05R" as the
bar before the run; the median came in at +0.020R. By the rule as written, this is
the "still break-even" branch.

The secondary signals are more encouraging and are reported because suppressing
them would be as dishonest as promoting them:

- Difference of means +0.052R, permutation test **p = 0.048** — marginal, and one
  of several tests run, so it should not be read as a 5% result.
- Win/loss went from 33/35 (a coin flip, which is what ADR 107 described) to
  21/10. Fisher exact **p = 0.086** — not significant.
- Median heading swing halved, 136 to 75 degrees. That is mechanistically
  consistent with the change: detecting the edge earlier means the turn starts
  further out and needs less rotation. It is consistent with the mechanism, not
  evidence of the outcome.

**Why this is not called a success.** The primary statistic was chosen in advance
precisely so a marginal secondary could not be substituted for it after the fact,
and that is exactly what promoting the mean here would be. The sample is also a
third of the baseline's — 62 turns against 173 — and the comparison is between
sessions, so map and situation differ along with the code.

**Why it is not a revert either.** V7's third branch — revert if turns per mission
rise with range gained unchanged — requires "unchanged". Turns did rise as
predicted (2.9 to 4.1 per mission, the expected cost of a detector that reads
more), but range gained did not stay put; it moved in the right direction on
three separate measures. Reverting on that would discard a signal, not a defect.

**Verdict: inconclusive. Repeat V7 on a soak of comparable size to the baseline**
(~170 turns, roughly an overnight run). If the median holds at +0.02R on 170
turns it is real and small; if it returns to 0.000R the effect here was sampling.

Crossings for the session were 5 over 29 missions (0.172), above the pooled 0.132
— and meaningless at that sample size, per ADR 106's resolution table.
