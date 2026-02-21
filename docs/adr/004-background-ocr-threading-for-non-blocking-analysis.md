# ADR 004: Background OCR Threading for Non-Blocking Analysis

**Status:** Accepted  
**Date:** 2026-02-21  

## Context

The respawn detection system uses EasyOCR to identify the "RESPAWN" text on-screen. However, OCR analysis is CPU-intensive:

- First OCR run: ~2600-2900ms (blocks main game loop)
- Subsequent cached runs: 0-14ms (instant)

**Problem:** Even with aggressive caching (5s cooldown), the main game loop was blocked for 2.6+ seconds whenever OCR ran, delaying hotkey responsiveness and mission control.

### Example Performance Timeline (Before Threading)
```
05:23:06,028 ▶ Starting analysis of frame_05_23_06_024
05:23:08,785 ✓ Analysis complete (2760.8ms)  ← Main loop blocked!
05:23:08,884 ▶ Starting analysis of frame_05_23_08_884
05:23:08,884 ✓ Analysis complete (0.0ms)      ← Cache hit
...
05:23:15,173 ▶ Starting analysis of frame_05_23_15_173
05:23:17,835 ✓ Analysis complete (2661.9ms)  ← Main loop blocked again!
```

## Decision

Implement **background OCR threading** with thread-safe caching:

1. **Main thread:** Returns cached result immediately (0-14ms), never blocks
2. **Background thread:** Runs OCR asynchronously, updates cache when done
3. **Cache lock:** `threading.Lock()` ensures thread-safe cache access

### Code Architecture

```python
class GameStateAnalyzer:
    def __init__(self):
        self._ocr_cache = {...}
        self._ocr_cache_lock = threading.Lock()  # Thread safety
        self._background_ocr_running = False
        self._background_ocr_frame = None
        self._background_ocr_thread = None

    def _detect_respawn_ocr(self, frame):
        # Check cache (locked for thread safety)
        with self._ocr_cache_lock:
            if cache_still_valid():
                return cached_result  # Non-blocking! (0-14ms)
        
        # Cache expired - start background thread (non-blocking!)
        if not self._background_ocr_running:
            self._background_ocr_thread = threading.Thread(
                target=self._run_ocr_in_background,
                daemon=True
            )
            self._background_ocr_thread.start()
        
        # Return stale cache while background thread runs
        return cached_result

    def _run_ocr_in_background(self):
        # Runs in separate thread (doesn't block main loop)
        try:
            result = reader.readtext(frame)  # 2.6-2.9s
            with self._ocr_cache_lock:
                self._ocr_cache['result'] = result
                self._ocr_cache['timestamp'] = time.time()
        finally:
            self._background_ocr_running = False
```

## Rationale

### Why Threading?
- **Respawn screens are persistent** (visible for 5+ seconds)
- **Stale data is acceptable** (cached result from previous frame)
- **Latency is better managed** when OCR doesn't block hotkey detection
- **Standard Python pattern** for long-running I/O operations

### Why Thread-Safe Caching?
- **Two threads access `_ocr_cache`:** main thread (read) + background thread (write)
- **Lock prevents race conditions** during cache updates
- **Lock is held briefly** (microseconds for dict updates, not 2.6s)

### Why 5s Cooldown?
- Respawn screens visible for 5+ seconds minimum
- Main game loop runs every 50-100ms (20-10 fps)
- 5s cooldown = sufficient respawn detection rate
- Reduces CPU load (only one OCR run per 5 seconds)

## Consequences

### Benefits
✅ **Main loop never blocks** on OCR (always 10-50ms per frame)  
✅ **Hotkeys remain responsive** during OCR analysis  
✅ **Missions execute immediately** without OCR wait  
✅ **Cache hits are instant** (0-14ms per frame)  
✅ **Background work doesn't impact gameplay** feel  

### Trade-offs
⚠️ **Stale detection (up to 5s):** Cache may be 5s old before refresh  
⚠️ **Race condition risk:** Mitigated by thread-safe lock (minimal impact)  
⚠️ **Added complexity:** Threading adds 50 lines of code, but pattern is standard  

### When Stale Cache is OK
- **Respawn screens:** Visible for 10-20+ seconds, 5s stale data harmless
- **Game state changes:** Rare and persistent (not millisecond-critical)
- **Hotkey responsiveness:** Not affected (independent of OCR)

## Performance Impact

### Before Threading
```
Frame 1: 2760.8ms (OCR)
Frame 2: 0.0ms     (cache)
Frame 3: 0.0ms     (cache)
...
Frame N: 2661.9ms  (OCR after 5s cooldown)
```
**Problem:** 2.6s blocking delays

### After Threading
```
Frame 1: 2760.8ms (OCR in background, cache still valid)
Frame 2: 0.0ms    (cache hit, fast return)
Frame 3: 0.0ms    (cache hit, fast return)
...
Frame N: 0.0ms    (cache hit, OCR running in background)
```
**Result:** Main loop always fast, OCR runs unnoticed

## Monitoring

Cache performance can be observed in logs:

```
[DEBUG] Using cached OCR result (0.23s old)     ← Fast path
[DEBUG] Background OCR scheduled                 ← Cache expired, OCR starting
```

Enable `--log-level DEBUG` to see threading in action.

## Implementation Details

### Thread Safety
- Lock acquisition time: **microseconds** (minimal contention)
- Lock held during: Cache read/write only (not during OCR)
- No deadlock risk: Single lock, no circular dependencies

### Daemon Thread
- Thread marked as `daemon=True`
- Automatically cleaned up on program exit
- Won't prevent graceful shutdown

### Default Behavior
- Background OCR disabled if EasyOCR unavailable (graceful degradation)
- If OCR fails, cache remains valid (no cache invalidation on error)
- Multiple background threads never spawn (guarded by `_background_ocr_running` flag)

## Related Decisions
- [ADR 003: Grid-Based Screen Scanning](./003-grid-based-screen-scanning-architecture.md) - Regional OCR optimization (abandoned in favor of full-frame threading)
- [ADR 002: Keyboard Library for Game Input](./002-keyboard-library-for-game-input.md) - Hotkey responsiveness requirements
- [ADR 001: EasyOCR for Screen Number Detection](./001-easyocr-for-screen-number-detection.md) - OCR technology choice

## Future Improvements
1. **GPU acceleration** if CUDA/MPS available (10-50x speedup on OCR itself)
2. **Configurable cooldown** per game state (shorter for rapid respawns, longer for stability)
3. **Async/await pattern** if moving to async architecture (Python 3.10+)
4. **OCR result validation** (check confidence threshold, not just presence)
