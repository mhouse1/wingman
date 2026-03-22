# Code Review — Engineering Debt TODO

Generated from engineering quality review (2026-03-20).
Items are grouped by severity and area. Each item references the exact file and line(s) to change.

---

## Priority 1 — Correctness / Will crash or misbehave

### 1.1 Dead code: `calibrate_respawn_detection` references uninitialised attributes

**File:** [wingman/analyzer.py](../wingman/analyzer.py) — lines 976, 982, 984, 986, 988
**Issue:** `calibrate_respawn_detection()` reads `self.respawn_bar_hsv_lower`, `self.respawn_bar_hsv_upper`, `self.respawn_text_threshold`, and `self.respawn_bar_threshold`. None of these are initialised in `__init__`. Calling the method raises `AttributeError`.
**Fix options:**
- Delete the method entirely — it is a debug utility superseded by the OCR approach.
- Or initialise the four missing attributes in `__init__` and document the method as a manual debug tool.

---

### 1.2 Click-to background thread is never joined on shutdown

**File:** [wingman/analyzer.py](../wingman/analyzer.py) — lines 547–549, 512–519
**Issue:** `_run_click_to_in_background` is a daemon thread started on first frame and runs an infinite `while True` loop. `cleanup()` shuts down the `ThreadPoolExecutor` but does not signal or join this thread. On interpreter shutdown the thread is killed mid-operation, which can produce spurious log lines and leave the executor in an inconsistent state.
**Fix:** Add a stop `threading.Event` (e.g. `_click_to_stop`) to `__init__`. Check it in the loop. Set it in `cleanup()` before joining.

---

### 1.3 `ThreadPoolExecutor` lifecycle not guaranteed

**File:** [wingman/analyzer.py](../wingman/analyzer.py) — lines 488–499, 512–519
**Issue:** The executor is only shut down if `cleanup()` is called. If the caller forgets (or an exception bypasses the `finally` in `main.py`), the pool's worker threads and their EasyOCR model references are never released.
**Fix:** Implement `__enter__` / `__exit__` on `GameStateAnalyzer` so it can be used as a context manager, or add `__del__` as a fallback that calls `cleanup()`. Update `main.py` to use `with GameStateAnalyzer(cfg) as analyzer:`.

---

## Priority 2 — Robustness / Will silently fail or leak under load

### 2.1 Hotkey handlers have no timeout guard

**File:** [wingman/controller.py](../wingman/controller.py) — lines 107–114, 753–765
**Issue:** `_do_ocr_scan()` called from `_start_game_starting_loop` submits a task to the OCR executor and calls `.result()` with `timeout=30`. If EasyOCR hangs (GPU OOM, model load failure) the hotkey handler thread blocks for 30 seconds. During that window the J20 key press loop stalls entirely.
**Fix:** Wrap the `.result()` call in a `try/except TimeoutError`. Log the timeout and return `False` so the loop continues. Consider reducing the timeout to 10 s to match the loop's 5 s polling interval.

---

### 2.2 `_run_ocr_in_background` has no timeout on lock acquire

**File:** [wingman/analyzer.py](../wingman/analyzer.py) — lines 659–674
**Issue:** `with self._background_ocr_lock:` will block indefinitely if the lock is held by a thread that has stalled (e.g. mid-exception). No recovery path exists.
**Fix:** Use `self._background_ocr_lock.acquire(timeout=5.0)`. If acquire fails, log a warning and skip the current frame rather than blocking the main loop.

---

### 2.3 `mss` screen capture not guarded against monitor disconnect

**File:** [wingman/capture.py](../wingman/capture.py) — lines 27–34
**Issue:** `get_frame()` calls `self.sct.grab()` with no exception handling. If the monitor is disconnected, the screen locks, or the region falls outside current display bounds, `mss` raises and crashes the main loop.
**Fix:** Wrap `sct.grab()` in `try/except Exception` and return `None`. Update callers (`main.py` line 111) to handle a `None` frame gracefully (log and `continue`).

---

### 2.4 `click_grid_region` has no platform guard

**File:** [wingman/controller.py](../wingman/controller.py) — lines 688–693
**Issue:** `ctypes.windll.user32` is Windows-only. On Linux or macOS this raises `AttributeError: module 'ctypes' has no attribute 'windll'` at call time, not import time — so the error surfaces mid-operation rather than at startup.
**Fix:** Add a platform check at the top of `_do_click()`:
```python
import sys
if sys.platform != "win32":
    logger.error("click_grid_region: Win32 mouse_event not available on %s", sys.platform)
    return
```
This also documents the Windows dependency explicitly for future ADB migration (see [ADR 018](adr/018-adb-input-injection-and-remote-control-architecture.md)).

