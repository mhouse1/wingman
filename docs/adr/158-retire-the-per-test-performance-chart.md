# ADR 158 — Retire the Per-Test Performance Chart

| Status | Date       | Wingman Version |
|--------|------------|-----------------|
| Draft  | 2026-10-03 | 1.9.0           |

Supersedes [ADR 034](034-two-tier-performance-regression-detection.md). Amends the
target list in [ADR 100](100-repository-growth-and-generated-artifacts.md) D8.

## Decision

**D1. The per-test performance chart (ADR 034 "Tier 1") is removed.** This
covers `tests/performance_tracking.py`, the `performance.json` snapshot that
`tests/conftest.py` wrote at session end, the `test-perf`, `test-perf-csv` and
`test-perf-chart` make targets, and the chart and record steps inside `tp`,
`tp-full` and `wrelease`.

**D2. The runtime tracker (ADR 034 "Tier 2") is the only performance regression
mechanism.** `PerformanceTracker`, `tests/runtime_performance_tracking.py`, the
`runtime-perf-*` targets and the thresholds in `wingman/config.yaml` are
unchanged.

**D3. Per-test timing collection stays in `tests/conftest.py`.** The
`test_timings` fixture feeds `test_level5_performance_validation`, which
compares the OCR tests against `tests/test_timing_baseline.yaml` inside every
`make test` run.

## Consequences

- `make tp`, `make tp-full` and `make wrelease` no longer write
  `tests/test-output/performance-trends.html`, `performance-history.csv` or
  `performance.json`.
- ADR 034's fast signal between a commit and the next runtime baseline is gone.
  What remains is coarse: the level 5 check covers eight tests with a tolerance
  of 5 s and only warns unless `--strict-timing` is passed. A crop-level
  slowdown smaller than that is first seen by the runtime tracker.
- To list slow tests, pass pytest's own `--durations=N`. No history is kept.
- `tests/perf-history/` on veda is no longer read or written. It stays
  gitignored so a leftover copy is not staged; it can be deleted by hand.
- `.gitattributes` is deleted. Its only entry suppressed diffs for a tracked
  `performance.json`, which has been untracked since ADR 100.
- [Job Aid 005](../job-aids/005-update-performance-chart.md) and
  [Performance Doc 001](../performance/001-performance-tracking.md) are marked
  Retired. ADR 100 D8 and V10 still name the `test-perf*` targets; read them as
  applying to the `runtime-perf-*` targets and `wrelease` only.

## Context

ADR 034 kept the chart because a small suite of real-OCR tests gave a same-day
timing signal per crop. The suite has since grown from 476 tests (v1.7.1) to
2,216, almost all of them mocked unit tests, and the chart draws one subplot per
test name. Measured on the report generated 2026-10-03 at v1.9.0:

| Measure | Value |
|---------|-------|
| Test names charted | 2,216 |
| Plotly traces | 6,649 |
| Figure height | 332,400 px |
| File size | 7.5 MB |
| Snapshots in the local history | 2 |

- The page does not open in a Chromium browser, so the chart had no reader.
- Typical durations are well under a millisecond, for example
  `test_prune_by_bytes` at 0.000216 s, measured once per run. Differences
  between versions at that scale are noise.
- No gate, threshold or test consumed the history.
- The history became local to veda under ADR 100 and holds two snapshots, so no
  trend is lost.

## Alternatives rejected

- **Chart only the slowest tests.** This would make the page load, but it keeps
  a history, a record step in `wrelease` and a report that nothing gates on.
  `--durations` answers the same question without them.

## Validation

- **V1.** `make test` passes with `tests/performance_tracking.py` removed and
  writes no `performance.json`.
- **V2.** `make tp` and `make wrelease` complete on veda without the chart and
  record steps.
