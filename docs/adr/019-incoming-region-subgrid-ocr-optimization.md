# ADR 019: Incoming Missile Region Sub-Grid OCR Optimization

| Status   | Date       | Wingman Version |
|----------|------------|-----------------|
| Accepted | 2026-03-21 | 1.5.2           |

> **Update 2026-03-21:** Respawn sub-grid added (see below). ADR extended to cover both regions.

## Context

Wingman scans a region of the game HUD every OCR cycle to detect the "INCOMING" missile warning text (matched via substring `MING` or `ARNING`). The region is defined in the 8x8 main grid as region 21.

Prior to this change, the full cell of region 21 was passed to EasyOCR. This caused two problems:

1. **High OCR latency for the incoming region:** Incoming OCR was consistently taking 1–4 seconds per cycle during active gameplay, and up to 4+ seconds during high-load periods when the OCR thread pool was saturated.
2. **HUD noise in the region:** The raw OCR debug logs revealed that region 21 was picking up unrelated game HUD elements alongside the warning text — player callsigns (`ICEMANII15`), aircraft designations (`PUCKERFACTO2MISU-57`, `[VE]KETAM0.73MIJAS39`), and kill feed text. While these did not cause false positives (none contained `MING` or `ARNING`), they increased OCR processing time and indicated the scanned area was larger than necessary.

The raw OCR logging (added as part of the incoming detection debugging work) made this visible:

```
Analyzer: No match in incoming region 21 — raw OCR: gray_up_1p4='PUCKERFACTO2MISU-57', binary_otsu_up_1p4='PUCKERFOCTO2MLSU-57'
Analyzer: No match in incoming region 21 — raw OCR: gray_up_1p4='[VE]KETAM0.73MIJAS39', binary_otsu_up_1p4='[VE]KETAM0.73MIVAS3G'
```

Through observation it was determined that the actual "INCOMING" warning text appears in the top-left area of region 21. Subdividing region 21 into a 3×3 sub-grid and scanning only sub-region 1 (top-left cell) would cover the warning text while excluding the HUD noise.

## Decision

Introduce a configurable sub-grid crop applied to the incoming region before it is passed to OCR.

Two new config parameters were added to `respawn_detection` in `config.yaml`:

```yaml
incoming_subgrid_size: 3   # subdivide incoming_region into NxN cells
incoming_subregion: 1      # scan only this cell (1-based, row-major)
```

A `_crop_subregion(frame, grid_size, subregion_num)` helper was added to `GameStateAnalyzer`, using the same row-major indexing as the existing `get_region` method. The crop is applied in `_run_ocr_in_background` after the main grid extraction:

```python
incoming_frame = self.get_region(full_frame, self.incoming_region)
if self.incoming_subgrid_size > 1:
    incoming_frame = self._crop_subregion(incoming_frame, self.incoming_subgrid_size, self.incoming_subregion)
```

Setting `incoming_subgrid_size: 1` disables the feature and restores full-region scanning.

## Consequences

### Performance

Measured from production logs before and after the change:

| Metric | Before (full region 21) | After (3×3 sub-region 1) |
|---|---|---|
| Incoming OCR — typical steady state | 1.0s – 1.8s | **0.17s – 0.35s** |
| Incoming OCR — busy/high-load | 3.5s – 4.3s | 0.50s – 0.70s |
| "Background OCR busy" events | Frequent during missions | Essentially eliminated |
| Missile detection latency | 2–4s after warning appears | **0.18s – 0.29s** |

This represents a **5–10× reduction** in incoming OCR time, consistent with scanning approximately 1/9th the pixel area of the original region.

Before:
```
Analyzer: Parallel OCR Timings - Extract: 0.00s, Submit: 0.00s | Respawn OCR: 1.88s | Incoming OCR: 4.35s | Total: 4.35s
Analyzer: Parallel OCR Timings - Extract: 0.00s, Submit: 0.00s | Respawn OCR: 1.79s | Incoming OCR: 4.20s | Total: 4.20s
```

After:
```
Analyzer: Parallel OCR Timings - Extract: 0.00s, Submit: 0.00s | Respawn OCR: 0.34s | Incoming OCR: 0.29s | Total: 0.34s
Analyzer: Parallel OCR Timings - Extract: 0.00s, Submit: 0.00s | Respawn OCR: 0.27s | Incoming OCR: 0.18s | Total: 0.27s
```

### Detection quality

Missile detections in the post-change log were clean and fast:

