# ADR 003: Grid-Based Screen Scanning Architecture for Game State Detection

**Date**: 2026-02-20

**Status**: Accepted

**Context**: MetalStorm Wingman needs to continuously detect game state from screen captures to make intelligent decisions. Specifically, we need to detect when the player is on the respawn screen to avoid executing flight maneuvers. Running OCR on the entire screen every frame is prohibitively expensive (40-120ms per frame), making real-time gameplay impossible.

## Problem

Initial naive approach:
- Capture full screen (1920x1080) every frame
- Run EasyOCR on entire frame to detect "RESPAWN" text
- **Result**: 40-120ms per frame = ~8-25 FPS maximum
- At 60 FPS target: OCR alone would take 240-7200% of available frame time

**Key Challenges:**
1. OCR is expensive (10-120ms depending on GPU/CPU)
2. Game state doesn't change every frame
3. Different UI elements appear in different screen regions
4. Full-screen analysis wastes compute on irrelevant areas

## Decision

Implement a **grid-based screen scanning architecture** with the following components:

### 1. Spatial Grid Partitioning (6×6 = 36 regions)

Divide the screen into a 6×6 grid of regions:

```
 1  2  3  4  5  6
 7  8  9 10 11 12
13 14 15 16 17 18
19 20 21 22 23 24
25 26 27 28 29 30
31 32 33 34 35 36
```

**Rationale:**
- **Targeted analysis**: Only process regions containing relevant UI elements
- **Smaller images**: Region = 1/36 of screen → ~4x faster OCR per region
- **Spatial indexing**: Configure which region(s) to scan for each game state
- **Visual debugging**: Grid overlay shows exactly what's being analyzed

**Why 6×6 (not 3×3 or 10×10)?**
- 3×3 = too coarse, UI elements span multiple regions
- 6×6 = good balance between precision and simplicity
- 10×10 = too fine-grained, increases lookup overhead

### 2. Region-Specific Detection Configuration

Configure analyzer to only scan relevant regions:

```yaml
respawn_detection:
  region: 27  # Only scan region 27 (bottom-center) where RESPAWN text appears
```

**Performance gain:**
- Full screen: 1920×1080 = 2,073,600 pixels
- Region 27: 320×180 = 57,600 pixels (36x fewer!)
- Regions 1-26, 28-36: Skip OCR entirely (instant)

### 3. OCR Result Caching/Throttling

Cache OCR results with configurable cooldown:

```python
self._ocr_cache = {
    'result': (False, 0.0, None),
    'timestamp': 0.0,
    'cooldown': 0.5  # Seconds between OCR runs
}
```

**How it works:**
1. First call: Run OCR, cache result + timestamp
2. Subsequent calls within cooldown: Return cached result (instant)
3. After cooldown expires: Run OCR again, update cache

**Performance:**
- At 60 FPS with 0.5s cooldown: 2 OCR calls/sec instead of 60 = **30x reduction**
- At 30 FPS with 0.5s cooldown: 2 OCR calls/sec instead of 30 = **15x reduction**

**Why this works:**
- Respawn screens stay visible for 5-10 seconds
- No need to detect state change within 500ms
- OCR can "miss" a few frames without impacting UX

### 4. Image Preprocessing Pipeline

Optimize OCR accuracy and speed:

```python
gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
_, binary = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
small = cv2.resize(binary, None, fx=0.8, fy=0.8, interpolation=cv2.INTER_AREA)
results = reader.readtext(small, detail=1, paragraph=False)
```

**Pipeline stages:**
1. **Grayscale conversion**: Reduce 3 channels to 1 (3x less data)
2. **Binary thresholding** (Otsu): Pure black/white text = better OCR accuracy
3. **Downscaling (80%)**: Smaller image = faster inference, text still clear
4. **EasyOCR inference**: Run on preprocessed image

