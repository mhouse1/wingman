# Performance Diagnostic 002 — Pre-Fix Control Curve (acct1)

| Status | Date       | Wingman Version |
|--------|------------|-----------------|
| Active | 2026-08-23 | 1.8.5           |

Memory curve of the **pre-ADR-091** `make r1` session on 2026-08-23 04:24-06:50,
recorded before the shared-display fix. Preserved here because `wingman.log` is
truncated by every `make rd`, and this is the control the post-fix session must
be compared against.

Same version (1.8.5), same account (acct1), same aircraft, same machine, no
instrumentation running. `run_20260823_042436_acct1.json` is the matching
performance record in `current/`.

| min | rss MB | mi_use MB | mi_free MB | ocr_med | ocr_p95 |
|-----|--------|-----------|------------|---------|---------|
| 0 | 683 | 140 | 0 | n/a | n/a |
| 5 | 2366 | 1428 | 225 | 0.23 | 0.35 |
| 20 | 2632 | 1505 | 427 | 0.25 | 0.40 |
| 40 | 2707 | 1680 | 322 | 0.27 | 0.54 |
| 60 | 2941 | 1902 | 330 | 0.27 | 0.55 |
| 80 | 3187 | 2194 | 280 | 0.31 | 0.71 |
| 100 | 3601 | 2594 | 290 | 0.29 | 1.06 |
| 120 | 3950 | 3009 | 218 | 0.34 | 1.24 |
| 145 | 4685 | 3639 | 320 | 0.55 | 2.53 |

Session summary: 2h 25m, 23 missions, 100% click-to finish, 0 spawn crashes.

**Post-warm-up rate: +952 MB/h** (mi_use, anchored after the 600 s warm-up;
`RESOURCE SUMMARY` reported +951 MB/h for RSS).

Two things this control fixes in place:

- **Memory.** mi_use reached 3,639 MB at 145 min.
- **OCR degradation.** `ocr_med` doubled (0.23 to 0.55) and `ocr_p95` rose 7x
  (0.35 to 2.53), crossing the 1.5 s tick at about 115 min. This is an
  independent signal: if memory flattens but OCR still degrades on the same
  curve, the degradation has a second cause that ADR 091 does not address.

## Pass criteria for the post-fix session — registered before it runs

This document has already recorded two confident explanations that measurement
destroyed. Both were judged after the fact. The thresholds below are fixed in
advance so the next result cannot be rationalised into a confirmation.

**Primary — post-warm-up `mi_use` rate**, from `RESOURCE SUMMARY`:

| result | verdict |
|--------|---------|
| under 100 MB/h | **PASS** — ADR 091 confirmed |
| 100 to 400 MB/h | **AMBIGUOUS** — partial effect, needs a 4h run |
| over 400 MB/h | **FAIL** — the fix is not the dominant term |

Control is +948 MB/h. Predicted post-fix is ~38 MB/h, which is below the
`_LEAK_RATE_MB_PER_H = 50` threshold at which `resource_monitor` declines to
call anything a leak.

**Secondary — equal-elapsed `mi_use`** against the table above at 20, 60, 100
and 145 min. Predicted ~1,516 MB at 145 min against the control's 3,639 MB.

**Tertiary — OCR, an independent signal.** `ocr_med` should stay near 0.25 and
`ocr_p95` below 1.0 for the whole session. If memory flattens but OCR still
climbs to 0.55 / 2.53, then ADR 091 fixed the leak and the degradation has a
second cause.

**Why two hours is enough here, despite the four-hour rule.** That rule was
written for marginal effects: the reader-churn fix predicted qualitative
improvement and measured +1,478 vs +1,645 MB/h, inside session variance. This
prediction is a 25x reduction, and the two curves separate by roughly 700 MB by
the 60-minute mark. Two hours settles it.

**What two hours will not settle.** Whether a second, slower leak sits
underneath. The residual ~38 MB/h is at the edge of the noise floor, and
separating a slow leak from ordinary drift still needs 4h+. Two hours validates
ADR 091; it does not close Performance 008.

**Setup.** `heap_census.enabled` must stay `false` — tracemalloc inflates the
OCR timings by 49-74% (Diagnostic 001) and would contaminate the tertiary
signal. `memory_guard` needs no change: predicted peak is about 2.5 GB against
a 6 GB soft limit.
