# ADR 020 — CPU-Only OCR Optimizations

| Status   | Date       | Wingman Version |
|----------|------------|-----------------|
| Accepted | 2026-03-23 | 1.5.3           |

## Context

Wingman has always run EasyOCR on CPU — no GPU has been used. Three sources of unnecessary overhead existed in the CPU path:

### 1. Unnecessary GPU probe on every worker thread init

`_get_thread_ocr_reader()` always attempted `gpu=True` first, caught the exception, then re-initialized with `gpu=False`. With the CPU-only PyTorch wheel (`torch+cpu`) installed, this failure is near-instant — no CUDA runtime is linked, so PyTorch raises in under a millisecond. The practical time cost is negligible (~3–10ms across 3 workers at session start).

The real problem is noise and clarity: three `GPU init failed, falling back to CPU` warnings appear in every session log despite GPU never being available, and the try/except/retry code path implies a GPU fallback that will never succeed.

### 2. DataLoader subprocess workers in `readtext`

EasyOCR's `readtext` passes a `workers` argument to PyTorch's DataLoader. For the small single-region images Wingman scans (one grid cell at a time), the IPC overhead of subprocess workers exceeds the actual inference time. Single-image inference requires no batching; the DataLoader worker count adds overhead with no benefit.

### 3. Three pool workers competing on CPU

The `ThreadPoolExecutor` was created with `max_workers=3`, intended as one worker each for respawn, incoming, and click-to detection. However, click-to runs on its own dedicated background thread that submits jobs to the pool every 5 seconds — it does not need a dedicated pool worker sitting idle. On CPU, three concurrent OCR jobs compete for the same cores, worsening the 1.85–4.60s variance documented in ADR 017:

```
# Live session log 2026-03-20, 11 cycles, CPU-only (from ADR 017)
Average OCR cycle: 3.25s
Range:             1.85s – 4.60s
```

The high variance reflects CPU thread contention — whichever of the two active OCR threads loses core access during a cycle balloons toward the 4.60s worst case.

## Decision

Three targeted changes, all reversible via config or a one-line code change:

### 1. `use_gpu` config flag — skip GPU probe entirely

Added `use_gpu: false` to `config.yaml` under `respawn_detection`. A module-level `_use_gpu` flag is set at `GameStateAnalyzer.__init__` time and read by `_get_thread_ocr_reader`. When false, the reader is initialized directly with `gpu=False` — one code path, no warnings, unambiguous log output (`OCR mode: CPU`).

The flag also makes the GPU path explicitly opt-in should a CUDA environment become available in the future (see ADR 017 Option A).

### 2. `workers=0` on all `readtext` calls

All five `reader.readtext(...)` call sites now pass `workers=0` explicitly, forcing PyTorch's DataLoader to run in the calling thread and eliminating subprocess spawn overhead for single-image inference.

```python
# before
reader.readtext(img, detail=0, paragraph=True)

# after
reader.readtext(img, detail=0, paragraph=True, workers=0)
```

### 3. `ThreadPoolExecutor` reduced from 3 workers to 2

Respawn and incoming OCR genuinely run in parallel on the hot path — 2 workers preserves that. Click-to detection runs on its own 5s background thread and submits to the pool as a low-priority job; it does not need a reserved worker. Reducing to 2 removes one idle CPU-bound thread from the contention pool.

## Consequences

- **GPU path remains available**: setting `use_gpu: true` in config re-enables it with no code changes required
- **`workers=0` is safe for single-image inference**: no batching occurs in Wingman's OCR pipeline; the DataLoader worker count has no effect on accuracy
- **Click-to jobs may queue briefly behind respawn/incoming**: with 2 workers, a click-to job submitted while both workers are active will wait. Click-to runs every 5 seconds and is lower priority than respawn/incoming — this trade-off is acceptable
- **Thread init is simpler**: one code path instead of try/except/fallback; log messages are unambiguous (`OCR mode: CPU`)

## References

- [ADR 017](017-ocr-performance-gpu-vs-template-matching.md) — CPU performance baseline (3.25s avg, 1.85–4.60s range) and GPU vs template matching options
- [ADR 016](016-ocr-multiprocessing-to-threading-migration.md) — threading model that introduced the executor
- [wingman/analyzer.py](../../wingman/analyzer.py) — `_use_gpu`, `_get_thread_ocr_reader`, `ocr_executor` property, all `readtext` call sites
- [wingman/config.yaml](../../wingman/config.yaml) — `respawn_detection.use_gpu`