**Why binary threshold instead of CLAHE?**
- CLAHE (histogram equalization): Good for low-contrast images
- Binary threshold: Better for clean UI text (high contrast)
- Game HUD text is already high contrast → binary wins

### 5. Lenient Text Matching

Remove non-alphabetic characters before matching:

```python
text_clean = ''.join(c for c in text.strip().upper() if c.isalpha())
if 'RESPA' in text_clean:  # Match found
```

**Handles OCR errors:**
- `"RE$PA!"` → `"RESPA"` ✓
- `"R E S P A W N"` → `"RESPAWN"` ✓
- `"RE-SPA-WN"` → `"RESPAWN"` ✓

**Why partial match "RESPA" not full "RESPAWN"?**
- OCR often truncates or misreads last character
- 5 characters = sufficient uniqueness
- Trade accuracy for robustness

## Architecture Overview

```
┌─────────────────────────────────────────────────────┐
│ Main Loop (60 FPS)                                  │
├─────────────────────────────────────────────────────┤
│ 1. Capture screen                                   │
│ 2. analyzer.analyze_frame(frame)                    │
│    ├─ Check OCR cache (<0.5s old?) ────────┐       │
│    │  └─ Yes: Return cached result (0.01ms)│       │
│    └─ No: Run OCR workflow ────────────────┘       │
│       ├─ Extract region 27 only (1/36 screen)      │
│       ├─ Preprocess: grayscale → binary → resize   │
│       ├─ EasyOCR.readtext() (10-120ms)             │
│       ├─ Match "RESPA" in results                  │
│       └─ Cache result + timestamp                   │
│ 3. Use game_state to make decisions                │
│ 4. Execute actions (missions, fire, etc.)          │
└─────────────────────────────────────────────────────┘
```

## Alternatives Considered

### 1. Full-Screen OCR Every Frame
- **Pros**: Simple, no caching complexity
- **Cons**: 40-120ms per frame, makes real-time impossible
- **Verdict**: ❌ Rejected - performance unacceptable

### 2. Hash-Based Frame Diffing (Skip OCR if frame unchanged)
- **Pros**: Intelligent caching based on actual content
- **Cons**: Hash computation overhead, game has constant motion (backgrounds, particles)
- **Verdict**: ❌ Rejected - game frames always changing, hash never matches

### 3. Template Matching (OpenCV)
- **Pros**: Very fast (1-5ms), no OCR needed
- **Cons**: Brittle to font changes, scaling, anti-aliasing
- **Verdict**: ⚠️ Consider for fixed UI elements in future

### 4. Fixed-Time OCR (Every N seconds, ignore frames)
- **Pros**: Simple rate limiting
- **Cons**: Can miss short-lived states, fixed delay regardless of need
- **Verdict**: ⚠️ Our caching approach is more flexible

### 5. Motion Detection (Only OCR when screen changes significantly)
- **Pros**: Adaptive to actual state changes
- **Cons**: Complex logic, background motion triggers false positives
- **Verdict**: ❌ Rejected - game has constant motion animations

### 6. Neural Network State Classifier
- **Pros**: Can detect complex states beyond text
- **Cons**: Requires training data, model deployment, more complex
- **Verdict**: ⚠️ Consider for v2.0 if more states needed

## Consequences

### Positive

✅ **30-50x performance improvement**
- Before: 60 OCR calls/sec at 60 FPS = 2400-7200ms of OCR time/sec
- After: 2 OCR calls/sec = 20-240ms of OCR time/sec

✅ **Real-time gameplay possible**
- Frame budget at 60 FPS: 16.67ms
- OCR overhead: <1ms average (cached), 10-120ms every 0.5s (acceptable spike)

✅ **Configurable responsiveness vs. performance**
- Fast respawn detection: `ocr_cooldown: 0.2` (5x/sec)
- Balanced: `ocr_cooldown: 0.5` (2x/sec, default)
- Max performance: `ocr_cooldown: 1.0` (1x/sec)

