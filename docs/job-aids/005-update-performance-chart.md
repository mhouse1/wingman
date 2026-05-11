# Job Aid 005 — Updating the Performance Chart

| Status | Date       | Wingman Version |
|--------|------------|-----------------|
| Active | 2026-05-11 | 1.6.6           |

## Two performance systems

Wingman has two complementary performance tracking systems:

| System | What it measures | Where data lives |
|--------|-----------------|------------------|
| **Test-based chart** (this doc) | Automated OCR test accuracy and speed across git history | `tests/test-output/performance.json` → `performance-trends.html` |
| **Runtime tracking** (Job Aid 008) | Per-crop OCR timing and reaction latency during live sessions | `docs/performance/current/` → `docs/performance/release/` |

Both are snapshotted by `make wrelease`. This document covers the test-based chart.

---

## Updating the chart

### 1. Run the performance tests

```sh
make test-perf
```

This runs all automated tests, updates `tests/test-output/performance.json` with the latest results, and generates the CSV and HTML chart files.

### 2. Preview before committing (optional)

```sh
make tp
```

Runs the test suite and generates the chart with the current (uncommitted) `performance.json` included as a preview point. The preview point is not added to history. Open `tests/test-output/performance-trends.html` to review.

Typical workflow when evaluating a change:

```sh
make tp        # check results look right
make wrelease  # commit to history once satisfied
```

### 3. Commit and snapshot

```sh
make wrelease
```

This:
- Force-adds `tests/test-output/performance.json` to git (it is normally gitignored)
- Copies all `docs/performance/current/run_*.json` files into `docs/performance/release/` as the new runtime baseline
- Commits both with the current `WINGMAN_VERSION` and `WINGMAN_VERSION_DETAILS`
- Regenerates the chart

### 4. View the chart

Open in your browser:

```
tests/test-output/performance-trends.html
```

The x-axis shows `WINGMAN_VERSION`. Multiple data points for the same version are supported — each `wrelease` commit is stored as a separate entry.

---

## Adding multiple data points for the same version

Run the workflow repeatedly to track performance across different runs or environments within a single release:

```sh
make test-perf
make wrelease   # first data point for v1.6.6

# change test conditions or rerun...

make test-perf
make wrelease   # second data point for v1.6.6
```

All points appear under the same version label on the chart.

---

## Troubleshooting

**New data point not showing up?**
- Confirm you ran `make wrelease` (not just `make test-perf`).
- Refresh the HTML file in your browser — it does not auto-reload.

**`make wrelease` says "No staged changes to commit"?**
- `performance.json` was not updated. Run `make test-perf` first.

---

## References

- [Job Aid 008 — Runtime Performance Regression Workflow](008-performance-regression-workflow.md)
- [ADR 031 — Round-End Histogram Reporting](../adr/031-round-end-histogram-reporting.md)
