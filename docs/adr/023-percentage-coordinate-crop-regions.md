# ADR 023 — Percentage-Coordinate Crop Regions

| Status   | Date       | Wingman Version |
|----------|------------|-----------------|
| Accepted | 2026-03-26 | 1.5.4           |

## Context

The grid-based region system (ADR 003) divides the capture frame into an N×N grid and identifies areas of interest by cell number. This was a practical starting point — the V-key debug overlay makes it easy to discover region numbers without knowing pixel coordinates — but the design has accumulated compounding costs:

**Crops are larger than necessary.** A grid cell is the minimum unit of extraction. Text that falls near a cell border forces the caller to include the adjacent cell, often doubling or tripling crop area. Larger crops mean more pixels for EasyOCR to process and slower inference.

**Subgrid workarounds exist because the grid is too coarse.** `incoming_subgrid_size`, `respawn_subgrid_rows`, `respawn_subgrid_cols`, and `_crop_subregion` were added (ADR 019) specifically to recover performance lost to oversized grid cells. These are workarounds for an architectural mismatch, not features.

**The grid couples region addressing to a fixed cell count.** Changing `grid_size` renumbers every region, invalidating all configured values. Adding a region of interest in an area that doesn't align with cell boundaries requires choosing the nearest cell and accepting the mismatch.

**The system is not resolution-independent.** A region number identifies a position only relative to the current `grid_size`; if the game renders at a different resolution or the capture window is resized, the relationship between cell number and screen content is unchanged only by coincidence.

## Decision

Replace the grid-based region system with **named percentage-coordinate crops**. Each crop is defined by two `[x%, y%]` corners — top-left and bottom-right — expressed as fractions of the full capture frame dimensions (0.0–1.0). All crop definitions live in `config.yaml` under a `crops:` section.

### Config schema (new)

```yaml
crops:
  respawn:       [[0.44, 0.55], [0.62, 0.70]]
  incoming:      [[0.00, 0.06], [0.22, 0.19]]
  click_to:      [[0.28, 0.72], [0.72, 0.84]]
  good_luck:     [[0.24, 0.38], [0.76, 0.54]]
  event_refresh: [[0.30, 0.30], [0.70, 0.55]]
  ready_button:  [[0.44, 0.88], [0.56, 0.96]]
```

Coordinates are **scale-independent within a stable capture region**: if the game renders at a different internal resolution but the capture area (`config.yaml` `region:`) stays the same, the named crops remain correctly positioned without reconfiguration. If the capture region itself is moved or resized, crops must be recalibrated — but this is true of any coordinate scheme tied to a capture frame.

### Core extraction (new)

```python
def get_crop(frame, x1: float, y1: float, x2: float, y2: float):
    """Extract a percentage-coordinate crop from a full frame."""
    h, w = frame.shape[:2]
    return frame[int(h * y1):int(h * y2), int(w * x1):int(w * x2)]
```

This is a pure function with no instance state. `GameStateAnalyzer` loads crop coordinates from config and stores them as named tuples; callers pass the coordinates directly to `get_crop`.

### Config migration (before → after)

| Before | After |
|---|---|
| `respawn_detection.region: 44` | `crops.respawn: [[x1,y1],[x2,y2]]` |
| `respawn_detection.incoming_region: 21` | `crops.incoming: [[x1,y1],[x2,y2]]` |
| `respawn_detection.click_to_region: 60` | `crops.click_to: [[x1,y1],[x2,y2]]` |
| `respawn_detection.incoming_subgrid_size: 3` | _(removed — crop is exact)_ |
| `respawn_detection.respawn_subgrid_rows/cols: 2/1` | _(removed — crop is exact)_ |
| `controls.good_luck_region: 16` | `crops.good_luck: [[x1,y1],[x2,y2]]` |
| `controls.event_refresh_region: 30` | `crops.event_refresh: [[x1,y1],[x2,y2]]` |
| `controls.ready_button_region: 64` | `crops.ready_button: [[x1,y1],[x2,y2]]` |
| `respawn_detection.grid_size: 8` | _(removed)_ |
| `debug.capture_grid_size: 8` | _(removed — debug overlay draws named boxes)_ |

### Debug overlay (V-key screenshot)

The V-key debug screenshot replaces the numbered grid overlay with labelled bounding boxes drawn from the `crops` config. Each named crop is drawn as a coloured rectangle with its name, making calibration as simple as: take a screenshot, measure the target area as a fraction of total width/height, update the two coordinates.

### Clicking by crop coordinates

`click_grid_region` currently calculates click targets from a region number:

```python
# current
abs_x = int(abs_left + (col + 0.5) * cell_w)
abs_y = int(abs_top  + (row + 0.5) * cell_h)
```

Under the new system the click target is the centre of the named crop:

```python
# new
abs_x = int(abs_left + ((x1 + x2) / 2) * cap_w)
abs_y = int(abs_top  + ((y1 + y2) / 2) * cap_h)
```

The event-refresh corner dismiss (`4 * cap_w / 8, 4 * cap_h / 8`) is also grid-derived. It becomes a named crop entry:

```yaml
crops:
  event_refresh_dismiss: [[0.49, 0.49], [0.51, 0.51]]  # corner point between regions 28/29/36/37
```