---

### 2.5 `get_frame()` uses a shared `mss` instance across calls

**File:** [wingman/capture.py](../wingman/capture.py) — lines 12, 27
**Issue:** `self.sct = mss()` is created once in `__init__`. `mss` uses thread-local storage internally. If `get_frame()` is ever called from a thread other than the one that constructed `Capture`, the grab silently uses the wrong context. `controller.py` already works around this by creating a fresh `mss()` instance in daemon threads (lines 158–165, 667–668) — but `capture.get_frame()` itself is not protected.
**Fix:** Document the single-thread contract in a docstring on `get_frame()`, or make `get_frame()` always create a short-lived `mss()` context (matching the pattern already used in controller).

---

## Priority 3 — Performance / Wastes resources unnecessarily

### 3.1 Tight poll loop during `loop_interval_sec` sleep

**File:** [wingman/main.py](../wingman/main.py) — lines 206–212
**Issue:** The main loop spins at 20 Hz (`time.sleep(0.05)`) checking `_deploy_flares_on_new_incoming()` during the configured sleep interval. This burns CPU continuously even when no events are occurring.
**Fix:** Add a `threading.Event` (e.g. `_incoming_event`) that `_run_ocr_in_background` sets whenever a new incoming result is written to the cache. The main loop can then `_incoming_event.wait(timeout=remaining)` instead of spinning. On wake, check the cache and clear the event.

---

### 3.2 `import time` inside method body

**File:** [wingman/analyzer.py](../wingman/analyzer.py) — line 765
**Issue:** `_run_click_to_in_background` re-imports `time` at every call. `time` is already available at module level.
**Fix:** Remove the local import. One line change.

---

## Priority 4 — Code hygiene / Confusing or misleading

### 4.1 Duplicate import in `capture.py`

**File:** [wingman/capture.py](../wingman/capture.py) — lines 1–5
**Issue:** `from mss import mss` appears twice (line 1 and line 4).
**Fix:** Remove the duplicate. One line change.

---

### 4.2 ANSI colour codes without a Windows terminal guard

**File:** [wingman/controller.py](../wingman/controller.py) — lines 256–268, 576, 583, 591, 627 and throughout
**Issue:** Raw `\033[9Xm` escape sequences are written directly into log strings. On Windows, the default `cmd.exe` and older PowerShell versions do not render ANSI codes — the sequences appear as literal characters in the log output. `colorama` is not listed in `requirements.txt` or `pyproject.toml`.
**Fix options:**
- Add `colorama` to dependencies and call `colorama.init()` at startup in `main.py`.
- Or strip the ANSI codes from log strings and use Python's `logging` formatters with a coloured handler (e.g. `rich` or `colorlog`) that is platform-aware.

---

### 4.3 Emoji in log strings — encoding risk

**File:** [wingman/main.py](../wingman/main.py) — lines 93, 117, 129, 144, 197
**File:** [wingman/controller.py](../wingman/controller.py) — lines 418, 475, 506, 576, 686, 706
**Issue:** Emoji characters in log strings (🚀, 🎮, ⚠, 📋) will raise `UnicodeEncodeError` on Windows terminals or log file handlers configured with a non-UTF-8 encoding (the Windows default is `cp1252`).
**Fix:** Either replace emoji with plain ASCII tags (e.g. `[INCOMING]`, `[STATE]`, `[RESPAWN]`) or set `encoding='utf-8'` explicitly on the `FileHandler` in `main.py`'s logging setup.

---

### 4.4 Typos in log/comment strings

**File:** [wingman/controller.py](../wingman/controller.py)

| Line | Current | Should be |
|------|---------|-----------|
| 576 | `"initiated roll_right while afterburner loop is active"` | `"initiating roll_right while afterburner loop is active"` |
| 591 | `"initiating finall roll right 300sec"` | `"initiating final roll right 300 sec"` |

---

### 4.5 `restart_last_mission()` tri-state return value is undocumented

