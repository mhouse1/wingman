# ADR 022 — Concurrency Safety Patterns

| Status   | Date       | Wingman Version |
|----------|------------|-----------------|
| Accepted | 2026-03-24 | 1.5.3           |

## Context

Wingman runs three concurrent subsystems on the main-loop path: background OCR (respawn + incoming), a low-frequency click-to poll thread, and a `ThreadPoolExecutor` for parallel region inference. Three recurring patterns in the codebase were found to introduce silent failure modes under this concurrency model, identified during an engineering quality review and fixed in this version.

Each pattern had an obvious-looking alternative that appears correct but fails in a specific way.

## Decisions

### 1. Lock release in finally blocks — `if locked(): release()` not `try/except`

**The naive pattern:**
```python
finally:
    try:
        self._mission_lock.release()
    except RuntimeError:
        pass
```

**The failure mode:** if `release()` raises (e.g. because the lock was never acquired due to an earlier exception), the `except RuntimeError: pass` swallows the error and the lock remains held. The next call to `mission_j20()` or `mission_loiter()` calls `acquire(blocking=False)`, gets `False`, and silently refuses to start — with no recovery path short of restarting the script.

**The adopted pattern:**
```python
finally:
    if self._mission_lock.locked():
        self._mission_lock.release()
```

This only releases if the lock is actually held. The condition is atomic on CPython's GIL, making it safe in the single-thread-per-mission model used here.

**Files:** `wingman/controller.py` — `mission_loiter` and `mission_j20` finally blocks.

---

### 2. Stoppable daemon threads — `Event.wait(timeout)` not `while True: time.sleep`

**The naive pattern:**
```python
while True:
    time.sleep(5.0)
    ...
```

**The failure mode:** the thread cannot be signalled to stop. `cleanup()` can shut down the `ThreadPoolExecutor`, but the thread loops back and calls `executor.submit(...)` on the now-shutdown executor, raising `RuntimeError: cannot schedule new futures after shutdown`. More broadly, daemon threads killed at interpreter shutdown mid-operation can produce spurious log output and leave shared state inconsistent.

**The adopted pattern:**
```python
# __init__
self._click_to_stop = threading.Event()

# thread body
while not self._click_to_stop.wait(timeout=5.0):
    ...

# cleanup()
self._click_to_stop.set()
```

`Event.wait(timeout)` returns `True` when the event is set (stop requested) and `False` on timeout (normal tick). This gives `cleanup()` a clean cooperative shutdown path without joining or killing the thread directly.

**File:** `wingman/analyzer.py` — `_run_click_to_in_background`, `cleanup()`.

---

### 3. Lock acquire on main-loop paths — `acquire(timeout=N)` not bare `with lock:`

**The naive pattern:**
```python
with self._background_ocr_lock:
    ...
```

**The failure mode:** if the background OCR thread stalls mid-operation (unhandled exception inside a `with` block that holds the lock), the main loop blocks on `with self._background_ocr_lock:` indefinitely. There is no watchdog and no recovery — the main loop simply hangs.

**The adopted pattern:**
```python
if not self._background_ocr_lock.acquire(timeout=5.0):
    logger.warning("Analyzer: background OCR lock timeout - skipping frame")
    return cached_result
try:
    ...
finally:
    self._background_ocr_lock.release()
```

The 5-second timeout is chosen to exceed the worst-case OCR inference time on CPU (measured at ~4.6s in ADR 017) while still being short enough to surface a genuine stall within one main-loop cycle.

Bare `with lock:` remains acceptable when both sides of the lock run in background threads — the constraint applies specifically to any lock acquired on the main-loop path.

**File:** `wingman/analyzer.py` — `_detect_respawn_ocr()`.

## Consequences

- Mission lock failures now surface immediately as log warnings rather than silently deadlocking future mission starts.
- The click-to thread exits cleanly on shutdown; `cleanup()` can be called safely from a `finally` block or context manager `__exit__`.
- A stalled background OCR thread causes the main loop to skip one frame and log a warning rather than hanging indefinitely.
- All three patterns are codified in `CLAUDE.md` so future contributors apply them consistently.

## References

- [ADR 004](004-background-ocr-threading-for-non-blocking-analysis.md) — non-blocking background OCR design
- [ADR 016](016-ocr-multiprocessing-to-threading-migration.md) — threading model and executor lifecycle
- [ADR 017](017-ocr-performance-gpu-vs-template-matching.md) — OCR timing baseline (4.6s worst case)
- [wingman/analyzer.py](../../wingman/analyzer.py) — `_detect_respawn_ocr`, `_run_click_to_in_background`, `cleanup`
- [wingman/controller.py](../../wingman/controller.py) — `mission_loiter`, `mission_j20` finally blocks
- [CLAUDE.md](../../CLAUDE.md) — project rules encoding these patterns
