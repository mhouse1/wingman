# Job Aid 005 — Updating the Performance Chart

| Status  | Date       | Wingman Version |
|---------|------------|-----------------|
| Retired | 2026-10-03 | 1.9.0           |

The test-based performance chart this job aid described was removed by
[ADR 158](../adr/158-retire-the-per-test-performance-chart.md). `make test-perf`,
`make test-perf-csv` and `make test-perf-chart` no longer exist, and
`make wrelease` no longer records a test-performance snapshot.

For performance regression tracking, use
[Job Aid 008 — Runtime Performance Regression Workflow](008-performance-regression-workflow.md).

To see which tests are slow, pass `--durations=20` to pytest:

```sh
uv run --active pytest --durations=20
```
