# Performance 008 — Long-Session Memory Leak and Progressive OCR Degradation

| Status | Date       | Wingman Version |
|--------|------------|-----------------|
| Active | 2026-08-21 | 1.8.5           |

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

## Current status (2026-08-21)

| | |
|---|---|
| **Problem** | Wingman RSS grows without bound across a long session; OCR latency degrades in lockstep until it exceeds the 1.5s tick budget. |
| **Whose leak** | Wingman's own process. Game grows +157 MB/h against wingman's +1,530 MB/h, and the memory returns to the host on wingman's exit. |
| **Cause — partly known** | glibc arena fragmentation across the OCR thread pool. `MALLOC_ARENA_MAX=2` cuts the rate 69% (5,187 to 1,620 MB/h) and delays onset from hour ~2 to hour ~3. |
| **Cause — still unknown** | The residual +1,620 MB/h. Not yet attributed to a specific allocation path. |
| **Fixed?** | **No.** Mitigated only. A 5h43m session on 2026-08-21 reached 10.9 GB RSS with OCR median at 1.97s. |
| **Mitigation in force** | `MALLOC_ARENA_MAX=2` (Makefile `WINGMAN_ENV`), plus restarting wingman every ~3 hours. |
| **Next measurement** | `anon_mb` vs `rss_mb` on the RESOURCE line (added 2026-08-21) separates wingman's own heap from mapped capture buffers. Needs one 4h+ session. |

**Method rule.** No leak claim from a session shorter than four hours. Every
premature conclusion in this document came from measuring inside the flat early
window — hour 1 reads ~0.30s whether or not the leak is fixed.

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

## 2026-08-20 23:04 — attribution answered: the leak is wingman-side

The first session run with the new `RESOURCE` instrumentation settled the
attribution question in 25 minutes, and the leak is far more violent than the
8-hour session suggested:

| elapsed | wingman rss | game rss | threads | fds | gc gen2 |
|---------|-------------|----------|---------|-----|---------|
| 0 s | 681 MB | 1139 MB | 2 | 73 | 11 |
| 300 s | 4,598 MB | 1381 MB | 22 | 69 | 9 |
| 601 s | 7,112 MB | 1510 MB | 23 | 69 | 53 |
| 902 s | 10,144 MB | 1552 MB | 20 | 69 | 52 |
| 1202 s | 12,172 MB | 1591 MB | 19 | 69 | 53 |
| 1502 s | 15,879 MB | 1608 MB | 23 | 69 | 73 |

**+15.2 GB in 25 minutes (+36,300 MB/h)** while the game grew 469 MB. The
session was stopped manually at 15.9 GB with 6.5 GB of host memory remaining;
available memory returned to 22.3 GB immediately on exit, confirming wingman
held all of it.

What the instrumentation rules out directly:

- **Not the game** — 469 MB over the same window, two orders of magnitude less.
- **Not a thread leak** — thread count flat at 19–23 across the whole climb.
- **Not an fd leak** — 69 throughout, *below* the t=0 value of 73. This
  specifically exonerates the leaked-X-Display-connection hypothesis that
  Future 001 raised.
- **Not Python-tracked objects** — gc gen2 stayed in the 9–73 range while RSS
  grew 15 GB. `/proc/<pid>/smaps_rollup` showed **12.1 GB of private dirty
  anonymous** memory, i.e. native allocation (torch/OpenCV/EasyOCR), not
  Python heap the collector can see.
- **Not OCR degradation-driven** — `ocr_med` stayed 0.22–0.27 s and
  `pool_depth` stayed 0 for the entire climb. This is important: it means the
  **memory leak precedes the OCR degradation** rather than resulting from it.
  The 8-hour session's OCR collapse is downstream of memory pressure, not a
  peer symptom.

### Narrowing experiments — what is ruled OUT

Each was run as a bounded standalone script measuring `VmRSS` across a loop.
Recorded so the next investigator does not repeat them:

| Path | Test | Result |
|------|------|--------|
| Screen capture | `Capture.get_frame()` x120 | **Clean.** +53 MB pipeline warm-up, then exactly flat. 0 MB/frame. |
| Full analysis, static frame | `analyze_frame` x60 on one battle frame | **Plateau.** Rose to 3.1 GB (13 thread-local EasyOCR readers), fell back to 2.7 GB, flat. |
| Full analysis, varied frames | `analyze_frame` x120 cycling 9 distinct gate frames | **Plateau.** 1770 -> 2968 MB during pool warm, then flat from frame 30 onward. |
| Minimap components | `detect_enemy_map_components` x600 | **Clean.** +1 MB total. |
| Frame retention | source audit | **Clean.** Every frame attribute is single-slot (overwritten, not appended); every `deque` carries a `maxlen`. |

A correction worth recording: gc generation counts were initially read as
evidence against a frame-retention leak. That inference is invalid — **gc
counts objects, not bytes**, so a list holding 800 numpy frames is ~800 objects
(a negligible gen2 count) and 15 GB of RSS. The audit above, not the gc
numbers, is what actually rules retention out.

### Leading hypothesis: glibc arena fragmentation across the OCR thread pool

Everything reproducible in isolation plateaus; only the full live loop climbs.
The remaining candidate that fits every observation is allocator-level:

- glibc gives each thread its own malloc arena (up to 8 x cores). The OCR pool
  runs 13 workers, each allocating and freeing numpy/torch buffers of
  *varying* sizes every tick.
- Freed blocks are returned to their arena, not to the OS, and glibc's dynamic
  `M_MMAP_THRESHOLD` climbs as it observes large frees — so 18 MB frame-sized
  allocations that initially used `mmap` (returned on free) migrate to the
  heap (retained on free).
- The result is RSS that grows with tick count, is private dirty anonymous,
  is invisible to Python's gc, never appears in a short single-path test, and
  is released in full at process exit — which is every symptom observed.

**Cheap test, not yet run:** launch with `MALLOC_ARENA_MAX=2` (and optionally
`M_MMAP_THRESHOLD` pinned via `MALLOC_MMAP_THRESHOLD_=131072`) for a bounded
10-minute session and compare the `RESOURCE` slope against the 2026-08-20
23:04 baseline of ~3 GB per 5-minute interval. No code change is required to
test it. If the slope collapses, the fix is an environment setting in the
launcher plus a periodic `malloc_trim(0)` via `ctypes`.

If that does not explain it, the next step is `--tracemalloc` (Future 001
Tier 2) to catch any Python-side allocation, paired with an allocator-level
check, since a pure-native leak will not appear in tracemalloc at all.

The gap between the isolated tests (plateau) and live operation (linear climb)
is now the whole question. The difference is that live runs **fresh frames
every tick** through the full handler stack, where the tests reused one frame
through a single path.

**Next experiment:** `--tracemalloc` (Future 001 Tier 2) is now clearly
justified — the leak is confirmed wingman-side, so snapshot diffing will name
the allocation site directly. Native allocations will need `tracemalloc` plus
an allocator-level check, since the growth is not in the Python heap.

## 2026-08-21 — PARTIAL CAUSE: glibc arena fragmentation

> **Superseded as a root-cause claim.** This section's conclusion was retracted
> the same day — see "RETRACTION" below. Arena fragmentation is real and the
> mitigation cuts the rate by 69%, but it is not the whole cause and the leak
> is not fixed. The measurements here remain valid; only the verdict changed.

The hypothesis below was tested by launching with `MALLOC_ARENA_MAX=2` and no
code change. Identical workload (same `n_ocr`, same `ocr_med`), same host,
same game build:

| elapsed | default arenas | `MALLOC_ARENA_MAX=2` | reduction |
|---------|----------------|----------------------|-----------|
| 0 s | 681 MB | 684 MB | baseline match |
| 300 s | 4,598 MB | 2,453 MB | -47% |
| 601 s | 7,112 MB | 2,637 MB | -63% |
| 902 s | 10,144 MB | **2,545 MB** | **-75%** |

Interval growth is the decisive number: the 300→601 s interval grew
**+2,514 MB** with default arenas and **+184 MB** with the cap. RSS then went
*down* between the 601 s and 902 s samples (2,637 → 2,545 MB) — memory being
returned to the OS, which never happened in any prior session.

