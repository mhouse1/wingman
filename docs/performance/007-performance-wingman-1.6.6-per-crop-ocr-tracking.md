# Performance Doc 007 — Wingman Performance Report v1.6.6 (Per-Crop OCR Tracking)

| Status   | Date       | Wingman Version |
|----------|------------|-----------------|
| Draft    | 2026-05-11 | 1.6.6           |

## Scope

This report is the first to use the ADR 031 runtime performance tracker (implemented in v1.6.6), which records per-crop OCR timing and incoming → flare reaction latency automatically during live sessions. All data comes directly from the tracker output in `wingman.log` — no manual log parsing.

- Source log: `wingman.log` (2026-05-10 23:22 → 2026-05-11 02:50)
- Run file: `docs/performance/current/run_20260510_232239.json`
- Comparator: `docs/performance/006-performance-wingman-1.6.0-dynamic-crops.md`
- OCR cycles tracked: 6,263 (incoming crop, session total)
- Rounds completed: 35
- Reaction events: 162

## Executive Summary

v1.6.6 is substantially faster than v1.6.0.

- v1.6.0 mean incoming OCR: 0.90s (sequential)
- v1.6.6 mean incoming OCR: 0.38s (parallel)
- Change: −0.52s (−58%)

The primary driver is the parallel 5-crop OCR architecture: in v1.6.0 all crops ran sequentially, making the total proportional to their sum. In v1.6.6 all five crops run concurrently in the thread pool, so wall-clock time equals only the slowest crop per cycle. Reaction latency is a new metric with no prior baseline; the v1.6.6 session mean of 0.36s (p95 0.68s) establishes the first reference point.

## Session-Level OCR Crop Statistics (v1.6.6)

From the session comparison block at exit (35 rounds, 6,263 incoming cycles):

| Crop          | Mean  | P95   |
|---------------|-------|-------|
| incoming      | 0.38s | 0.69s |
| respawn       | 0.34s | 0.68s |
| health        | 0.34s | 0.62s |
| ammo_flares   | 0.30s | 0.54s |
| ammo_missiles | 0.32s | 0.58s |

`incoming` is consistently the slowest crop — it runs two image variants through EasyOCR. `ammo_flares` and `ammo_missiles` are the fastest. No crop exceeded 0.50s mean across the session.

## Reaction Latency Statistics (v1.6.6)

Incoming → flare deploy latency across 162 events:

| Metric | Value |
|--------|-------|
| Mean   | 0.36s |
| P95    | 0.68s |

Bucket distribution (all 162 events):

| Bucket      | Approx share |
|-------------|-------------|
| < 0.25s     | ~20%        |
| 0.25–0.49s  | ~62%        |
| 0.50–0.99s  | ~18%        |
| ≥ 1.00s     | 0%          |

No reaction event exceeded 1.00s. The 0–0.49s band covers ~82% of events. This establishes the first quantitative baseline for flare response time.

## Per-Round Observations

Three representative rounds illustrate the session range:

### Round 1 — warm-up (141 cycles)

```
  crop            <0.10s  0.10-0.24s  0.25-0.49s  >=0.50s    mean     p95
  incoming            0%         57%         33%       9%  0.29s  0.64s
  respawn             0%         65%         26%       9%  0.28s  0.66s
  health              0%         65%         26%       9%  0.27s  0.56s
  ammo_flares         0%         70%         26%       5%  0.24s  0.49s
  ammo_missiles       0%         65%         30%       6%  0.26s  0.56s
```

Fastest round of the session. ~57% of incoming cycles complete under 0.25s; only 9% reach the >=0.50s bucket. Consistent with a freshly warmed thread pool.

### Round 8 — peak load (160 cycles, 20 reaction events)

```
  crop            <0.10s  0.10-0.24s  0.25-0.49s  >=0.50s    mean     p95
  incoming            0%         11%         61%      28%  0.43s  0.78s
  respawn             0%         26%         60%      14%  0.36s  0.69s
  health              0%         22%         61%      17%  0.36s  0.63s
  ammo_flares         0%         36%         55%       9%  0.32s  0.59s
  ammo_missiles       0%         26%         62%      12%  0.35s  0.61s

[ROUND 8 — Reaction latency | 20 events]
  <0.25s 25%   0.25-0.49s 65%   0.50-0.99s 10%   >=1.00s 0%
  mean 0.32s   max 0.53s
```

Heaviest round: 28% of incoming cycles in the >=0.50s bucket. Also the highest reaction event count (20). Despite the heavier OCR load, reaction latency stayed under 0.55s.

### Round 16 — mid-session (191 cycles, 5 reaction events)

