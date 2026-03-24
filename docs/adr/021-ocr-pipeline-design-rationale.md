# ADR 021 — OCR Pipeline Design Rationale

| Status   | Date       | Wingman Version |
|----------|------------|-----------------|
| Accepted | 2026-03-23 | 1.5.3           |

## Context

Wingman's OCR pipeline has accumulated several non-obvious design decisions across multiple ADRs. This document consolidates the rationale behind the five patterns that make it robust in production — each one exists because a simpler approach was tried or considered and found to fail.

## Design Decisions

### 1. Thread-local EasyOCR readers

Each `ThreadPoolExecutor` worker thread owns its own `EasyOCR` reader instance, initialized once on first use via `_get_thread_ocr_reader()`.

**The naive approach** — sharing a single reader behind a `threading.Lock` — would serialize all OCR calls. Respawn and incoming detection would execute sequentially despite running in separate futures, eliminating the parallelism the executor was created to provide.

**The thread-local approach** gives each thread a private reader. Inference runs concurrently with no lock contention, while still sharing the process address space (no IPC, no serialization of numpy arrays across process boundaries). This was the core insight of the multiprocessing → threading migration in ADR 016.

### 2. Non-blocking main loop with stale-cache reads

`analyze_frame()` always returns immediately. When the cache is fresh it returns the cached result; when the cache is expired it schedules a background OCR job and returns the *stale* cached result. The main loop never waits for OCR.

**The naive approach** — calling OCR inline in the main loop — blocked for 2.6–2.9s per cycle (measured in ADR 004). Hotkeys were unresponsive, mission control was delayed, and the entire system felt frozen during detection.

**The stale-cache approach** works because the signals Wingman detects are persistent: respawn screens are visible for 5–20 seconds, incoming warnings for 2–5 seconds, click-to prompts for several seconds. A result that is 0.2s stale is still actionable. Blocking the main loop for 2.6s to get a fresh result is strictly worse than acting on a stale one.

### 3. Multiple preprocessing variants per region

Each region is processed through multiple image variants before OCR:

- **Respawn**: grayscale (0.7× resize) + binary Otsu (0.7× resize)
- **Incoming**: grayscale (1.4× upscale) + binary Otsu (1.4× upscale)
- **Click-to / Good Luck / Event Refresh**: grayscale upscale + binary Otsu

OCR on game HUD text is unreliable through a single preprocessing path. Anti-aliasing, HDR rendering, partial occlusion, and background colour bleed all affect the same text differently depending on the frame. A single preprocessing that works for a clean frame may produce garbage on a discoloured or partially obscured one — as captured in `RESPAWNC.png` (the discoloured test image).

Running variants and accepting the first match that clears the Levenshtein threshold is more expensive than a single OCR call but significantly more reliable. The background thread absorbs the cost without blocking the main loop.

### 4. Levenshtein fuzzy matching

OCR results are not compared with exact string equality. Every candidate string goes through `_is_respawn_text()` / `_is_incoming_text()` which apply edit-distance matching with a configurable threshold.

**The naive approach** — `if "RESPAWN" in text` — fails on partial reads. In practice, EasyOCR frequently returns `REPA`, `RESPA`, `RESPAW`, or `RESPMN` depending on which pixels are visible in the captured region and how the binary threshold cuts the letterforms. Exact matching would miss all of these.

**Levenshtein matching** tolerates substitutions, deletions, and insertions up to a threshold. `REPA` has edit distance 3 from `RESPAWN` — caught. `MING` has edit distance 5 from `INCOMING` but is the most reliably captured substring in region 21 — caught by substring check first, Levenshtein as fallback. This was formalized in ADR 008.

### 5. Serialized reader initialization

`_get_thread_ocr_reader()` acquires `_ocr_init_lock` before creating a new reader. Initialization of the first reader on a fresh install triggers a model file download to a shared temp path. If two threads initialize concurrently, both attempt to extract the same `temp.zip`, causing a `FileNotFoundError` on the second thread.

The lock serializes first-time initialization only — after the model is cached to disk, subsequent threads initialize in ~1s and the lock is held briefly. In production (model already downloaded), the lock has no measurable effect. It exists to prevent a specific race that would otherwise silently corrupt new installs.

## What Each Pattern Addresses

| Pattern | Failure mode it prevents |
|---|---|
| Thread-local readers | Serialized OCR calls despite parallel executor |
| Stale-cache non-blocking reads | 2.6s main loop freeze per detection cycle |
| Multiple preprocessing variants | False negatives on discoloured / partially occluded frames |
| Levenshtein fuzzy matching | False negatives on partial OCR reads (`REPA`, `MING`) |
| Serialized reader init | `FileNotFoundError` race on first-run model download |

## References

- [ADR 001](001-easyocr-for-screen-number-detection.md) — EasyOCR selection
- [ADR 004](004-background-ocr-threading-for-non-blocking-analysis.md) — non-blocking background OCR, stale cache design
- [ADR 008](008-levenshtein-distance-for-ocr-text-matching.md) — fuzzy text matching
- [ADR 012](012-dual-region-ocr-architecture.md) — single-frame dual-region pipeline
- [ADR 016](016-ocr-multiprocessing-to-threading-migration.md) — thread-local readers, multiprocessing → threading migration
- [wingman/analyzer.py](../../wingman/analyzer.py) — `_get_thread_ocr_reader`, `_ocr_init_lock`, `_process_respawn_region`, `_process_incoming_region`, `_is_respawn_text`, `analyze_frame`
