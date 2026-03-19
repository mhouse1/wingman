# ADR 016 — Migrate OCR Workers from Multiprocessing to Threading

## Status

Proposed

## Context

The current OCR pipeline spawns a `multiprocessing.Pool` with 3 worker processes (one each for respawn, incoming, and click-to detection). Each worker initialises its own EasyOCR reader on startup (~10s cold start per worker), and every OCR call requires the frame to be serialised to bytes, pickled across the IPC boundary, and deserialised inside the worker.

This architecture was chosen to bypass Python's GIL for CPU-bound OCR work. That reasoning is correct for pure Python code, but EasyOCR runs on PyTorch, which **releases the GIL during inference** (C++/CUDA operations). Threading therefore gives true parallelism without the IPC overhead.

Observed symptoms in production logs:

- Detection latency of 2–6 seconds per OCR cycle
- `UserWarning: 'pin_memory' argument is set as true but no accelerator is found` — EasyOCR initialises with `gpu=True` but CUDA is not usable across spawned processes on Windows, so all inference falls back to CPU
- Respawn detection delayed by up to the full incoming OCR time (up to 4s extra) because both results were previously written only after both workers finished (fixed separately in the cache update ordering, but root cause remains)

## Decision

Migrate OCR workers from `multiprocessing.Pool` to `threading.Thread`. A single EasyOCR reader instance will be shared across threads in the main process.

## Consequences

### Gains

| | Multiprocessing (current) | Threading (proposed) |
|---|---|---|
| Model cold start | ~10s × 3 workers | Once, at startup |
| Frame handoff | Serialize → pickle → IPC → deserialize | Direct numpy reference |
| GPU usage | Blocked — separate CUDA context per process not reliable on Windows | Single shared context, GPU inference available |
| Memory | Full model loaded 3× in RAM | Loaded once |
| Adding a new detection task | New process + new model copy | New thread, shared model |
| OCR cycle time (CPU) | 2–6s | ~1–2s |
| OCR cycle time (GPU, once enabled) | Not accessible | <200ms |

### Tradeoffs

- A crash inside a thread can affect the main process; a crash inside a worker process is isolated. In practice the OCR workers do not raise unhandled exceptions — they catch and log all errors — so this is an acceptable tradeoff.
- The shared EasyOCR reader must be confirmed thread-safe. EasyOCR's `readtext()` is stateless (no mutable model state between calls) and PyTorch releases the GIL during forward passes, so concurrent calls are safe.

### Future enemy detection

The primary motivation for keeping EasyOCR (rather than switching to template matching for current static text) is the roadmap requirement to detect enemy type from in-game HUD labels — e.g. identifying stealth aircraft by their displayed designation. Multiprocessing would require a new worker process and a second full model copy in RAM for each new detection region. Threading allows arbitrarily many concurrent detection tasks sharing a single loaded model, which is the correct foundation for that expansion.

## Alternatives Considered

### Template matching for current static text (RESPA, MING)

Would reduce current detection to <5ms per cycle and eliminate the OCR pipeline entirely for these two signals. Rejected as the primary architecture because enemy label detection requires true OCR — template matching cannot generalise to arbitrary text. Remains viable as a fast path for the two known fixed strings if latency remains a concern after the threading migration.

### Keep multiprocessing, fix GPU access

Investigated but multiprocessing with CUDA on Windows requires `spawn` context (already in use) and separate GPU memory per process. For a 3-worker pool this would consume 3× GPU VRAM for the model. Threading with a shared model uses 1× VRAM regardless of detection task count.