### Calibration tooling

The V-key debug screenshot is augmented to draw each named crop as a labelled bounding box, replacing the numbered grid overlay. For initial calibration, `analyzer_cli.py` gains a `--calibrate` flag: it captures a live screenshot and lets the user click two corners, then prints the resulting `[x%, y%]` pairs ready to paste into config.

### Code changes

| File | Change |
|---|---|
| `wingman/analyzer.py` | Replace `get_region(frame, region_num)` and `_crop_subregion` with `get_crop(frame, x1, y1, x2, y2)`. Remove `grid_rows`, `grid_cols`, `respawn_region`, `incoming_region`, `click_to_region`, subgrid fields. Load named crop coordinates from `crops:` config section. |
| `wingman/controller.py` | Replace `ready_button_region`, `good_luck_region`, `event_refresh_region` integer fields with crop-coordinate tuples. Update `click_grid_region` to compute click target from crop centre. Add `event_refresh_dismiss` crop for corner click. Remove `REGION_*` integer-passing call sites. |
| `wingman/main.py` | Update `click_grid_region` calls to pass crop coordinates directly. |
| `wingman/config.yaml` | Add `crops:` section; remove `respawn_detection.grid_size`, region number keys, and subgrid keys. |
| `tests/analyzer_cli.py` | Add `--calibrate` mode: live capture → click two corners → print percentage coordinates. |
| `tests/` | Update any test that constructs region numbers. |

## Future Capability: Target Tracking

This section describes a follow-on feature enabled by the crop architecture but **not part of this ADR's implementation scope**. It is recorded here to document the design intent that motivated some of the architectural choices above.

The percentage-coordinate system directly enables frame-to-frame enemy tracking using the HUD diamond markers.

### HUD diamond behaviour

The game renders a **green diamond** on each enemy visible on screen. When missile lock is achieved the diamond turns **red**. The diamond moves with the enemy aircraft across the frame.

### Tracking approach

Define a named crop covering the area of the HUD where enemy diamonds appear (typically the central combat zone, e.g. `tracking_zone: [[0.20, 0.15], [0.80, 0.85]]`). On each scan:

1. **Detect diamonds** — run an HSV colour filter over the crop for the green or red diamond colour range. Each contour centroid is a candidate target.
2. **Detect lock** — if any detected diamond is red (HSV hue shift from green), missile lock is confirmed.
3. **Infer heading** — compare centroid positions between the current frame and the previous frame. The delta vector `(dx, dy)` gives the target's screen-space velocity. A target moving right means `dx > 0`; anticipating the lead angle becomes possible.

```python
# Pseudocode — per scan cycle
diamonds = detect_diamonds_hsv(crop, color="green")
locked   = detect_diamonds_hsv(crop, color="red")

if locked:
    trigger_weapon_fire()

if diamonds and prev_diamonds:
    dx = diamonds[0].cx - prev_diamonds[0].cx
    dy = diamonds[0].cy - prev_diamonds[0].cy
    # dx/dy in pixels relative to crop size → convert to pct for resolution independence
```

### Why this requires the new architecture

With grid regions this would not be practical:
- A grid cell is too large — colour noise from the cockpit and sky fills the cell, drowning out the small diamond marker.
- Subgrid crops can isolate a cell but the diamond moves between cells, requiring the caller to track which cell it is currently in.
- Percentage coordinates allow the tracking zone to be sized and positioned precisely around the combat HUD with no wasted area, and the same coordinates work at any capture resolution.

### Config additions (future)

```yaml
crops:
  tracking_zone: [[0.20, 0.15], [0.80, 0.85]]  # Area scanned for HUD diamonds

tracking:
  diamond_green_hsv_lower: [40, 120, 120]
  diamond_green_hsv_upper: [80, 255, 255]
  diamond_red_hsv_lower:   [0,  150, 150]
  diamond_red_hsv_upper:   [10, 255, 255]
  min_contour_area: 20   # px² — filters noise below diamond size
```

## Consequences

**Positive:**
- OCR crops are exactly sized to the text of interest — no wasted pixels, faster inference.
- Subgrid workarounds (`_crop_subregion`, `incoming_subgrid_size`, `respawn_subgrid_rows/cols`) are deleted.
- Crops are scale-independent within a stable capture region; no recalibration needed when the game's internal render resolution changes.
- Adding a new region of interest requires only two coordinates, not grid-size arithmetic.
- `get_crop` is a pure function — trivially testable and reusable.

**Negative / migration cost:**
- All existing region numbers must be recalibrated to percentage coordinates. The V-key screenshot with the new labelled overlay is the calibration tool.
- Config schema is a breaking change; old `config.yaml` files with `region:` keys will not work until updated.

## Supersedes

ADR 003 — Grid-Based Screen Scanning Architecture.

## References

- [ADR 003](003-grid-based-screen-scanning-architecture.md) — original grid design
- [ADR 019](019-incoming-region-subgrid-ocr-optimization.md) — subgrid workaround (to be removed)
- [ADR 020](020-cpu-only-ocr-optimizations.md) — OCR performance baseline
- [wingman/analyzer.py](../../wingman/analyzer.py) — `get_region`, `_crop_subregion`
- [wingman/config.yaml](../../wingman/config.yaml) — region configuration
