# Job Aid: Enable GPU Acceleration for OCR (EasyOCR + PyTorch)

This guide explains how to enable and validate GPU OCR for Wingman on Windows.

## Goal
Use the GPU for OCR to reduce analysis latency and smooth out heavy CPU load periods.

## 1. Prerequisites

- NVIDIA GPU
- NVIDIA driver installed and working
- Python virtual environment activated
- `uv` available in the environment

Check GPU visibility in Windows:

```powershell
nvidia-smi
```

If `nvidia-smi` is not available or shows no GPU, fix driver/toolkit first.

## 2. Install CUDA-Enabled PyTorch

From the project root:

```bash
uv pip install --upgrade pip
uv pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu121
uv pip install easyocr
```

Notes:
- `cu121` is the CUDA 12.1 wheel index.
- If your environment requires a different CUDA build, use the matching PyTorch index URL.

## 3. Verify GPU in Python

Run:

```bash
python -c "import torch; print('cuda_available=', torch.cuda.is_available()); print('device=', torch.cuda.get_device_name(0) if torch.cuda.is_available() else 'CPU only')"
```

Expected:
- `cuda_available= True`
- A GPU device name

If this shows `False`, EasyOCR GPU mode will not activate.

## 4. Update OCR Worker Initialization

Current multiprocessing worker initialization in `wingman/analyzer.py` forces CPU mode:

- `_init_ocr_reader()` currently uses `easyocr.Reader(['en'], gpu=False, verbose=False)`.

Change behavior to support GPU mode in workers.

Recommended pattern:

1. Detect CUDA availability once.
2. Pass `use_gpu` into worker initializer (`initargs`).
3. Use `gpu=use_gpu` when creating `easyocr.Reader`.
4. Keep fallback to CPU if GPU init fails.

## 5. Worker Count Strategy

Use different pool sizes by mode:

- CPU mode: `processes=2` (or tune as needed)
- GPU mode: `processes=1`

Reason:
- Multiple GPU worker processes can duplicate model memory and increase contention.
- One process usually gives better stability and throughput on a single GPU.

## 6. Add Config Switches

Add config flags (example):

```yaml
ocr:
  use_gpu: true
  workers_cpu: 2
  workers_gpu: 1
```

Behavior:
- If `use_gpu` is true and CUDA is available, run GPU mode.
- Otherwise, use CPU mode automatically.

## 7. Runtime Validation

After starting Wingman, confirm:

- No recurring warning:
  - `'pin_memory' argument is set as true but no accelerator is found`
- Logs show GPU path enabled for OCR reader initialization.
- OCR total times improve, especially during mission loops.

Optional quick check while running:

```powershell
nvidia-smi -l 1
```

You should see Python process GPU utilization/memory increase during OCR.

## 8. Performance Measurement Checklist

Capture at least 2 runs:

1. Idle/background OCR only
2. Full mission load (weapons, padlock, afterburner active)

Record:
- Average total OCR time
- p50 / p90 / p95 total OCR times
- Max spike
- Detection reliability (incoming/respawn)

## 9. Rollback Plan

If GPU mode is unstable:

1. Set `use_gpu: false` in config.
2. Keep multiprocessing with CPU workers.
3. Re-test to confirm baseline behavior is restored.

## 10. Common Issues

- CUDA not detected:
  - Wrong PyTorch wheel (CPU-only build installed)
  - Driver mismatch
  - Wrong virtual environment active

- Slower than expected on GPU:
  - Too many worker processes in GPU mode
  - GPU thermal throttling
  - Heavy concurrent graphics load

- Intermittent initialization failures:
  - Keep CPU fallback in code
  - Log whether reader initialized with GPU or CPU

---
Use this job aid when enabling GPU OCR in a new machine setup or when revisiting performance tuning later.
