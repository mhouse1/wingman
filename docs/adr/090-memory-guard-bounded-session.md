# ADR 090 — Memory Guard: Bound the Session Instead of the Leak

| Status | Date       | Wingman Version |
|--------|------------|-----------------|
| Draft  | 2026-08-22 | 1.8.5           |

## Context

The Performance 008 leak is unfixed and, as of 2026-08-23, narrowed but not
found: growth is **88% live allocation** rather than fragmentation, invisible to
`tracemalloc`, and therefore native. EasyOCR — the assumed culprit since that
document was named — has been eliminated both single-threaded (0 KB retained
over 60 calls) and across a 13-thread pool (1 KB/call). Thread count, file
descriptors, and OCR queue depth are all flat.

The operational cost is measured and continuous. From the 6h 58m session of
2026-08-22:

| Hour | RSS | OCR median | OCR p95 |
|------|-----|-----------|---------|
| 1 | 2.8 GB | 0.28 s | 0.43 s |
| 3 | 5.0 GB | 0.49 s | **1.81 s** |
| 6 | 11.4 GB | **1.91 s** | 9.23 s |
| 8 | — | 4.92 s | 27.19 s |

Against a 1.5 s tick budget: **p95 crosses it at hour 3, the median at hour 6.**

Two properties make this worse than it looks:

**It degrades quality, not throughput.** Missions per hour stay flat (6–11)
across the whole session. Wingman keeps completing missions while its
perception cycles fall from 1,937/hour to 622/hour. Nothing visibly breaks; it
simply sees less. At hour 7 a p95 of 14 s means an incoming-missile alert can be
observed long after the evade would have mattered.

**The endpoint is a system-wide failure.** The session reached 13.2 GB and was
still climbing. The foundry cross-reference records a compositor OOM taking down
an entire desktop session at a comparable footprint.

Finding the allocation needs a native heap profiler (`heaptrack`), which is not
installed and is a separate piece of work. This ADR does not wait for it.

## Decision

Wingman bounds its own session. Two thresholds, deliberately different in
urgency.

### d1 — Soft limit stops at the next safe point

On crossing `memory_guard.soft_limit_mb` (default 6000) the guard **arms** and
the session ends at the next safe point, defined as `GAME_LOBBY` with no mission
running.

Waiting for that boundary is the whole point. Stopping mid-mission abandons an
aircraft in flight and loses the mission; stopping in the lobby costs nothing.
The soft limit is set well below the observed 13.2 GB so that the wait is
affordable — arming at 6 GB leaves hours of headroom at ~1.6 GB/h.

6 GB is chosen from the degradation curve, not from available RAM: it lands
between hour 3 (p95 over budget) and hour 4, so a session ends while its
perception is still usable rather than after it has become unreliable.

### d2 — Hard limit stops immediately

On crossing `memory_guard.hard_limit_mb` (default 10000) the session ends
regardless of state.

Past this point the risk of an OOM kill taking the desktop session with it
outweighs one abandoned mission. This is the only case where wingman
deliberately drops an aircraft in flight, and it is a considered trade rather
than an oversight.

### d3 — The guard stops; it does not restart

Wingman exits cleanly and writes its session summary. It does not re-exec
itself.

A self-restart would need to survive the portal capture session, thirteen
thread-local OCR readers, X connections, and a game process it does not own —
significant machinery to test in a path that only runs during a fault. Exiting
is the behaviour every existing supervisor, Makefile target, and operator habit
already handles.

Restart remains the operator's action, and the log line says exactly why the
session ended.

## Consequences

Unattended sessions become safe to leave running: the failure mode changes from
"desktop dies at 13 GB" to "wingman stops at 6 GB with a summary".

**Sessions now end on their own**, which is a behaviour change. A long soak that
previously ran until stopped will now terminate after roughly three to four
hours. That is the intent — it is also roughly where the existing
Performance 008 guidance already said to restart manually — but it means a
session ending is no longer necessarily a fault.

**This masks the leak.** With the guard active, RSS never reaches the values
that made the growth obvious, and OCR never degrades far enough to be
alarming. Any future leak investigation must raise or disable the limits to
reproduce, and Performance 008 should say so. A mitigation that hides its own
symptom is a real hazard, and the honest response is to name it here rather
than to leave the next investigator to rediscover it.

**It does not fix anything.** The allocation is still unfound. This ADR buys
safe operation and nothing else.

## Alternatives considered

**Restart on a timer.** Simpler, and roughly equivalent given a stable leak
rate. Rejected because the rate is not stable across sessions (+1,478 to
+1,784 MB/h measured) and a timer would fire early on a good session and late on
a bad one. Memory is the quantity that matters; time is a proxy for it.

**Trigger on OCR latency instead of RSS.** Closer to the actual harm — latency
is what degrades. Rejected as the primary trigger because latency also varies
with scene content and OCR load, so it would fire on a busy battle rather than
on a leak. Worth revisiting as a *second* trigger once there is a baseline for
normal per-scene variation.

**Do nothing until the leak is found.** Rejected on the 13.2 GB reading: the
next unattended overnight run is a plausible desktop OOM, and the diagnosis has
already outlived two refuted hypotheses.

## Validation

**V1 — soft limit waits.** With `soft_limit_mb` set low enough to trigger mid
session, the log shows `MEMORY GUARD armed` during a mission and the session
ends only after the next `GAME_LOBBY`, with the mission completed.

**V2 — hard limit does not wait.** With `hard_limit_mb` set low, the session
ends promptly and logs the hard-limit reason regardless of state.

**V3 — clean shutdown.** A guard-triggered exit writes the session summary,
performance artifacts, and mission stats exactly as a normal exit does.

**V4 — no effect below the limits.** A session that never crosses
`soft_limit_mb` behaves identically to one with the guard disabled.

## References

- Performance 008 — the leak; the degradation table above; the live-allocation finding
- Research 008 Lesson 5 — why the four-hour rule made both refutations possible
- foundry `docs/performance/001-brave-oom-full-session-crash.md` — the desktop OOM this bounds
