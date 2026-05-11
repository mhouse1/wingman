# Job Aid 008 — Performance Regression Tracking Workflow

| Status | Date       | Wingman Version |
|--------|------------|-----------------|
| Draft  | 2026-05-10 | 1.6.6           |

## Overview

After ADR 031 is implemented, Wingman automatically tracks per-crop OCR timing and incoming → flare reaction latency across every session. Data accumulates on its own — no special setup is needed. The only intentional action you take is running `make wrelease` when you want to lock in a performance baseline.

---

## Phase 1 — First run (no baseline yet)

Run Wingman normally and stop it cleanly (`backspace` or Ctrl+C after the session).

At shutdown you will see:

```
[ROUND 4 — OCR crop timings | 312 cycles]
  crop          <0.10s  0.10-0.24s  0.25-0.49s  >=0.50s   mean    p95
  incoming        12%      68%        16%          4%      0.21s  0.42s
  respawn          6%      72%        18%          4%      0.22s  0.44s
  health          15%      65%        17%          8%      0.19s  0.38s
  ammo_flares     18%      69%        11%          2%      0.17s  0.34s
  ammo_missiles   19%      70%        10%          1%      0.17s  0.33s

[ROUND 4 — Reaction latency | 3 events]
  <0.25s  33%   0.25-0.49s  67%   0.50-0.99s  0%   >=1.00s  0%
  mean 0.38s   max 0.48s

[SESSION vs CURRENT PERIOD | this session: 8 rounds 312 cycles | period: 1 session 312 cycles]
  (all deltas 0% — only one session in period)

[PERIOD COMPARISON] accumulating baseline (N=1 sessions, 312 cycles — need 5 sessions and 1 000 cycles)
```

A file is written to `docs/performance/current/run_YYYYMMDD_HHMMSS.json`. This directory is gitignored and accumulates silently between releases.

---

## Phase 2 — Sessions 2–4 (outlier detection becomes useful)

Run additional sessions normally. Block 1 (session vs current-period aggregate) starts showing real deltas:

```
[SESSION vs CURRENT PERIOD | this session: 8 rounds 312 cycles | period: 3 sessions 936 cycles]
  crop            this session          period mean    delta
  incoming        mean 0.24s p95 0.49s   0.21s        +14% ↑
  respawn         mean 0.23s p95 0.45s   0.22s        + 5%
  reaction        mean 0.58s p95 0.91s   0.54s        + 7%  (7 events this session)

[PERIOD COMPARISON] accumulating baseline (N=3 sessions, 936 cycles — need 5 sessions and 1 000 cycles)
```

Use this to spot whether a particular session was an outlier (e.g. CPU throttling, game lag). Block 2 (regression vs release) is still skipped.

---

## Phase 3 — Session 5+ (full outlier detection)

Once `current/` has 5 sessions and 1,000 incoming OCR cycles, both blocks appear. Block 2 compares the current period against `release/`. Until you have run `make wrelease`, `release/` is empty and Block 2 is skipped with a note.

---

## Phase 4 — Locking in a baseline

When you are satisfied with the current code's performance, run:

```powershell
make wrelease
```

This copies all `docs/performance/current/` files into `docs/performance/release/` and commits them alongside the version bump. `current/` is left untouched.

From this point, every session's Block 2 compares against that baseline:

```
[PERIOD vs RELEASE v1.6.6 | current: 9 sessions 2 788 cycles | release: 21 sessions 6 300 cycles]
  crop            current mean   release mean   delta
  incoming          0.21s          0.21s        + 0%  —
  respawn           0.22s          0.22s        + 0%  —
  health            0.19s          0.19s        + 0%  —
  ammo_flares       0.17s          0.17s        + 0%  —
  ammo_missiles     0.17s          0.17s        + 0%  —
  reaction          0.54s          0.54s        + 0%  —
```

---

## Phase 5 — Detecting a regression after a code change

Make a code change, play 5+ sessions. Block 2 shows the drift:

```
[PERIOD vs RELEASE v1.6.6 | current: 7 sessions 2 100 cycles | release: 21 sessions 6 300 cycles]
⚠️ incoming       0.31s          0.21s        +48% ↑  REGRESSION
  respawn          0.22s          0.22s        + 0%  —
```

`⚠️ REGRESSION` fires when any crop mean or reaction mean deviates more than 20% from the release baseline. The flag names the specific crop so you know exactly where to look.

---

## Summary table

| Stage | Action | What you see |
|-------|--------|-------------|
| First run | Run normally, stop cleanly | Per-round histograms; Block 2 skipped |
| Runs 2–4 | Run normally | Block 1 (outlier detection) becomes meaningful |
| Run 5+ | Run normally | Both blocks active; Block 2 needs a `wrelease` baseline |
| Ready to baseline | `make wrelease` | `release/` locked in and committed |
| After code change, run 5+ | Run normally | Block 2 shows drift; `⚠️ REGRESSION` if >20% |

---

## What triggers a clean write (and what doesn't)

A session file is only written when Wingman shuts down cleanly — meaning the `ThreadPoolExecutor` shuts down successfully. Crashes, kill signals, and `taskkill` do not produce a file. This is intentional: crash data is excluded from the baseline.

Stop Wingman with `backspace` or Ctrl+C for the file to be written.

---

## References

- [ADR 031 — Round-End Histogram Reporting](../adr/031-round-end-histogram-reporting.md)
