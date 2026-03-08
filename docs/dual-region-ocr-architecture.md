# Dual-Region OCR Architecture (RESPAWN + INCOMING)

This document describes how Wingman uses a single captured screenshot to detect two independent HUD signals in one OCR pipeline:

- `RESPAWN` state in region `27` (mission restart/cancel logic)
- `INCOMING`/`MING` warning in region `10` (flare response)

## Why This Design

- Only one screenshot is captured per loop iteration.
- One background OCR thread processes both regions to avoid duplicated capture/OCR startup overhead.
- OCR results are cached to keep the main loop non-blocking.

## High-Level Flow

```mermaid
flowchart TD
    A[Main Loop Captures Full Frame] --> B[analyze_frame full frame]
    B --> C[_detect_respawn_ocr cache check]
    C -->|Cache fresh| D[Return cached respawn result]
    C -->|Cache expired| E[Schedule background OCR thread]

    E --> F[Extract Region 27 for RESPAWN]
    E --> G[Extract Region 10 for INCOMING]

    F --> H[Respawn preprocessing + OCR]
    G --> I[Incoming preprocessing variants + OCR]

    H --> J[Update respawn cache]
    I --> K[Update incoming cache]

    J --> L[Next loop reads caches]
    K --> L
    L --> M[Main loop acts on respawn/incoming states]
```

## Single Screenshot, Two Regions

```mermaid
flowchart LR
    A[Full Screenshot Frame] --> B[Grid Split 6x6]
    B --> C[Region 27 - RESPAWN path]
    B --> D[Region 10 - INCOMING path]

    C --> C1[Gray -> Otsu Binary -> Resize 0.7]
    C1 --> C2[EasyOCR]
    C2 --> C3[_is_respawn_text]

    D --> D1[Gray -> Otsu Binary]
    D1 --> D2[Variants: 1.0 / up1.4 / inv1.4 / gray up1.4]
    D2 --> D3[EasyOCR detail=0 paragraph=true]
    D3 --> D4[_is_incoming_text]
```

## Runtime Timing Model

```mermaid
sequenceDiagram
    participant MainProc as Main Loop
    participant Analyzer as GameStateAnalyzer
    participant BG as Background OCR Thread
    participant Cache as OCR Caches

    MainProc->>Analyzer: analyze_frame(frame)
    Analyzer->>Cache: read respawn cache
    alt cache valid
        Analyzer-->>MainProc: return cached respawn + incoming
    else cache expired
        Analyzer->>BG: schedule _run_ocr_in_background(frame)
        Analyzer-->>MainProc: return last cached values
        BG->>BG: OCR region 27 (respawn)
        BG->>Cache: write respawn result + timestamp
        BG->>BG: OCR region 10 (incoming)
        BG->>Cache: write incoming result + timestamp
    end

    MainProc->>MainProc: apply mission restart and flare logic
```

## Key Implementation Notes

- The main loop stays responsive because OCR is asynchronous.
- Region 10 uses test-validated preprocessing variants because incoming text can be thin/faint in runtime frames.
- Respawn and incoming results are independent cache entries, both produced from the same full-frame capture.
- Debug images are written to `tests/test-output/` to compare runtime OCR inputs with test behavior.
