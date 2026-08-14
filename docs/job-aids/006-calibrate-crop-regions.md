# Job Aid 006 — Calibrate Crop Regions

Crop regions are named percentage-coordinate rectangles defined in `config.yaml` under `crops:`.
Calibration maps each crop to the pixel area it should cover, using static reference screenshots.
The game is only needed for the screenshot-refresh step (`make p1`); the click-through calibration
itself runs offline.

---

## Standard recalibration workflow

Use this whenever the game UI shifts (patch, resolution change, HUD layout update):

```bash
make p1            # step 1 — refresh the reference screenshots
make recalibrate   # step 2 — walk through every crop and click new corners
```

**Step 1 — `make p1`** launches MetalStorm (auto-launched on Linux, same as `make rd`) and
replays capture path PATH1, overwriting the gate-corpus screenshots in
`test_screenshots/integration_test/` (`P1_000_LOBBY_PLAY.png` … `P1_080_LOBBY_AFTER_MISSION.png`).
A capture summary is written to `tests/test-output/capture_summary_PATH1.json`. The default
timeout is 600 s (`CAPTURE_TIMEOUT_S`).

**Step 2 — `make recalibrate`** walks through every screenshot/crop pair in
`tests/calibration_map.yaml` in order, opening each screenshot in a window. For each crop,
click two corners (or press S to keep the existing value). Coordinates are written to
`config.yaml` immediately after each crop, so quitting early loses nothing.

