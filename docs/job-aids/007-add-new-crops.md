# Job Aid 007 — Add New Crops from Screenshots

| Status | Date | Wingman Version |
|---|---|---|
| Draft | 2026-04-09 | 1.6.1 |

## Purpose

Use this process to add new crop regions from screenshots placed in `test_screenshots/to_be_added`.
Each image is calibrated interactively, saved into `wingman/config.yaml` under `crops:`, and then moved to `test_screenshots/` after it is successfully saved.

## Prerequisites

- Screenshot dimensions must match `region.width` and `region.height` in `wingman/config.yaml`.
- Each image filename becomes the crop name (filename stem only, extension removed).
- Supported image formats: `.png`, `.jpg`, `.jpeg`, `.bmp`, `.webp`, `.tif`, `.tiff`.

## One-Time Setup

1. Put new screenshots into `test_screenshots/to_be_added/`.
2. Name each file exactly how you want the crop key in `config.yaml`.

Examples:

- `REVEAL_ALL.png` -> `crops.REVEAL_ALL`
- `TAP_HERE_TO_CONTINUE.png` -> `crops.TAP_HERE_TO_CONTINUE`
- `UNLOCK_CLOSE.png` -> `crops.UNLOCK_CLOSE`

## Run Calibration

Run:

```bash
make add_new_crops
```

This executes:

```bash
uv run python tests/calibrate.py --add-new-crops
```

For each image in `test_screenshots/to_be_added/`:

1. A calibration window opens.
2. Click top-left corner of the target area.
3. Click bottom-right corner of the target area.
4. The crop is written to `wingman/config.yaml` immediately.
5. The screenshot is moved to `test_screenshots/`.

## Controls During Calibration

- Left-click twice: set top-left then bottom-right.
- `S`: skip current crop (only works if that crop already exists).
- `Q`: quit and keep progress saved so far.

## What Gets Updated

- `wingman/config.yaml`
  - New or updated crop entry is written under `crops:`.
- `test_screenshots/to_be_added/`
  - Successfully calibrated images are removed from this folder.
- `test_screenshots/`
  - Successfully calibrated images are moved here.
  - If a same-name file already exists, the moved file gets a suffix (example: `_1`, `_2`).

## Troubleshooting

### No images found

If the tool reports no images, verify files are in `test_screenshots/to_be_added/` and have a supported extension.

### Dimension mismatch error

If an image size differs from `config.yaml` region dimensions, recalibrate is blocked.
Fix by retaking the screenshot using the same capture region size as `wingman/config.yaml`.

### Crop naming issue

If the crop name is not what you expected, rename the image file first, then run `make add_new_crops` again.

## Validation

After calibration completes:

1. Open `wingman/config.yaml` and confirm all new crop keys under `crops:`.
2. Confirm `test_screenshots/to_be_added/` is empty (or contains only skipped/unprocessed images).
3. Confirm moved files are present in `test_screenshots/`.
