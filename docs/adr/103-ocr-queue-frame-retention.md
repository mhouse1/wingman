# ADR 103 — OCR Queue Frame Retention

| Status | Date       | Wingman Version |
|--------|------------|-----------------|
| Draft  | 2026-09-01 | 1.8.8           |

## Context

On 2026-09-01 a 4h30m session — 40 missions, 100% click-to-finish — failed the
ADR 092 leak gate at **+252 MB/h** and blocked the release. The session median
across 30 sessions is +1 MB/h.

The growth was not a leak. It was one step:

```
07:41 → 10:42   ~2400-2545 MB      flat for three hours
11:07:25        rss=2553  mi_use=1364
11:17:26        rss=3688  mi_use=2440      +1135 MB in ten minutes
11:22 → 11:37   ~3690 MB            flat again
```

The cause was external:

```
11:11:38  popup 'event_refresh' (text='ANCEINPROGRESS2TRYAGAINSOON_RYAGAIN')
11:41:36  LIVENESS GUARD: ending session (no progress for 901s)
```

The game servers went into maintenance. OCR completions fell from 306 per five
minutes to 163 and then to zero after 11:09, and the ADR 093 liveness guard
ended the session at its hard limit.

### Why a stalled pool costs a gigabyte

`ThreadPoolExecutor(max_workers=13)` has an **unbounded** queue, and every task
the quick-scan submitted carried the **whole frame** — 1920x1200x3 is 6.9 MB.
`get_crop` ran inside the worker by design, so the crop would sit under the
caller's `result(timeout=N)`.

While the workers keep up this is invisible. When they stall, each scan cycle
submits another batch against another frame, and every queued task pins its
frame for as long as it sits in the queue. About 160 frames is 1.1 GB, which is
the step. It plateaus because the loop eventually blocked on a future and
stopped submitting.

Two further details made it worse than it looks. A cycle submits every lobby
crop but the handlers `break` on the first hit, so up to three futures per cycle
were never read at all. And `result(timeout=20)` raising leaves the future
queued.

## Decision

**D1. Crop in the caller, and detach the copy.** The quick-scan now submits
`_process_text_region` with `_crop_for_ocr(frame, coords)` rather than
`_process_crop_region` with the frame. A lobby crop is tens of KB, so a backlog
costs megabytes instead of gigabytes.

The copy is load-bearing, not defensive. `get_crop` returns a numpy **view**
whose `.base` is the frame, so cropping in the caller without copying would pin
all 6.9 MB exactly as before. `np.ascontiguousarray` detaches it.

**D2. Cancelling the futures is not the fix.** It was the first thing tried and
it does not work: CPython leaves the `_WorkItem` — and its arguments — in the
executor queue until a worker pops it, which is precisely what a stalled pool
never does. `cancel()` marks the future and frees nothing. A test asserts this
directly, so the mistake is not available to be made twice.

The unconsumed futures are still cancelled in a `finally`, for the smaller
benefit of not running OCR nobody will read once the pool drains. That block is
commented as explicitly not the memory fix.

**D3. Retire the timeout argument for cropping in the worker.**
`_process_crop_region`'s docstring justified the placement on the grounds that a
synchronous `get_crop` "can block indefinitely". It cannot — it is a bounded
numpy slice and copy. The function is retained for callers that crop a frame
they are about to discard, with the reasoning corrected rather than repeated.

**D4. Do not bound the queue.** A `Semaphore` around submissions would also cap
the memory, but it caps it at whatever the bound is times 6.9 MB, and it adds a
failure mode — the choice of what to drop when the bound is hit — to a path that
now has none. Making each queued item small removes the problem instead of
sizing it.

## Consequences

An OCR stall now costs tens of KB per queued crop rather than 6.9 MB per queued
frame — roughly a hundredfold reduction on the path that produced the step.

The crop copy moves to the submitting thread. It is a bounded memcpy of a region
the worker was going to make anyway; the work is the same, the thread differs.

This does not prevent stalls, and it does not stop the liveness guard ending a
session that is making no progress — both of those worked correctly here. It
stops a stall from also looking like a memory leak.

The failing gate reading for the 2026-09-01 session stands as a historical
artifact: it measured a real step, correctly, and attributed it to a rate.

## Validation

- **V1.** A queued task holding a whole frame keeps that frame alive, and
  `cancel()` does not release it. (Asserts the premise and the rejected fix.)
- **V2.** A detached crop handed to a queued task does not keep the frame alive.
- **V3.** `_crop_for_ocr` returns an array with no `.base`.
- **V4.** Every `executor.submit` in the quick-scan passes a detached crop.
- **V5.** Cancelling a future that already completed does not disturb its result.
- **V6 — live.** A session that stalls shows no step in RSS. Not yet observed;
  this ADR is Draft until one is.

## References

- ADR 092 — the leak gate this failed, and its 100 MB/h threshold
- ADR 093 — the liveness guard that correctly ended the stalled session
- Performance 008 — the standing memory-growth investigation
- `wingman/analyzer.py` — `_crop_for_ocr`, `_process_crop_region`,
  `_run_game_lobby_quick_scan`
