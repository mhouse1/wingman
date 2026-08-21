# Performance 008 — Long-Session Memory Leak and Progressive OCR Degradation

| Status | Date       | Wingman Version |
|--------|------------|-----------------|
| Active | 2026-08-20 | 1.8.0           |

## Summary

Wingman's OCR pipeline degrades **progressively and reproducibly** across a
long session. Respawn-crop OCR median rises from ~0.24 s in the first hour to
**4.85 s by hour nine**, p95 from 0.38 s to 16.9 s — against a 1.5 s main-loop
tick budget. All crops degrade uniformly, which is the signature of resource
starvation rather than a defect in any one OCR path.

The mechanism is a memory leak: system swap climbed **4.3 GB → 14.8 GB** during
the 2026-08-20 session and collapsed to 3.6 GB within minutes of exit. Every
session starts clean at ~0.25 s median regardless of how degraded the previous
one became, so **restarting wingman fully resets it**.

Downstream, the starvation manufactures false respawns (see "Secondary damage"),
and the safety-critical incoming→flare reaction path regressed **+533%**.

**Not the same phenomenon as the Brave/compositor OOM** tracked in
foundry `docs/performance/001-brave-oom-full-session-crash.md` — different
memory class, disjoint time windows, anti-correlated. See "Relationship to the
compositor OOM investigation".

## Evidence

### Progressive degradation (2026-08-20, 8h12m, 79 missions)

Respawn-crop OCR duration by hour of the session:

| Hour | Samples | Median | p95 | Max |
|------|---------|--------|-----|-----|
| 07 | 1768 | 0.24 s | 0.38 s | 0.73 s |
| 08 | 1844 | 0.26 s | 0.67 s | 2.52 s |
| 09 | 1811 | 0.32 s | 1.64 s | 8.03 s |
| 10 | 1652 | 0.47 s | 2.71 s | 12.50 s |
| 11 | 1492 | 0.66 s | 4.15 s | 20.97 s |
| 12 | 1040 | 0.86 s | 6.63 s | 22.79 s |
| 13 | 740 | 1.95 s | 10.65 s | 38.85 s |
| 14 | 474 | 2.55 s | 16.86 s | 33.69 s |
| 15 | 66 | 4.85 s | 16.46 s | 27.86 s |

Sample count per hour is itself a symptom: tick throughput fell by ~73%
(1768 → 474) as the loop slowed.

### Reproducible across sessions, resets on restart

| Session | Duration | Hour-1 median / p95 | Final-hour median / p95 |
|---------|----------|---------------------|-------------------------|
| 2026-08-19 23:22 | ~6 h | 0.26 s / 0.39 s | 0.44 s / 4.05 s |
| 2026-08-20 03:51 | ~2 h | 0.24 s / 0.40 s | 0.27 s / 1.15 s |
| 2026-08-20 07:07 | ~8 h | 0.24 s / 0.38 s | 4.85 s / 16.46 s |

Every session begins at the same clean baseline. The degradation is a function
of time-in-session, not of accumulated wall-clock or machine uptime.

### Memory correlation (host sampler, 10-minute cadence)

System swap during the 2026-08-20 session, from `~/.shell-cgroup-watch.log`:

| Time | Swap | Wingman |
|------|------|---------|
| 07:19 | 4,281 MB | yes |
| 09:25 | 12,371 MB | yes |
| 12:34 | 12,856 MB | yes |
| 14:40 | 14,762 MB | yes |
| 15:22 | 4,642 MB | **no** |
| 15:43 | 3,641 MB | no |

Across the full sampler history, bucketing each 10-minute delta by wingman state:

| Wingman | Samples | Mean Δswap | Mean Δshmem |
|---------|---------|------------|-------------|
| running | 742 | **+56.7 MB** | −85.5 MB |
| off | 1598 | −25.1 MB | +43.6 MB |

The leaked class is **anonymous memory** (swap-backed). Compositor-cgroup
`shmem` stays flat (~715 MB) throughout a wingman session.

### Regression against the release baseline

The performance gate is already flagging this (session-end report, 2026-08-20):

| Crop | Release baseline | Current period | Δ |
|------|------------------|----------------|---|
| incoming | 0.45 s | 1.06 s | +137% |
| respawn | 0.41 s | 1.00 s | +143% |
| health | 0.42 s | 1.00 s | +140% |
| telemetry | 0.66 s | 1.35 s | +106% |
| **reaction** (incoming→flare) | **0.39 s** | **2.48 s** | **+533%** |

Reaction latency is the safety-critical number: it is the delay between an
incoming-missile detection and the flare burst.

## Secondary damage: manufactured false respawns