```
🚀 INCOMING MISSILE DETECTED (variant=gray_up_1p4) - text='MING'  [0.29s incoming OCR]
🚀 INCOMING MISSILE DETECTED (variant=gray_up_1p4) - text='MING'  [0.18s incoming OCR]
```

No false positives or missed detections were observed. The HUD noise that previously appeared in raw OCR output (callsigns, aircraft designations) is no longer present since those elements fall outside sub-region 1.

### Limitations

- The optimal sub-region number (1 = top-left) is empirically determined for the current game resolution and HUD layout. If the HUD layout changes (resolution, UI scale, monitor setup), the sub-region may need to be reconfigured.
- During the respawn phase, incoming OCR time climbs back to 1.0–2.0s because the OCR thread pool workers are saturated by the heavier respawn region processing. This is acceptable since missile response is not the priority during respawn.

## Configuration

```yaml
respawn_detection:
  incoming_region: 21
  incoming_subgrid_size: 3   # 3x3 sub-grid
  incoming_subregion: 1      # top-left cell contains the warning text
```

To disable (scan full region 21):
```yaml
  incoming_subgrid_size: 1
```

## Respawn Region Sub-Grid (follow-up, 2026-03-21)

The same sub-grid technique was subsequently applied to the respawn region (region 44).

### Problem

The respawn region (region 44, 8×8 main grid) was being passed to EasyOCR in full. The "RESPA" text only appears in the lower portion of the cell. Visual inspection via the `--subgrid` overlay tool confirmed the text spans the bottom half of the region.

An initial 2×3 sub-grid was configured but the "RESPA" text was found to span two cells (regions 5 and 6), meaning neither cell alone captured a matchable string. The sub-grid was changed to 2×1 (top/bottom halves), with sub-region 2 (bottom half) containing the complete word.

Config:

```yaml
respawn_detection:
  respawn_subgrid_rows: 2
  respawn_subgrid_cols: 1
  respawn_subregion: 2      # bottom half — contains full "RESPA" text
```

### Performance (2×1 respawn sub-grid, production log)

| Metric | Before (full region 44) | After (2×1 sub-region 2) |
|---|---|---|
| Respawn OCR — typical steady state | 1.0s – 4.0s | **0.17s – 0.45s** |
| Respawn OCR — fast floor | ~1.0s | **0.16s** |
| Respawn OCR — peak spike | 4s+ | 2.02s |
| Overall improvement | — | **~5–8× faster typical case** |

Log samples (steady state after model warmup):

```
Respawn OCR: 0.26s | Incoming OCR: 0.26s | Total: 0.26s
Respawn OCR: 0.39s | Incoming OCR: 0.39s | Total: 0.40s
Respawn OCR: 0.17s | Incoming OCR: 0.16s | Total: 0.17s
Respawn OCR: 0.19s | Incoming OCR: 0.19s | Total: 0.19s
Respawn OCR: 0.23s | Incoming OCR: 0.23s | Total: 0.23s
```

Occasional spikes to 1.5–2.0s remain — these occur when EasyOCR processes complex image content (the GPU inference time is not strictly proportional to pixel count). They are infrequent and do not block the main loop.

### Detection quality

Respawn correctly detected with the 2×1 crop:

```
Respawn OCR results: [('gray', 'REPA', 0.749), ('binary', 'JSPAL', 0.136)]
Respawn detected (variant: gray, text: REPA)
Analyzer: detected 'RESPAWN' text (matched: 'REPA')
Analyzer: Parallel OCR Timings - Respawn OCR: 0.39s | Incoming OCR: 0.17s | Total: 0.39s
```

`REPA` (Levenshtein distance 1 from `RESPA`) is accepted by `_respawn_text_matches`. Two incoming detections in the same session also fired correctly with clean 3-flare bursts.

### `--subgrid` diagnostic tool

A `--subgrid` flag was added to `tests/analyzer_cli.py` to visualize sub-grid overlays on any screenshot:

```
uv run python tests/analyzer_cli.py test_screenshots/RESPAWNE.png --subgrid
```

Outputs `tests/test-output/subgrid_respawn.png` and `subgrid_incoming.png` showing the sub-grid cells with the configured sub-region highlighted. Used to confirm the 2×1 split captures the full "RESPA" text and to diagnose the initial 2×3 misconfiguration.

## Related ADRs
- ADR 007: OCR Time Reduction via Image Downscaling
- ADR 012: Dual-Region OCR Architecture (RESPAWN + INCOMING)
