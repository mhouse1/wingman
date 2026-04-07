# Job Aid 006 — Calibrate Crop Regions

Crop regions are named percentage-coordinate rectangles defined in `config.yaml` under `crops:`.
Calibration maps each crop to the pixel area it should cover, using static reference screenshots.
The game does not need to be running.

---

## Quick reference

### Keyboard controls during calibration

| Key | Action |
|-----|--------|
| Left-click (×2) | Set top-left corner, then bottom-right corner |
| S | Skip — keep existing value (disabled if crop is undefined) |
| Q | Quit and save progress |

### CLI flags

| Command | When to use |
|---------|-------------|
| `python tests/calibrate.py` | Full loop — all crops in `calibration_map.yaml` order |
| `python tests/calibrate.py --crop <name>` | Single crop — after a misclick or minor UI shift |

### Crops config format (`config.yaml`)

```yaml
# [[x1_pct, y1_pct], [x2_pct, y2_pct]]  x=horizontal, y=vertical, 0.0–1.0
crops:
  respawn:   [[0.44, 0.55], [0.62, 0.70]]
  incoming:  [[0.00, 0.06], [0.22, 0.19]]
```

### Calibration map format (`tests/calibration_map.yaml`)

```yaml
calibration:
  - screenshot: respawn_screen.png   # 1920×1200 — must match config.yaml region w×h
    crops: [respawn, incoming, click_to]
  - screenshot: lobby_ready.png      # 1920×1200
    crops: [ready_button]
```

---

## Task A — Add a new crop region

1. **Get the game screen visible.** Navigate the game to the screen state where the new region appears.

2. **Take a reference screenshot** and save it to `tests/test_screenshots/`. Filename should describe the screen state (e.g., `respawn_screen.png`). The screenshot must be taken while `config.yaml region:` has its current dimensions — note the width × height.

3. **Add a `crops:` entry** in `config.yaml`:
   ```yaml
   crops:
     my_new_crop: [[0.0, 0.0], [0.0, 0.0]]  # placeholder — calibrate.py will overwrite
   ```

4. **Add or update a `calibration_map.yaml` entry** to map the screenshot to the new crop name:
   ```yaml
   - screenshot: my_screen.png  # 1920×1200
     crops: [my_new_crop]
   ```

5. **Run the calibration tool:**
   ```
   python tests/calibrate.py --crop my_new_crop
   ```

6. **Click two corners** on the image — top-left first, then bottom-right. The green rectangle confirms the selection. Coordinates are written to `config.yaml` immediately.

7. **Verify** — see [Task D](#task-d--verify-calibration-with-v-key).

---

## Task B — First-time calibration (all crops)

Use this when setting up on a new machine or after a full config reset.

1. **Capture reference screenshots** for every game screen state that contains crops. Save each to `tests/test_screenshots/` with the current `config.yaml region:` in effect.

2. **Populate `tests/calibration_map.yaml`** — one entry per screenshot, listing all crop names visible in that screen state. Add the region dimensions as a comment.

3. **Run the full calibration loop:**
   ```
   python tests/calibrate.py
   ```
   The tool validates dimensions and warns about any `config.yaml crops:` keys missing from the map, then opens each screenshot in sequence.

4. **For each crop prompt**, click top-left then bottom-right. Press Q to quit early — progress is saved after each crop.

5. **Verify** — see [Task D](#task-d--verify-calibration-with-v-key).

---

## Task C — Recalibration after capture region change

### Region position changed only (left/top moved, width/height unchanged)

Existing screenshots are still valid. Re-run the full loop and press S on any crop that still looks correctly positioned:

```
python tests/calibrate.py
```

### Region size changed (width or height differs)

Existing screenshots are from a different frame size and the tool will refuse to run. New screenshots are required.

1. Update `config.yaml region:` to the new dimensions.
2. Capture fresh reference screenshots for every game screen state.
3. Update the dimension comments in `calibration_map.yaml`.
4. Run the full loop:
   ```
   python tests/calibrate.py
   ```
   S is blocked for all crops (none have valid values for the new frame size). Complete the full click loop.
5. Verify — see [Task D](#task-d--verify-calibration-with-v-key).

---

## Task D — Verify calibration with V-key

While Wingman is running, press **V**. A debug screenshot is saved to `tests/test-output/` with every named crop drawn as a labelled coloured rectangle over the live frame.

**Good calibration:** the box tightly encloses the target text with minimal padding.

**Box too large:** wastes OCR time. Recalibrate with `--crop <name>` to tighten.

**Box clips text:** OCR will produce partial reads. Recalibrate with `--crop <name>` to expand.

**Box in wrong position:** run `python tests/calibrate.py --crop <name>` to redo that crop.

---

## Task E — Fix a misclick

There is no undo within a session. Correct a bad click pair immediately:

```
python tests/calibrate.py --crop <name>
```

The existing (wrong) box is shown in yellow as a reference. Click the correct corners to overwrite.

---

## Troubleshooting

| Error | Cause | Fix |
|-------|-------|-----|
| `Dimension mismatch: respawn_screen.png is 1920×1080, expected 1920×1200` | Screenshot taken at different region size | Recapture the screenshot with the current `config.yaml region:` in effect |
| `Warning: crops not in calibration map: [my_crop]` | New crop added to config but no screenshot entry | Add an entry to `calibration_map.yaml` and capture a screenshot |
| S key does nothing | Crop has no existing value | A click pair is required — S is disabled for undefined crops |
| Box visible in V-key screenshot but wrong position | Region position changed | Re-run `calibrate.py`; press S on crops that still look correct |