OCR starvation produces health-confirmation gaps, which the ADR 064 weak-tier
fallback misreads as death-and-respawn episodes. Traced instance
(2026-08-20 13:14):

```
13:14:14  Respawn OCR: 8.42s                      <- pipeline starved
13:14:14  health read 250 unconfirmed (window=[250]) — holding previous value
13:14:14  Health respawn detector: death mark set (tier=weak)
13:14:16  health alive transition False→True — resetting health ceiling
13:14:16  HEALTH RESPAWN FALLBACK firing (tier=weak, dead_for=1.8s)
          → mission cancelled → restarted
```

The aircraft never died: health read 250 on both sides of the gap. Across the
session, **17 of 31 weak-tier fallback fires had `dead_for` < 3 s** (median
2.8 s, minimum 0.2 s). A real death→respawn cycle cannot complete that fast —
the respawn overlay alone displays for roughly 8 s.

This is what the session summary's `Spawn crashes: 5` / `redetect churn: 12`
counters are actually catching. They are not crashes.

**Proposed guard (not yet implemented):** a minimum-`dead_for` floor on
weak-tier fallback fires. The value is already computed at the fire site; a 3 s
floor rejects all 17 false fires while leaving the 14 legitimate ones
untouched. This would be a third gate alongside ADR 064's existing state gate
and alive-transition gate, and needs an ADR 064 amendment.

## What is not yet known

- **Which process leaks.** Wingman and the game (Metalstorm.exe under Proton)
  start and exit together in every observed session, so the sampler cannot
  attribute the growth. The discriminating experiment is below.
- **Which allocation path.** No memfd or `/dev/shm` growth accompanies it
  (both flat across the session), so it is ordinary heap/anon memory — EasyOCR
  tensors, OpenCV buffers, accumulated Python objects, or leaked X Display
  connections (`_linux_key_event` opens a fresh Display per key event) are all
  untested candidates.
- **Whether the rate is load-dependent.** The 2026-08-20 session averaged
  ~1.3 GB/h against a sampler-wide mean of ~340 MB/h while running, suggesting
  heavier combat leaks faster, but this is one session.

## Monitoring plan

The host sampler (`~/.shell-cgroup-watch.log`, systemd user timer, 10-minute
cadence) already records everything needed: `system swap used_mb`,
`cgroup_stat shmem_mb`, and a `context wingman=yes|no` flag. No new
instrumentation is required.

Per session, record:

1. **Session duration** and hour-by-hour OCR median/p95 (extract from the
   session log: `grep -o 'Respawn OCR: [0-9.]*s'` bucketed by hour).
2. **Swap at session start and end**, and the drop after exit.
3. **Weak-tier fallback fires with `dead_for` < 3 s** — the false-respawn count.
4. **Reaction latency** from the session-end performance report.

### Discriminating experiment (highest value, not yet run)

Leave the game running; restart **only** wingman once degradation is visible
(after ~4 hours, when p95 exceeds the tick budget). Then:

- If OCR timing resets to ~0.25 s and swap drops → the leak is in the wingman
  process.
- If timing stays degraded and swap stays high → the leak is in the game, and
  wingman is a victim rather than the cause.

### Interim mitigation

Restart wingman every ~3 hours. Hours 1–3 stay within budget (p95 < 1.5 s);
degradation becomes operationally significant from hour 4 onward.

## Relationship to the compositor OOM investigation

Foundry tracks a separate host-level failure in
`docs/performance/001-brave-oom-full-session-crash.md`: compositor-cgroup
`shmem` growing 6+ GB/hour **while the session is locked**, pinned
unevictable, until the OOM killer takes down the GNOME session. That
investigation already ruled out wingman's PipeWire screencast as its cause
(467 locked samples, all with `wingman=no`).

This document is a **different phenomenon**, and the two are cleanly separable:

| | Compositor OOM (foundry 001) | This leak |
|---|---|---|
| Memory class | `shmem`, pinned unevictable | anonymous (swap-backed) |
| Charged to | GNOME Shell cgroup | process heap |
| Occurs while | session **locked** | wingman **running** (always unlocked) |
| Mean Δ per sample | +43.6 MB shmem (wingman off) | +56.7 MB swap (wingman on) |
| Released by | unlocking | wingman exit |

They are **anti-correlated and never overlap in time**, because wingman only
runs while the operator is active.

They do, however, **compound as risk**: both consume the same 24.5 GB swap
device, and an 8-hour wingman session leaves the machine at ~14.8 GB swap used
before the operator locks the screen and the compositor growth begins from that
elevated baseline. Neither investigation should be closed on the strength of
the other's evidence.
