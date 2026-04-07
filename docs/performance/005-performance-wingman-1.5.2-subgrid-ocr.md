# Performance Doc 005 — Wingman Performance Report v1.5.2 (Sub-Grid OCR)

**Test Date:** 2026-03-21
**Wingman Version:** 1.5.2
**Test Type:** Runtime performance metrics (J20 mission, live gameplay with respawn and missile events)
**Device:** Primary workstation, GPU inference enabled (`OCR thread: initialized EasyOCR reader (GPU)`)
**Config:** Incoming 3×3 sub-grid (region 1) + Respawn 2×1 sub-grid (region 2)

---

## Executive Summary

With dual sub-grid cropping active, the parallel OCR cycle (respawn + incoming) completes in **0.78s median / 0.86s average** across 54 samples, down from 2.21–2.98s average in v1.4.0 (CPU-only, full-region scans). The incoming sub-grid accounts for the largest share of the gain. Respawn detection correctly triggers mission cancel on both respawn events observed in this session.

---

## OCR Timing Statistics (54 samples)

| Metric | Value |
|--------|-------|
| **Average (mean)** | **0.86s** |
| **Median (P50)** | **0.78s** |
| **P75** | 1.09s |
| **P90** | ~1.35s |
| **P95** | ~1.57s |
| **Minimum** | 0.27s |
| **Maximum** | 1.89s |

### Distribution

| Bucket | Count | Share |
|--------|-------|-------|
| < 0.50s | 10 | 18.5% |
| 0.50 – 1.00s | 20 | 37.0% |
| 1.00 – 1.50s | 16 | 29.6% |
| > 1.50s | 8 | 14.8% |

Over half of all cycles complete under 1.0s. The sub-1.0s fast path is driven by the sub-grid crop reducing pixel area — the incoming region is 1/9th the original size, the respawn region is 1/2.

### Log Samples (representative)

```
Respawn OCR: 0.38s | Incoming OCR: 0.36s | Total: 0.38s   ← fast floor
Respawn OCR: 0.27s | Incoming OCR: 0.27s | Total: 0.27s   ← fast floor
Respawn OCR: 0.78s | Incoming OCR: 0.77s | Total: 0.78s   ← typical
Respawn OCR: 1.01s | Incoming OCR: 1.00s | Total: 1.01s   ← moderate
Respawn OCR: 1.87s | Incoming OCR: 1.84s | Total: 1.87s   ← spike (respawn phase)
```

---

## Comparison vs Prior Versions

| Version | Config | Avg Total OCR | Min | Max |
|---------|--------|---------------|-----|-----|
| v1.4.0 (ADR 009) | Sequential, full regions, CPU | 2.21 – 2.98s | 1.26s | 4.39s |
| v1.5.2 (this report) | Parallel, sub-grids, GPU | **0.86s** | **0.27s** | **1.89s** |
| **Improvement** | | **~2.8× faster avg** | **~4.7×** | **~2.3×** |

The improvement combines three independent changes (ADR 016, ADR 019):
1. Multiprocessing → threading (eliminates IPC overhead, enables GPU)
2. Incoming 3×3 sub-grid (1/9th pixel area)
3. Respawn 2×1 sub-grid (1/2 pixel area)

---

## Respawn Detection Events

Two respawn events were captured in the log. Both were correctly detected and triggered mission cancel within one OCR cycle.

### Respawn Event 1 (04:55:20)

```
Respawn OCR results: [('gray', 'REPA', 0.572), ('binary', 'RESPA', 0.231)]
Respawn detected (variant: gray, text: REPA)
Analyzer: Parallel OCR Timings - Respawn OCR: 1.12s | Incoming OCR: 0.69s | Total: 1.12s
⚠ RESPAWN DETECTED - Cancelling active missions
```

During the respawn phase, incoming OCR drops significantly (0.29–0.38s) because the subgrid crop area has no complex HUD content — only the respawn overlay is visible.

### Respawn Event 2 (04:56:08)