**File:** [wingman/controller.py](../wingman/controller.py) — lines 816–834
**File:** [wingman/main.py](../wingman/main.py) — lines 178–187
**Issue:** The method returns `True` (restarted), `False` (lock held), or `None` (no previous mission). This is non-obvious. `main.py` handles all three cases correctly but only because it was written by the same author. A future contributor will likely treat `None` as falsy and collapse two distinct cases.
**Fix:** Add a docstring to `restart_last_mission()` spelling out the three return values and what each means. Or replace with a named enum/dataclass result.

---

## Priority 5 — Test coverage gaps

### 5.1 No test for `cleanup()` path

**What's missing:** A test that constructs `GameStateAnalyzer`, calls `analyze_frame()` once (to start the click-to thread and executor), then calls `cleanup()` and verifies the executor is shut down and no threads are left running.
**File to add test to:** [tests/](../tests/) — new file `test_analyzer_lifecycle.py`.

---

### 5.2 No test for hotkey registration failure

**What's missing:** A test that constructs `Controller` with `keyboard_module = None` and verifies it initialises without exception and that all game-control methods (`deploy_flares`, `mission_j20`, etc.) degrade gracefully.
**File to add test to:** [tests/](../tests/) — `test_controller.py` or new `test_controller_no_keyboard.py`.

---

### 5.3 No test for mission cancellation race

**What's missing:** A test that starts `mission_j20` in a thread, immediately calls `cancel_mission()`, and asserts: (a) the mission lock is released within 2 seconds, (b) no keys are pressed after cancellation is set.
**File to add test to:** [tests/](../tests/) — new file `test_mission_cancel.py`.

---

### 5.4 No test for `get_frame()` failure handling (after fix 2.3)

**What's missing:** Once `get_frame()` is wrapped in try/except (fix 2.3 above), add a test that mocks `sct.grab` to raise and asserts `get_frame()` returns `None` rather than propagating.
**File to add test to:** [tests/](../tests/) — `test_capture.py`.

---

## Priority 6 — Configuration defaults

### 6.1 Monitor index defaults to 2 (secondary monitor)

**File:** [wingman/config.yaml](../wingman/config.yaml)
**Issue:** `region.monitor: 2` means Wingman targets the secondary monitor by default. Most single-monitor setups will silently fail (mss raises `ValueError` on an out-of-range monitor index).
**Fix:** Change default to `monitor: 1`. Add a comment explaining that 1 = primary, 2 = first secondary.

---

### 6.2 Region defaults assume 1920×1200 without documentation

**File:** [wingman/config.yaml](../wingman/config.yaml)
**Issue:** The default `region` values (width, height, left, top) are calibrated for a specific display resolution with no comment explaining this.
**Fix:** Add a comment block above the `region:` key documenting the assumed resolution and how to recalibrate using the V key screenshot + grid overlay.

---

## Tracking

| # | Area | Priority | Status |
|---|------|----------|--------|
| 1.1 | Dead code: `calibrate_respawn_detection` uninitialised attrs | P1 | Resolved |
| 1.2 | Click-to thread never joined on shutdown | P1 | Open |
| 1.3 | `ThreadPoolExecutor` lifecycle not guaranteed | P1 | Open |
| 2.1 | Hotkey handler timeout guard missing | P2 | Open |
| 2.2 | `_background_ocr_lock` acquire has no timeout | P2 | Open |
| 2.3 | `mss` grab not guarded against monitor disconnect | P2 | Open |
| 2.4 | `click_grid_region` has no platform guard | P2 | Open |
| 2.5 | `get_frame()` shared `mss` instance thread contract undocumented | P2 | Open |
| 3.1 | Tight poll loop during sleep interval | P3 | Open |
| 3.2 | `import time` inside method body | P3 | Open |
| 4.1 | Duplicate import in `capture.py` | P4 | Open |
| 4.2 | ANSI codes without Windows terminal guard | P4 | Open |
| 4.3 | Emoji in log strings — encoding risk | P4 | Open |
| 4.4 | Typos in log/comment strings | P4 | Open |
| 4.5 | `restart_last_mission()` tri-state return undocumented | P4 | Open |
| 5.1 | No test for `cleanup()` path | P5 | Open |
| 5.2 | No test for hotkey registration failure | P5 | Open |
| 5.3 | No test for mission cancellation race | P5 | Open |
| 5.4 | No test for `get_frame()` failure (post fix 2.3) | P5 | Open |
| 6.1 | Monitor index defaults to 2 | P6 | Open |
| 6.2 | Region defaults assume 1920×1200, undocumented | P6 | Open |