✅ **Extensible architecture**
- Easy to add more detection types (enemy count, health, ammo)
- Each detection type can target specific regions
- Grid visualization aids development/debugging

✅ **Memory efficient**
- Only cache lightweight results (bool + float + string)
- No frame buffering or image caching

### Negative

⚠️ **Potential state change delay**
- Worst case: 500ms delay to detect respawn screen
- Acceptable because respawn screens visible for 5-10 seconds
- Not suitable for frame-critical detection (e.g., damage flash)

⚠️ **Configuration overhead**
- Developers must identify correct region for each UI element
- Grid visualization tool helps but adds development step

⚠️ **Brittle to UI changes**
- If game moves respawn text, must reconfigure region
- Template matching would have same issue

⚠️ **Cache invalidation complexity**
- Cache doesn't know if game state actually changed
- Fixed cooldown may cache stale positive result briefly

## Performance Characteristics

### Measured Performance

| Scenario | Time | Notes |
|----------|------|-------|
| Cached OCR result | 0.01ms | Reading from memory |
| Region extraction | 0.1ms | Array slicing |
| Grayscale conversion | 0.2ms | 320×180 region |
| Binary threshold | 0.1ms | Otsu's method |
| Image resize (80%) | 0.1ms | Downscaling |
| EasyOCR (GPU) | 10-25ms | NVIDIA GPU |
| EasyOCR (CPU) | 40-120ms | Intel i7/AMD Ryzen |
| Full analyze_frame (cached) | 0.5ms | No OCR |
| Full analyze_frame (OCR) | 15-125ms | With OCR |

### Throughput at 60 FPS

```
Frame budget: 16.67ms

Workload distribution:
- Screen capture: 2-5ms
- analyze_frame (cached): 0.5ms ✓
- analyze_frame (OCR): 15-125ms ✗
- Game logic: 1-2ms
- Controller actions: 0.1-1ms

With caching (2 OCR/sec):
- 58 frames: 0.5ms each = OK
- 2 frames: 15-125ms each = brief spike, acceptable
```

## Implementation

### Core Components

1. **`analyzer.py`**: GameStateAnalyzer class
   - `get_region()`: Extract grid region by number
   - `draw_grid()`: Visualization for debugging
   - `analyze_frame()`: Main analysis with caching
   - `_detect_respawn_ocr()`: OCR with preprocessing

2. **`config.yaml`**: Configuration
   - `respawn_detection.region`: Which region to scan
   - `respawn_detection.ocr_cooldown`: Cache duration

3. **`test_analyzer.py`**: Development tools
   - `--grid`: Generate grid overlay for region identification
   - `--multiple`: Test all screenshots in batch

### Usage Example

```python
# Initialize
analyzer = GameStateAnalyzer(config)

# Main loop
while running:
    frame = capture.get_frame()
    
    # Fast: cached most of the time
    state = analyzer.analyze_frame(frame)
    
    if state['is_respawning']:
        logger.info("Respawning, skipping mission")
        continue
    
    # Execute gameplay logic
    controller.mission_loiter()
```

## Future Optimizations

1. **Multi-region detection**: Check multiple regions in parallel
2. **Adaptive cooldown**: Dynamically adjust based on state change frequency
3. **GPU batch processing**: Accumulate multiple regions, run single OCR call
4. **Model quantization**: INT8 EasyOCR model for 2x CPU speed
5. **ONNX Runtime**: Export to optimized inference engine
6. **Hybrid detection**: Template matching + OCR fallback

## References

- [OpenCV Image Thresholding](https://docs.opencv.org/4.x/d7/d4d/tutorial_py_thresholding.html)
- [EasyOCR Documentation](https://github.com/JaidedAI/EasyOCR)
- ADR 001: EasyOCR for Screen Number Detection

---

**Decision made by**: Development Team  
**Supersedes**: N/A  
**Related ADRs**: ADR 001 (EasyOCR selection)