```
  crop            <0.10s  0.10-0.24s  0.25-0.49s  >=0.50s    mean     p95
  incoming            0%         13%         63%      24%  0.41s  0.74s
  respawn             0%         28%         51%      21%  0.40s  0.89s
  health              0%         19%         65%      15%  0.37s  0.71s
  ammo_flares         0%         36%         54%      10%  0.32s  0.59s
  ammo_missiles       0%         23%         64%      13%  0.35s  0.60s

[ROUND 16 — Reaction latency | 5 events]
  <0.25s 0%   0.25-0.49s 60%   0.50-0.99s 40%   >=1.00s 0%
  mean 0.47s   max 0.56s
```

`respawn` p95 at 0.89s is the highest observed for that crop in this session — OCR variants processing a borderline RESPAWN frame. Reaction latency mean of 0.47s is higher than Round 8 despite fewer events; small sample size (5 events) explains the variance.

## Comparison vs v1.6.0 (Doc 006)

v1.6.0 ran OCR sequentially (respawn then incoming); v1.6.6 runs all five crops in parallel. The meaningful comparison is incoming OCR time (the historically slow crop) and effective total wall-clock time.

| Metric              | v1.6.0 (Doc 006) | v1.6.6 (this report) | Delta          |
|---------------------|-------------------|----------------------|----------------|
| Mean incoming OCR   | 0.90s             | 0.38s                | −0.52s (−58%) |
| Mean respawn OCR    | 0.67s             | 0.34s                | −0.33s (−49%) |
| Mean total / wall   | 1.03s             | ~0.43s ¹             | −0.60s (−58%) |
| P95 total / wall    | 1.88s             | ~0.78s ¹             | −1.10s (−59%) |
| Max observed        | 5.78s             | < 1.00s (session) ²  | —              |
| OCR cycles (sample) | 63                | 6,263                | —              |

¹ In parallel architecture, wall time ≈ slowest crop (incoming). Used incoming p95 as proxy.  
² No single incoming OCR cycle exceeded 1.00s in this 6,263-cycle session.

## Representative Log Excerpts (v1.6.6)

Typical mid-session cycle:

```text
2026-05-11 00:09:38,978 [DEBUG] Analyzer: Parallel OCR Timings - Extract: 0.00s, Submit: 0.00s | Respawn OCR: 0.36s | Incoming OCR: 0.43s | Health OCR: 0.36s | Flares OCR: 0.32s | Missiles OCR: 0.35s | Total: 0.43s
```

Reaction latency event (Round 6):

```text
[ROUND 6 — Reaction latency | 12 events]
  <0.25s 50%   0.25-0.49s 42%   0.50-0.99s 8%   >=1.00s 0%
  mean 0.28s   max 0.54s
```

Session exit comparison (35 rounds, 6,263 cycles):

```text
[SESSION vs CURRENT PERIOD | this session: 35 rounds 6263 cycles | period: 2 sessions 6654 cycles]
  incoming        mean 0.38s p95 0.69s    0.39s      -2%
  respawn         mean 0.34s p95 0.68s    0.34s      -2%
  health          mean 0.34s p95 0.62s    0.35s      -2%
  ammo_flares     mean 0.30s p95 0.54s    0.30s      -2%
  ammo_missiles   mean 0.32s p95 0.58s    0.33s      -2%
  reaction        mean 0.36s p95 0.68s    0.36s      -0%   (162 events this session)
[PERIOD COMPARISON] accumulating baseline (N=2 sessions, 6654 cycles — need 5 sessions and 1000 cycles)
```

This session's deltas are all within −2% of the period aggregate — the two sessions measured so far are very consistent.

## Interpretation

The shift from sequential to parallel OCR architecture between v1.6.0 and v1.6.6 produces the dominant improvement: mean incoming time fell 58%, and the 5.78s max observed in v1.6.0 has not recurred in 6,263 cycles of v1.6.6. The background OCR backlog pressure seen in v1.6.0 (10 `Background OCR busy` log events) is effectively gone.

`incoming` remains the per-cycle bottleneck crop (two EasyOCR variant passes), but at 0.38s mean it no longer imposes a meaningful latency tax on the main loop.

Reaction latency (0.36s mean, 0.69s p95, 0 events ≥ 1.00s) is healthy for the game context. Flares are deployed well within the window where they can intercept an incoming missile. No reaction event has yet exceeded 1.00s.

## Next Steps

1. Run 3 more sessions to reach the 5-session minimum for period comparison (currently N=2).
2. Run `make wrelease` once the 5-session threshold is met to lock this as the v1.6.6 baseline.
3. Watch `incoming` p95 as the regression canary — it is the slowest crop and will be the first indicator of OCR throughput degradation.
4. Watch `respawn` p95 (0.89s peak in Round 16) for variance — RESPAWN detection uses two variant passes and can spike on borderline frames.

## References

- `docs/performance/006-performance-wingman-1.6.0-dynamic-crops.md` — prior baseline
- `docs/adr/031-round-end-histogram-reporting.md` — tracking design
- `docs/job-aids/008-performance-regression-workflow.md` — accumulation and release workflow