**~2.5 GB is wingman's real footprint** (13 thread-local EasyOCR readers), and
it is exactly the plateau the isolated `analyze_frame` tests reached. Every
byte above it in prior sessions was allocator fragmentation, not live data.

This also explains why every isolated test plateaued while live climbed: the
tests were effectively single-arena, whereas the live loop spreads allocations
across 13 worker arenas that never release to the OS. The tests were not
missing the leak — they were not able to reproduce its precondition.

**Fix applied:** `WINGMAN_ENV := MALLOC_ARENA_MAX=2` in the Makefile, applied
to all five wingman launch targets (`r`, `rd`, `newpaths`, `rr-path1`,
`rr-live-path1`). It must be set before the process starts, since glibc reads
it at first malloc — so this belongs in the launcher, not in Python.

## RETRACTION 2026-08-21 16:54 — the section above was measured too early

**The "ROOT CAUSE CONFIRMED" claims in the preceding section are wrong.
`MALLOC_ARENA_MAX=2` reduces the leak; it does not fix it.** They were drawn from a 56-minute session, and the
degradation does not become visible until roughly hour three. A 5h43m session
(55 missions, 100% click-to) shows the leak intact:

| Elapsed | RSS |
|---------|-----|
| 0.0h | 666 MB |
| 0.8h | 2,733 MB |
| 2.3h | 4,427 MB |
| 3.8h | 7,166 MB |
| 5.3h | 10,023 MB |

Sustained **+1,620 MB/h**, peak 11,101 MB, with the cap correctly applied
(verified in the `WINGMAN_ENV` line of the Makefile and in the process
environment). And the OCR degradation — the symptom this document exists for —
is fully present:

| Hour | n | median | p95 | max |
|------|---|--------|-----|-----|
| 11:00 | 1562 | 0.30s | 0.52s | 0.88s |
| 12:00 | 2008 | 0.37s | 0.92s | 2.73s |
| 13:00 | 1802 | 0.54s | 2.16s | 10.27s |
| 14:00 | 1595 | 0.81s | 4.19s | 23.04s |
| 15:00 | 1049 | 1.17s | 7.06s | 25.69s |
| 16:00 | 729 | **1.97s** | **11.22s** | **30.95s** |

Median rose 6.5x and throughput fell from 2,008 to 729 cycles/hour. Note that
hour 1 reads 0.30s — exactly the "flat" figure the retracted section cites. A
one-hour measurement cannot distinguish a fixed leak from an unfixed one.

**What the cap did achieve:** the rate fell from +5,187 MB/h to +1,620 MB/h
(-69%), and the onset of degradation moved from hour ~2 to hour ~3. Arena
fragmentation was therefore a real contributor, but not the whole cause. The
remaining +1,620 MB/h is unexplained and this document is reopened.

**Method rule going forward:** no leak claim from a session shorter than four
hours. Every premature conclusion in this document — including this author's —
came from measuring inside the flat early window.

### Consequences for the rest of this document — SUPERSEDED, see retraction above

A 56-minute session (9 missions, 100% click-to) with the cap in place settles
the downstream questions:

| Metric | Pre-fix | With `MALLOC_ARENA_MAX=2` | Release baseline |
|--------|---------|---------------------------|------------------|
| Memory growth rate | +5,187 MB/h | **+120 MB/h** | — |
| OCR median, hour 1 → hour 2 | 0.24 → 0.26 s (then 4.85 by hour 9) | **0.25 → 0.25 s** | — |
| Reaction latency (session) | 2.48 s period mean | **0.28 s** | 0.39 s |

The OCR curve is flat across the session instead of compounding, and reaction
latency is now *better than the release baseline*. The regression percentages
still shown in the session-end report are period aggregates that continue to
include pre-fix sessions; they will decay as capped sessions accumulate.

- The **OCR degradation** is downstream of memory pressure, but is NOT
  resolved — see the retraction above. It is delayed by roughly an hour and
  returns in full by hour 5.
- The **false-respawn cascade** is likewise downstream: it was caused by
  health-confirmation gaps under OCR starvation. The 2026-08-21 capped
  sessions recorded `Spawn crashes: 0` and no sub-3 s weak-tier fires. The
  proposed `dead_for` floor remains worth having as defence in depth, but it
  is no longer urgent.