**Step 3 — verify** with the V-key overlay ([Task D](#task-d--verify-calibration-with-v-key)).

> **Not covered by `make p1`:** a few crops calibrate against manually captured screenshots at
> the `test_screenshots/` root rather than the gate corpus — currently `incoming` (`INCOMING.png`)
> and `event_refresh` / `event_refresh_dismiss` (`UNREADY.png`). If those screens changed,
> recapture them manually (press V in a running Wingman session, copy the raw frame) before
> running `make recalibrate`, or press S to keep their existing values.

---

## Quick reference

### Keyboard controls during calibration

| Key | Action |
|-----|--------|
| Left-click (×2) | Set top-left corner, then bottom-right corner |
| S | Skip — keep existing value (disabled if crop is undefined) |
| Q | Quit and save progress |

### Commands

| Command | When to use |
|---------|-------------|
| `make p1` | Refresh the PATH1 gate-corpus screenshots in `test_screenshots/integration_test/` |
| `make recalibrate` | Full loop — all crops in `calibration_map.yaml` order (alias of `make calibrate`) |
| `make calibrate-crop CROP=<name>` | Single crop — after a misclick or minor UI shift |
| `make add-crops` | Calibrate new crops from images in `test_screenshots/to_be_added/` |

### Crops config format (`config.yaml`)

```yaml
# [[x1_pct, y1_pct], [x2_pct, y2_pct]]  x=horizontal, y=vertical, 0.0–1.0
crops:
  respawn:   [[0.44, 0.55], [0.62, 0.70]]
  incoming:  [[0.00, 0.06], [0.22, 0.19]]
```

### Calibration map format (`tests/calibration_map.yaml`)

Screenshot paths are relative to `test_screenshots/`; gate-corpus entries live under
`integration_test/` and are refreshed by `make p1`.

```yaml
calibration:
  - screenshot: integration_test/P1_050_RESPAWN_VISIBLE_NO_HEALTH.png  # gate corpus, make p1 refreshes
    crops: [respawn]
  - screenshot: INCOMING.png   # 1920×1200 — manually captured
    crops: [incoming]
```

---

## Task A — Add a new crop region

1. **Get the game screen visible.** Navigate the game to the screen state where the new region appears.

2. **Take a reference screenshot** and save it to `test_screenshots/to_be_added/`. Name the file
   after the crop: the crop name will be exactly the filename stem (e.g. `HEALTH.png` → crop `HEALTH`).
   The screenshot must be taken while `config.yaml region:` has its current dimensions.

3. **Run the add-crops tool:**
   ```bash
   make add-crops
   ```
   It scans `test_screenshots/to_be_added/`, prompts one crop per image, and moves calibrated
   images into `test_screenshots/`.

4. **Click two corners** on the image — top-left first, then bottom-right. The green rectangle
   confirms the selection. Coordinates are written to `config.yaml` immediately.

5. **Add a `calibration_map.yaml` entry** for the new crop so future `make recalibrate` runs
   include it. Prefer pointing at a gate-corpus screenshot (`integration_test/P1_*.png`) if the
   crop is visible in one — those refresh automatically with `make p1`.

6. **Verify** — see [Task D](#task-d--verify-calibration-with-v-key).

---

## Task B — First-time calibration (all crops)

Use this when setting up on a new machine or after a full config reset.

1. **Refresh the gate-corpus screenshots:**
   ```bash
   make p1
   ```

2. **Manually capture** any screenshots the map needs that PATH1 does not cover (see the note in
   the workflow section above) and place them in `test_screenshots/`.

3. **Run the full calibration loop:**
   ```bash
   make recalibrate
   ```
   The tool validates dimensions and warns about any `config.yaml crops:` keys missing from the
   map, then opens each screenshot in sequence.

4. **For each crop prompt**, click top-left then bottom-right. Press Q to quit early — progress
   is saved after each crop.

5. **Verify** — see [Task D](#task-d--verify-calibration-with-v-key).

---

## Task C — Recalibration after capture region change

### Region position changed only (left/top moved, width/height unchanged)

Existing screenshots are still valid. Re-run the full loop and press S on any crop that still
looks correctly positioned:

```bash
make recalibrate
```

### Region size changed (width or height differs)

Existing screenshots are from a different frame size and the tool will refuse to run. New
screenshots are required.

1. Update `config.yaml region:` to the new dimensions.
2. Refresh the gate corpus at the new size:
   ```bash
   make p1
   ```
3. Manually recapture the non-corpus screenshots (`INCOMING.png`, `UNREADY.png`) and update the
   dimension comments in `calibration_map.yaml`.
4. Run the full loop:
   ```bash
   make recalibrate
   ```
   S is blocked for all crops (none have valid values for the new frame size). Complete the full
   click loop.
5. Verify — see [Task D](#task-d--verify-calibration-with-v-key).

---

## Task D — Verify calibration with V-key

While Wingman is running, press **V**. A debug screenshot is saved to `tests/test-output/` with
every named crop drawn as a labelled coloured rectangle over the live frame.

**Good calibration:** the box tightly encloses the target text with minimal padding.

**Box too large:** wastes OCR time. Recalibrate with `make calibrate-crop CROP=<name>` to tighten.

**Box clips text:** OCR will produce partial reads. Recalibrate with `make calibrate-crop CROP=<name>` to expand.

**Box in wrong position:** run `make calibrate-crop CROP=<name>` to redo that crop.

---

## Task E — Fix a misclick

There is no undo within a session. Correct a bad click pair immediately:

```bash
make calibrate-crop CROP=<name>
```

The existing (wrong) box is shown in yellow as a reference. Click the correct corners to overwrite.

---

## Troubleshooting

| Error | Cause | Fix |
|-------|-------|-----|
| `Dimension mismatch: P1_000_LOBBY_PLAY.png is 1920×1080, expected 1920×1200` | Screenshot taken at different region size | Re-run `make p1` (and recapture manual screenshots) with the current `config.yaml region:` in effect |
| `Warning: crops not in calibration map: [my_crop]` | New crop added to config but no screenshot entry | Add an entry to `calibration_map.yaml` pointing at a screenshot that shows the crop |
| S key does nothing | Crop has no existing value | A click pair is required — S is disabled for undefined crops |
| Box visible in V-key screenshot but wrong position | Region position changed | Re-run `make recalibrate`; press S on crops that still look correct |
| `make p1` hangs or times out | Game failed to launch or reach a PATH1 state | Check the game window is visible; default timeout is 600 s (`CAPTURE_TIMEOUT_S`) |
