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


# TODO fix test

------------------- Generated html report: file:///C:/dev-tools/github/wingman/tests/test-output/report.html --------------------
================================================ 35 passed in 103.86s (0:01:43) =================================================
mkdir -p tests/test-output
rm -f wingman.log tests/test-output/replay_assertions.path1.json tests/test-output/replay_action_intents.path1.json tests/test-output/replay_required_screenshots.path1.json tests/test-output/runtime_replay_validation.path1.json
uv run --active python -m wingman.main \
        --config wingman/config.yaml \
        --replay-config tests/replay_paths/adr044_runtime_path1.yaml \
        --replay-path PATH1_RUNTIME \
        --replay-screenshot-dir test_screenshots/integration_test \
        --replay-exit-after 3.0 \
        --replay-report tests/test-output/replay_required_screenshots.path1.json \
        --replay-intents-output tests/test-output/replay_action_intents.path1.json \
        --replay-assertions-output tests/test-output/replay_assertions.path1.json \
        --log-file wingman.log
2026-05-31 13:27:36,999 [INFO] Configuration loaded from wingman/config.yaml
2026-05-31 13:27:37,022 [INFO] Replay mode enabled: path=PATH1_RUNTIME, screenshots=test_screenshots\integration_test
2026-05-31 13:27:37,023 [INFO] OCR mode: CPU
2026-05-31 13:27:37,032 [INFO] Unattended mode enabled from config
2026-05-31 13:27:37,225 [INFO] Initialized ThreadPoolExecutor with 13 workers for parallel OCR
2026-05-31 13:27:45,063 [INFO] OCR thread 26544: initialized EasyOCR reader (CPU)
C:\dev-tools\github\wingman\.venv-1\Lib\site-packages\torch\utils\data\dataloader.py:775: UserWarning: 'pin_memory' argument is set as true but no accelerator is found, then device pinned memory won't be used.
  super().__init__(loader)
2026-05-31 13:27:51,348 [INFO] OCR thread 25360: initialized EasyOCR reader (CPU)
2026-05-31 13:27:53,152 [INFO] Analyzer: 'PLAY' detected in PLAY crop (text='PLAY')
2026-05-31 13:27:53,155 [INFO] 🎮 Game state: UNKNOWN → GAME_UNKNOWN
2026-05-31 13:27:56,014 [INFO] OCR thread 6984: initialized EasyOCR reader (CPU)
2026-05-31 13:28:00,066 [INFO] OCR thread 28080: initialized EasyOCR reader (CPU)
2026-05-31 13:28:05,855 [INFO] OCR thread 17208: initialized EasyOCR reader (CPU)
2026-05-31 13:28:10,214 [INFO] OCR thread 26404: initialized EasyOCR reader (CPU)
2026-05-31 13:28:11,563 [ERROR] Unhandled exception in main loop
Traceback (most recent call last):
  File "C:\dev-tools\github\wingman\wingman\main.py", line 582, in main
    raise RuntimeError(
RuntimeError: Replay assertion failure(s): Checkpoint timeout waiting for state=game_starting, trigger=cancel_detected within 10.0s
2026-05-31 13:28:11,568 [INFO] Replay action intents saved to tests\test-output\replay_action_intents.path1.json
2026-05-31 13:28:11,571 [INFO] Replay assertions saved to tests\test-output\replay_assertions.path1.json
2026-05-31 13:28:12,004 [INFO] Controller: all keyboard hooks deregistered
2026-05-31 13:28:12,005 [INFO] ThreadPoolExecutor shut down successfully
2026-05-31 13:28:12,009 [INFO] PerformanceTracker: session data written to docs\performance\current\run_20260531_132737.json
2026-05-31 13:28:12,041 [INFO] [SESSION vs CURRENT PERIOD | this session: 0 rounds 0 cycles | period: 13 sessions 444 cycles]
  crop                      this session  period mean  delta
2026-05-31 13:28:12,042 [INFO] [PERIOD COMPARISON] accumulating baseline (N=13 sessions, 444 cycles — need 5 sessions and 1000 cycles)
2026-05-31 13:28:14,458 [INFO] OCR thread 10548: initialized EasyOCR reader (CPU)
2026-05-31 13:28:17,741 [INFO] OCR thread 25740: initialized EasyOCR reader (CPU)
2026-05-31 13:28:20,496 [INFO] OCR thread 21140: initialized EasyOCR reader (CPU)
2026-05-31 13:28:23,048 [INFO] OCR thread 27448: initialized EasyOCR reader (CPU)
2026-05-31 13:28:25,843 [INFO] OCR thread 10544: initialized EasyOCR reader (CPU)
2026-05-31 13:28:28,469 [INFO] OCR thread 23992: initialized EasyOCR reader (CPU)
2026-05-31 13:28:31,078 [INFO] OCR thread 11104: initialized EasyOCR reader (CPU)
uv run --active python tests/runtime_replay_validate.py \
        --log-file wingman.log \
        --assertions-file tests/test-output/replay_assertions.path1.json \
        --intents-file tests/test-output/replay_action_intents.path1.json \
        --summary-out tests/test-output/runtime_replay_validation.path1.json
FAIL: runtime replay validation failed
 - assertions.has_failures must be false
 - assertions.is_complete must be true
 - assertions.checkpoints contains failed statuses at indexes: 0
 - forbidden log pattern present (1): Traceback
 - forbidden log pattern present (1): [ERROR]
 - forbidden log pattern present (1): Replay assertion failure
 - required log pattern missing: MISSILES EMPTY — cancelling mission and ejecting
 - required log pattern missing: Controller: eject_and_dive — NOSE_DOWN + AFTERBURNER engaged
 - required log pattern missing: Analyzer: 'Good Luck' detected in good_luck crop
 - required log pattern missing: Controller: 'Good Luck' detected
 - missing terminal eject outcome: neither complete nor cancelled marker found
Summary: tests\test-output\runtime_replay_validation.path1.json
make: *** [Makefile:255: rr-validate-path1] Error 1
(metalstorm-wingman) 