- The **swap correlation** in the foundry cross-reference stands, but its
  magnitude should shrink dramatically: wingman at a 2.5 GB plateau no longer
  raises the swap baseline the compositor growth starts from.

Still open: whether `MALLOC_ARENA_MAX=2` costs measurable OCR throughput under
contention (13 threads sharing 2 arenas serialise more in malloc). `ocr_med`
held at 0.23-0.24 s through this session, matching the uncapped baseline, so
there is no early sign of a cost — but this needs a long session to confirm.

## What is not yet known

- ~~**Which process leaks.**~~ **Answered 2026-08-20 23:04: wingman.** See the
  section above — 36,300 MB/h against the game's 1,120 MB/h, with memory
  returned to the host on wingman's exit. The restart experiment is no longer
  needed.
- **Which allocation path.** No memfd or `/dev/shm` growth accompanies it
  (both flat across the session), so it is ordinary heap/anon memory — EasyOCR
  tensors, OpenCV buffers, accumulated Python objects, or leaked X Display
  connections (`_linux_key_event` opens a fresh Display per key event) are all
  untested candidates.
- **Why the observed rate varies so widely.** The 08-20 07:07 session implied
  ~1.3 GB/h from swap growth; the 23:04 session measured 36 GB/h directly.
  These are not necessarily inconsistent — swap growth measures *displacement*
  of other processes, which only begins once wingman has already consumed the
  free memory, so the earlier figure is a lower bound rather than the leak
  rate. Whether wingman plateaus near ~16 GB (its peak when stopped) or
  continues climbing is untested; the 23:04 session was halted before the
  answer was visible.

## Monitoring plan

### In-log instrumentation (added 2026-08-20, v1.8.0)

`wingman/resource_monitor.py` emits one `RESOURCE` line every
`resource_monitor.interval_s` (default 300 s, ~100 lines per 8-hour session),
starting with a t=0 baseline:

```
RESOURCE elapsed=8112s rss_mb=2841 swap_mb=1203 threads=31 fds=147
         gc=(412,29,7) ocr_med=0.47 ocr_p95=2.71 n_ocr=1652
         game_rss_mb=6210 game_swap_mb=3401 sys_swap_mb=12371
```

Every field earns its place against a specific open question:

| Field | Answers |
|-------|---------|
| `rss_mb`, `swap_mb` (self) vs `game_rss_mb`, `game_swap_mb` | **Which process leaks** — settles the attribution question without the restart experiment |
| `threads` | Daemon threads not being reaped |
| `fds` | Leaked X Display connections (`_linux_key_event` opens one per key event) |
| `gc=(g0,g1,g2)` | Python object accumulation vs native (EasyOCR/OpenCV) allocation. Secondary — `rss_mb` is the primary signal |
| `ocr_med`, `ocr_p95`, `n_ocr` | Degradation **for that interval only**, not cumulative — so the curve is readable inline |
| `sys_swap_mb` | Keeps the host correlation self-contained; no external sampler needed |
| `elapsed` | Session-relative bucketing without wall-clock arithmetic |

Notes: the OCR window is a read-only slice of the `PerformanceTracker` session
buffers (`snapshot_since`), so it cannot disturb the regression gate. Game
RSS/swap is summed across all matching processes and **double-counts shared
pages** — read it as a growth trend, never an absolute. Every probe is
individually guarded; a failing probe degrades to `n/a` rather than losing the
line, and the sampler cannot raise into the main loop.

Weak-tier fallback fires now also carry their diagnostic context:

```
HEALTH RESPAWN FALLBACK firing (tier=weak, dead_for=1.8s) — OCR missed this respawn
  [context: health_window=[250] last_respawn_ocr=8.42s]
```

which makes the false-respawn mechanism self-evident at the fire site rather
than something to reconstruct.

### Per-session analysis recipe

```bash
grep RESOURCE wingman.log            # the whole curve, one row per 5 min
grep -c "dead_for=[0-2]\." wingman.log   # false-respawn count (sub-3s fires)
```

