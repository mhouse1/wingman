# ADR 001: Using EasyOCR for Screen Number Detection

**Date**: 2026-01-10

**Status**: Accepted

**Context**: MetalStorm Wingman prototype requires optical character recognition (OCR) to extract numeric data from the game screen (e.g., speed, ammo count, scores, health indicators). This data enables the AI to make informed decisions about gameplay actions.

## Decision

We will use **EasyOCR** as the OCR engine for detecting and extracting numbers from screen captures.

### Alternatives Considered

1. **Tesseract OCR** (via pytesseract)
   - Requires separate binary installation (tesseract.exe)
   - Path configuration issues on Windows
   - Good accuracy but setup friction for end users
   - **Rejected**: Installation complexity and cross-platform deployment challenges

2. **PaddleOCR**
   - High accuracy, supports 80+ languages
   - Heavier dependencies (PaddlePaddle framework)
   - **Rejected**: Overkill for English-only numeric detection

3. **Cloud OCR APIs** (Google Vision, Azure CV, AWS Textract)
   - Excellent accuracy
   - Requires network connectivity and API costs
   - Adds latency (100-500ms per request)
   - **Rejected**: Real-time gaming requires <50ms latency; offline operation required

4. **Custom digit classifier** (CNN/CRNN)
   - Fastest inference (~5-20ms)
   - Requires training data collection and model training
   - **Deferred**: Consider for v2.0 if performance becomes critical

### Why EasyOCR?

- **Pure Python package**: `pip install easyocr` (no external binaries)
- **No external dependencies**: Self-contained, works immediately after install
- **Pre-trained models**: Downloads models automatically on first run
- **Reasonable accuracy**: 80-90% on game HUD text
- **Minimal code**: Simple API (`reader.readtext(frame)`)

## Consequences

### Positive

- Easy deployment: users run `uv add easyocr` and it works
- Cross-platform: works on Windows, macOS, Linux without platform-specific setup
- Robust detection: handles various fonts, sizes, and lighting conditions
- Returns bounding boxes: enables spatial analysis (e.g., "speed is in top-right")

### Negative

- **Slow CPU inference**: 500-1500ms per frame on CPU
  - Currently bottlenecks the main loop (target: <100ms per frame)
  - Causes input lag and reduces reaction time
- **Large model size**: ~100MB download on first run
- **Memory footprint**: 200-500MB RAM for model
- **CUDA warning spam**: Logs warning about missing GPU support on every read

## Performance Optimization Path

### Current State (CPU-only)
- **Hardware**: Intel i7 / AMD Ryzen typical
- **Frame time**: 500-1500ms per OCR call
- **Main loop frequency**: ~1 Hz (once per second)
- **Acceptable for**: Prototype testing, strategy games

### GPU Acceleration with CUDA

EasyOCR supports NVIDIA CUDA for GPU-accelerated inference:

**Requirements:**
- NVIDIA GPU (GTX 1060 or newer recommended)
- CUDA Toolkit 11.8+ or 12.x
- cuDNN 8.x
- PyTorch with CUDA support

**Installation (Windows):**
```bash
# Install CUDA-enabled PyTorch (replaces CPU version)
uv pip install torch torchvision --index-url https://download.pytorch.org/whl/cu121

# Verify GPU detection
uv run python -c "import torch; print('CUDA available:', torch.cuda.is_available())"
```

**Expected performance with GPU:**
- **Frame time**: 50-150ms per OCR call (10x faster)
- **Main loop frequency**: ~10 Hz
- **Acceptable for**: Fast-paced FPS games, real-time reaction

**Configuration:**
```python
# Enable GPU in EasyOCR initialization
reader = easyocr.Reader(['en'], gpu=True)  # Change from gpu=False
```

### Alternative Optimizations (without GPU)

1. **Reduce scan frequency**
   - Scan every 2-3 frames instead of every frame
   - Most HUD values don't update faster than 2-3 Hz

2. **Region of Interest (ROI)**
   - Only scan specific screen regions (e.g., top-right corner for speed)
   - Reduces image size by 75-90% → 2-4x speedup

3. **Image preprocessing**
   - Resize frame to 50% before OCR (width/height both 0.5x)
   - Trade accuracy for 2-3x speed gain

4. **Thread/process pool**
   - Run OCR in separate thread to avoid blocking game input
   - Stale data acceptable for non-critical indicators

5. **Fallback to template matching**
   - For fixed-position digital displays, use OpenCV template matching
   - 10-50ms for simple numeric displays

## Future Considerations

- **Benchmark custom digit classifier**: Train lightweight model on game-specific fonts
- **Hybrid approach**: Template matching for fixed displays, OCR for dynamic text
- **Model quantization**: Convert EasyOCR model to INT8 for 2x CPU speedup
- **ONNX Runtime**: Export model to ONNX and use optimized inference engine

## References

- [EasyOCR GitHub](https://github.com/JaidedAI/EasyOCR)
- [PyTorch CUDA Installation](https://pytorch.org/get-started/locally/)
- [NVIDIA CUDA Toolkit](https://developer.nvidia.com/cuda-downloads)
- [cuDNN Archive](https://developer.nvidia.com/rdp/cudnn-archive)

---

**Decision made by**: Development Team  
**Supersedes**: N/A  
**Related ADRs**: None yet