```
Respawn OCR results: [('gray', 'REPA', 0.503), ('binary', 'REPA', 0.332)]
Respawn detected (variant: gray, text: REPA)
Analyzer: Parallel OCR Timings - Respawn OCR: 0.46s | Incoming OCR: 0.20s | Total: 0.46s
⚠ RESPAWN DETECTED - Cancelling active missions
```

Fastest detection in the log — 0.46s total cycle including both regions.

### Respawn OCR — Confidence Progression During Screen Fade

During active respawn, the OCR cycles repeatedly and confidence improves as the screen stabilises:

```
04:55:20  REPA  gray=0.572  binary=0.231 (RESPA)   ← first detection
04:55:23  REPA  gray=0.559  binary=0.231
04:55:24  REPA  gray=0.555  binary=0.382
04:55:26  RESPA gray=0.775  binary=0.425            ← full word visible
04:55:28  RESPA gray=0.261  binary=0.046            ← screen fading out
```

`REPA` (Levenshtein distance 1 from `RESPA`) is accepted by the matching function, allowing detection before the full word is rendered.

---

## Incoming Missile Detection

### Confirmed Detection

One clean detection at 04:54:56 with a 3-flare burst response:

```
🚀 INCOMING MISSILE DETECTED (variant=gray_up_1p4) - text='MING'
Analyzer: Parallel OCR Timings - Respawn OCR: 1.07s | Incoming OCR: 1.12s | Total: 1.12s
🚀 INCOMING MISSILE DETECTED - Deploying flares
[flare × 3, 0.3s apart]
🚀 Flare burst complete
```

Detection-to-response latency: ~42ms (from log timestamp to first `deploy_flares`).

### Simultaneous Respawn + Incoming (04:56:16)

Both signals detected in the same OCR cycle during the second respawn:

```
Respawn detected (variant: gray, text: RESPA)  [confidence: 0.943]
🚀 INCOMING MISSILE DETECTED (variant=gray_up_1p4) - text='MING'
Respawn OCR: 1.01s | Incoming OCR: 0.52s | Total: 1.01s
```

The parallel thread architecture correctly reports both results from a single cycle rather than blocking one on the other.

### Near-Miss: ARNIN / ARNIA Partial Reads

Five cycles returned `ARNIN` or `ARNIA` in the incoming sub-region without triggering detection:

```
04:54:50  raw OCR: gray_up_1p4='ARNIN', binary_otsu_up_1p4='ARNIA'   [0.90s]
04:55:05  raw OCR: gray_up_1p4='ARNIN', binary_otsu_up_1p4='ARNIA'   [1.02s]
04:55:14  raw OCR: gray_up_1p4='ARNIN', binary_otsu_up_1p4='ARNIA'   [0.89s]
04:55:47  raw OCR: gray_up_1p4='ARNIA', binary_otsu_up_1p4='ARNIA'   [0.54s]
04:56:02  raw OCR: gray_up_1p4='ARNIN', binary_otsu_up_1p4='ARNIA'   [0.43s]
```

These are partial reads of the in-game "WARNING" text (letters A-R-N-I-N-G). None contains `MING` or `ARNING` so they are correctly rejected. The pattern confirms the sub-region 1 crop is capturing the left portion of the warning label. No false positives resulted.

A missile lock was confirmed 6 seconds after the first `ARNIN` detection — these partial reads are leading indicators of an approaching detection event.

---

## OCR Variance Notes

GPU inference time is not strictly proportional to pixel count. Spikes above 1.5s occur sporadically even with sub-grid crops active. Observed causes:

- **Complex image content:** frames containing many bright pixels or noise take longer regardless of crop size
- **Respawn phase congestion:** during active respawn the thread pool is busy processing multiple queued frames (`Background OCR busy; will process latest pending frame next`)
- **GPU scheduler jitter:** even with GPU active, CUDA scheduling can introduce latency on individual calls

These spikes are non-blocking — the main loop never waits on OCR and uses the last cached result until the next cycle completes.

---

## Configuration (this run)

```yaml
respawn_detection:
  region: 44
  respawn_subgrid_rows: 2
  respawn_subgrid_cols: 1
  respawn_subregion: 2        # bottom half

  incoming_region: 21
  incoming_subgrid_size: 3
  incoming_subregion: 1       # top-left cell (1/9th area)
```