Then record: session duration; `rss_mb`/`game_rss_mb` at t=0 vs end; whether
`threads` or `fds` grew; the `ocr_med` curve; the false-respawn count; and
reaction latency from the session-end performance report.

The host sampler (`~/.shell-cgroup-watch.log`) remains useful as an independent
cross-check and for the locked-session window that wingman cannot observe.

### Discriminating experiment — DONE, answered

~~Leave the game running; restart only wingman once degradation is visible.~~

Answered without needing the restart. The 2026-08-21 5h43m session measured
both processes directly: wingman +1,530 MB/h against the game's +157 MB/h, and
the host recovered the full 10.9 GB when wingman exited. **The leak is in the
wingman process.**

### Heap or mapped buffers? — ANSWERED 2026-08-21: heap

The 2h 19m acct1 session is the first with the `anon_mb` split. The result is
unambiguous:

| Elapsed | rss_mb | anon_mb | rss − anon |
|---------|--------|---------|------------|
| 0.0h | 685 | 379 | 306 |
| 0.5h | 2521 | 2202 | 319 |
| 1.0h | 3062 | 2742 | 320 |
| 1.5h | 3537 | 3218 | 319 |
| 2.0h | 4282 | 3962 | 320 |

**The non-anonymous portion is flat at ~320 MB for the entire session.** RSS grew
+3597 MB and anonymous memory grew +3583 MB — essentially all of it.

So the growth is **wingman's own heap**, not mapped capture buffers. The
PipeWire capture path is exonerated by direct measurement rather than by
inference, closing the ambiguity noted in the retraction above: `VmRSS` could
not distinguish the two, and now it does not have to.

This narrows the remaining +1,000–1,600 MB/h to allocator behaviour or genuine
Python-side retention within the process. The 2026-08-20 narrowing already ruled
out frame retention, thread growth, fd growth and Python object counts, and
`MALLOC_ARENA_MAX=2` removed the arena component it could reach — so the
residual is most likely fragmentation the cap does not address, but that is
inference, not measurement.

**Note on the four-hour rule.** This session is 2.3h, below the threshold for a
*rate* claim, and its +1,001 MB/h is reported as such. The attribution finding
above is not a rate claim: a constant rss−anon gap across 28 samples is
decisive regardless of session length.

### Superseded plan (kept for context)

`VmRSS` counts shared pages as well as heap, and wingman receives capture
buffers through the PipeWire pipeline the game feeds. So "growth in wingman's
address space" does not by itself distinguish:

- wingman retaining its own allocations → wingman's heap, or
- capture buffers accumulating → pages originating from the game's frames.

The RESOURCE line now carries the split, read from `/proc/self/smaps_rollup`:

```
RESOURCE elapsed=8112s rss_mb=2841 d_rss=+412 anon_mb=2610 d_anon=+390 shmem_mb=n/a ...
```

- `anon_mb` climbing with `rss_mb` → wingman's own heap (allocator or retention)
- `rss_mb` climbing while `anon_mb` stays flat → the capture path

`Shmem` is not exposed by every kernel (absent on this host) and degrades to
`n/a`; the `Anonymous` vs `Rss` comparison is the one that decides it. Needs a
single 4h+ session to read.

Prior evidence favours the heap: the 2026-08-20 narrowing found the growth
anonymous with no memfd or `/dev/shm` growth, and ruled out the capture path
and frame retention. But that was measured against the **pre-cap** leak; the
residual +1,620 MB/h has never been checked at this granularity.

### Interim mitigation

Two measures, both in force:

1. `MALLOC_ARENA_MAX=2` — set for every wingman launch target via `WINGMAN_ENV`
   in the Makefile. Cuts the growth rate 69%.
2. Restart wingman every ~3 hours. With the cap, hours 1–3 stay within budget
   (2026-08-21: median 0.30 s at hour 1, 0.37 s at hour 2, 0.54 s at hour 3);
   it becomes operationally significant from hour 4 (0.81 s) and severe by
   hour 6 (1.97 s median, 11.22 s p95).

**Avoid unattended overnight runs** until the residual is understood: the same
session peaked at 11.1 GB, which approaches the footprint that preceded the
compositor OOM in the foundry cross-reference.

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
