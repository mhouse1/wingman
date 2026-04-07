# Performance Doc 006 — Wingman Performance Report v1.6.0 (Dynamic Crop Regions)

| Status | Date | Wingman Version |
|--------|------|-----------------|
| Draft | 2026-04-07 | 1.6.0 |

## Scope

This report analyzes OCR runtime performance during GAME_BATTLE from the provided runtime log for v1.6.0 and compares it to the v1.5.2 baseline in Performance Doc 005.

- Source log: `log.md`
- Comparator: `docs/performance/005-performance-wingman-1.5.2-subgrid-ocr.md`
- OCR timing samples parsed: 63 (`Analyzer: Parallel OCR Timings ... Total`)

## Executive Summary

v1.6.0 is slower than v1.5.2 on this run.

- v1.6.0 mean total OCR: 1.03s
- v1.5.2 mean total OCR: 0.86s
- Change: +0.17s (+19.8%)

Median also regressed, and tail latency widened significantly due queued/background backlog periods during mode transitions.

## OCR Timing Statistics (v1.6.0)

| Metric | Value |
|--------|-------|
| Samples | 63 |
| Average (mean) total | 1.03s |
| Median (P50) total | 0.87s |
| P75 total | 1.19s |
| P90 total | 1.73s |
| P95 total | 1.88s |
| Minimum total | 0.35s |
| Maximum total | 5.78s |
| Mean respawn OCR | 0.67s |
| Mean incoming OCR | 0.90s |

### Distribution (Total)

| Bucket | Count | Share |
|--------|-------|-------|
| < 0.50s | 6 | 9.5% |
| 0.50s - 1.00s | 34 | 54.0% |
| 1.00s - 1.50s | 16 | 25.4% |
| >= 1.50s | 7 | 11.1% |

## Comparison vs v1.5.2

| Metric | v1.5.2 (Doc 005) | v1.6.0 (this report) | Delta |
|--------|-------------------|----------------------|-------|
| Samples | 54 | 63 | +9 |
| Mean total | 0.86s | 1.03s | +0.17s (+19.8%) |
| Median total | 0.78s | 0.87s | +0.09s (+11.5%) |
| P75 total | 1.09s | 1.19s | +0.10s |
| P90 total | 1.35s | 1.73s | +0.38s |
| P95 total | 1.57s | 1.88s | +0.31s |
| Min total | 0.27s | 0.35s | +0.08s |
| Max total | 1.89s | 5.78s | +3.89s |

## Representative Log Excerpts (v1.6.0)

Typical cycle range:

```text
2026-04-07 03:54:43,299 [DEBUG] Analyzer: Parallel OCR Timings - Extract: 0.00s, Submit: 0.00s | Respawn OCR: 0.30s | Incoming OCR: 0.35s | Total: 0.35s
2026-04-07 03:55:03,237 [DEBUG] Analyzer: Parallel OCR Timings - Extract: 0.00s, Submit: 0.00s | Respawn OCR: 0.60s | Incoming OCR: 0.71s | Total: 0.71s
2026-04-07 03:57:03,052 [DEBUG] Analyzer: Parallel OCR Timings - Extract: 0.00s, Submit: 0.00s | Respawn OCR: 0.75s | Incoming OCR: 0.67s | Total: 1.26s
```

Tail/outlier cycles:

```text
2026-04-07 03:55:28,575 [DEBUG] Analyzer: Parallel OCR Timings - Extract: 0.00s, Submit: 0.00s | Respawn OCR: 2.73s | Incoming OCR: 3.04s | Total: 5.78s
2026-04-07 03:55:22,784 [DEBUG] Analyzer: Parallel OCR Timings - Extract: 0.00s, Submit: 0.00s | Respawn OCR: 1.58s | Incoming OCR: 2.11s | Total: 2.11s
2026-04-07 03:55:10,545 [DEBUG] Analyzer: Parallel OCR Timings - Extract: 0.00s, Submit: 0.00s | Respawn OCR: 0.90s | Incoming OCR: 1.05s | Total: 1.96s
```

Backlog indicators in the same run:

```text
2026-04-07 03:55:10,081 [DEBUG] Background OCR busy; will process latest pending frame next
2026-04-07 03:55:10,546 [DEBUG] Background OCR processing latest pending frame
```

`Background OCR busy` appeared 10 times in this log, indicating queue pressure during heavy phases.

## Behavioral Notes from This Run

- Incoming detection still functioned (`INCOMING MISSILE DETECTED` appeared twice) with immediate flare burst behavior.
- No respawn detections occurred in this log segment (`Respawn detected` did not appear).
- A long GAME_STARTING phase was present; this overlaps with several slow OCR cycles and likely contributes to tail latency.

## Interpretation

Compared to v1.5.2 baseline, v1.6.0 in this run shows:

- Slower central tendency (mean and median)
- Larger long-tail latency (P90/P95/max)
- More evidence of background OCR backlog under load

This indicates throughput pressure rather than functional breakage: OCR still runs and events still trigger, but cycle completion time is less stable in this capture.

## Recommended Follow-Ups

1. Re-run the same scenario with identical GPU mode/settings used in Doc 005 for an apples-to-apples comparison.
2. Split timing statistics by game state (GAME_BATTLE vs GAME_STARTING) to isolate transition-driven spikes.
3. Add a rolling backlog metric (pending-frame depth / busy streak length) to logs for direct queue diagnostics.
