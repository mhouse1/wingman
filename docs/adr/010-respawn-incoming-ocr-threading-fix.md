# ADR 010: Respawn & Incoming Detection Threading Fix

**Status:** Accepted  
**Date:** 2026-03-13  

## Context
Respawn detection became unreliable after threading and worker pool changes. Incoming missile detection remained fast, but respawn detection was delayed or missed. The system uses background threading and multiprocessing for non-blocking OCR analysis.

## Decision
- Improved respawn detection by:
  - Running OCR on both grayscale and thresholded images for the respawn region.
  - Relaxing Levenshtein distance tolerance for short OCR results.
  - Logging all OCR results for debugging.
- Increased worker pool size (from 2 to 4) and enabled GPU for OCR workers.
    - More workers allow multiple OCR tasks to run in parallel, reducing wait time when frames arrive quickly.
    - This ensures both respawn and incoming detection are processed promptly, even under heavy load.
    - Note: GPU acceleration is not used; all OCR processing runs on CPU. EasyOCR falls back to CPU if GPU is unavailable or unsupported.
- Reduced OCR cooldown for faster detection.

## Consequences
- Respawn detection is now robust and responsive.
- Incoming missile detection remains fast and reliable.
- Debug logs provide visibility into OCR results and matching.

## Detection Flow (Mermaid Diagram)

### Overall Threading & OCR Flow

```mermaid
graph TD
    A[Main Thread] -->|Frame arrives| B[Background OCR Thread]
    B --> C[OCR Pool]
    C --> D1[Worker 1: Respawn Region]
    C --> D2[Worker 2: Incoming Region]
    C --> D3[Worker 3: Respawn/Incoming]
    C --> D4[Worker 4: Respawn/Incoming]
    D1 --> F1[OCR: Grayscale & Threshold]
    D2 --> F2[OCR: Variants]
    D3 --> F3[OCR: Grayscale/Variants]
    D4 --> F4[OCR: Grayscale/Variants]
    F1 --> H1[Respawn Text Matching]
    F2 --> I1[Incoming Text Matching]
    F3 --> H2[Respawn/Incoming Matching]
    F4 --> I2[Respawn/Incoming Matching]
    H1 --> J[Update Respawn Cache]
    I1 --> K[Update Incoming Cache]
    H2 --> J
    I2 --> K
    subgraph OCR Pool
        D1
        D2
        D3
        D4
    end
```

### Respawn Detection Logic

```mermaid
flowchart TD
    R1[Extract respawn region] --> R2[Preprocess: grayscale & threshold]
    R2 --> R3[Resize images]
    R3 --> R4[Run EasyOCR]
    R4 --> R5[Log OCR results]
    R5 --> R6[Levenshtein match: RESPAWN/RESPA]
    R6 --> R7{Match?}
    R7 -- Yes --> R8[Update cache: respawning]
    R7 -- No --> R9[No respawn detected]
```

### Incoming Detection Logic

```mermaid
flowchart TD
    I1[Extract incoming region] --> I2[Preprocess: grayscale & threshold]
    I2 --> I3[Resize variants]
    I3 --> I4[Run EasyOCR]
    I4 --> I5[Log OCR results]
    I5 --> I6[Match: MING/WARNING]
    I6 --> I7{Match?}
    I7 -- Yes --> I8[Update cache: incoming]
    I7 -- No --> I9[No incoming detected]
```

## References
- [004-background-ocr-threading-for-non-blocking-analysis.md](004-background-ocr-threading-for-non-blocking-analysis.md)
- [wingman/analyzer.py](../../wingman/analyzer.py)
- [wingman/config.yaml](../../wingman/config.yaml)
