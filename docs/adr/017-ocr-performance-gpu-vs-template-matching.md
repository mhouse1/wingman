# ADR 017 — OCR Performance: GPU Inference vs Template Matching

## Status

Proposed — no decision made
**Date:** 2026-03-20

## Context

Threading (ADR 016) eliminated IPC overhead and unblocked GPU access, but measured OCR performance on CPU remains high:

| Metric | Respawn OCR | Incoming OCR | Total (wall clock) |
|--------|------------|--------------|-------------------|
| Average | 2.63s | 3.07s | 3.25s |
| Best | 1.63s | 1.82s | 1.85s |
| Worst | 3.78s | 4.60s | 4.60s |

*(Source: live session log 2026-03-20, 11 cycles, CPU-only)*

The high variance (1.85–4.60s) reflects CPU thread contention: the respawn and incoming OCR threads compete for the same cores. When both are active simultaneously, whichever thread loses core access balloons — cycle 9 shows incoming at 4.60s while respawn was only 1.63s.

At ~3.25s average, a missile warning can sit undetected for 3+ seconds before flares deploy. The system works, but the detection window is wide.

Two paths exist to close it:

1. **Enable GPU inference** — CUDA path already implemented in v1.5.0; blocked only by the CPU-only PyTorch wheel (`torch==2.10.0+cpu`). Replacing the wheel is the only required change.
2. **Template matching** — Replace EasyOCR for the four known fixed UI strings with OpenCV `matchTemplate`, which runs in <5ms on CPU with no neural network involved.

ADR 016 noted template matching as viable for the current static strings but deferred the decision.

## Options

### Option A: Enable GPU Inference

Replace the CPU PyTorch wheel with a CUDA build matching the installed CUDA version:

```bash
uv remove torch
uv add torch --index-url https://download.pytorch.org/whl/cu121  # match to nvidia-smi output
```

No code changes required. EasyOCR already attempts GPU on init and falls back gracefully.

**Expected result:** OCR cycle time drops from ~3.25s to <200ms (per ADR 016 projections).

**Risks:**
- Requires a compatible NVIDIA GPU and CUDA toolkit
- CUDA wheel increases package size significantly
- If GPU is unavailable (different machine, driver issue), runtime silently falls back to CPU with no change in performance — the fix is hardware-dependent

**Brittleness:** None. EasyOCR with Levenshtein fuzzy matching handles font rendering variations, partial text visibility, and future UI changes without modification.

---

### Option B: Template Matching (CPU)

Capture reference screenshots of each target text as it appears in-game, then use `cv2.matchTemplate` to scan the configured grid region for each one on every OCR cycle.

**Expected result:** Detection latency <5ms per region on CPU — effectively zero compared to current OCR times.

**Risks:**
- **Brittle to UI changes:** Any game update that alters font, color, size, or position of a target string requires new reference images and re-tuning of the match threshold.
- **Brittle to rendering variation:** Anti-aliasing, resolution scaling, or HDR rendering can cause false negatives. EasyOCR's Levenshtein matching handles `REPA` for `RESPAWN`; template matching does not tolerate that kind of partial/noisy match without additional preprocessing.
- **Not generalisable:** Future detection tasks (e.g. enemy aircraft type from HUD labels, ammo counter, health bar text) require true OCR. Template matching cannot cover arbitrary text. Two parallel pipelines (template for known strings, OCR for dynamic text) would need to coexist and be maintained separately.

---

### Option C: Template Matching as Fast Pre-filter (Hybrid)

Use template matching as a first-pass check. If it matches, act immediately. If it does not match, skip EasyOCR for that cycle (template miss means the text is absent).

```
Frame arrives
  → matchTemplate (region 44) → match? → RESPAWN confirmed, act
  → matchTemplate (region 21) → match? → INCOMING confirmed, act
  → no match on either → skip OCR this cycle
```

EasyOCR would only run when template matching produces an ambiguous result (near-threshold match) or as a scheduled low-frequency verification pass.

**Expected result:** Near-zero latency on the common case (text visible or absent). EasyOCR runs rarely, eliminating most CPU contention.

**Risks:** All brittleness risks of Option B apply to the fast path. If template matching misses a true positive (rendering variation), the EasyOCR fallback must still catch it — requiring careful threshold tuning to decide when "ambiguous" triggers a fallback.

---

## Comparison

| | Option A (GPU) | Option B (Template) | Option C (Hybrid) |
|--|--|--|--|
| Detection latency | <200ms | <5ms | <5ms (common case) |
| CPU contention | Eliminated (moves to GPU) | Eliminated | Reduced |
| Code changes | Dependency swap only | New pipeline | New pipeline + logic |
| Robustness to UI changes | High (fuzzy OCR) | Low (exact match) | Low fast path, high fallback |
| Future-proof for new detections | Yes (shared OCR pipeline) | No (OCR still needed alongside) | Partial |
| Hardware dependency | NVIDIA GPU + CUDA | None | None |

## Decision

**Undecided.**

Option A is the lower-risk path if a compatible GPU is available: zero code changes, no brittleness introduced, and the existing OCR pipeline remains the single detection mechanism for all current and future targets.

Option B / C become relevant if GPU remains unavailable and CPU latency is causing missed detections in practice. At that point Option C (hybrid) is preferred over Option B alone — it preserves OCR as a fallback and avoids splitting the detection pipeline permanently.

## Consequences of Deferring

Current CPU performance (3.25s avg) is functional but wide. Two incoming missile detections in the log were genuine hits (`MING`, `ARNING`) — the bot did deploy flares, so the latency is not yet causing mission failures. Monitor logs for missed detections (incoming text visible in screenshot but no detection logged) before treating this as urgent.
