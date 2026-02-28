# How to Test the Game State Analyzer

This guide explains how to use `test_analyzer.py` to test and debug the respawn detection system.

## Prerequisites

1. Activate the virtual environment:
   ```bash
   source .venv/Scripts/activate  # Git Bash
   # or
   .venv\Scripts\activate  # PowerShell
   ```

2. Ensure you have test screenshots in the `test_screenshots/` directory (PNG format)

## Usage

### 1. Test a Single Image

Test respawn detection on one screenshot:

```bash
python test_analyzer.py path/to/screenshot.png

(Recommended) using uv

micha@impulse MINGW64 /c/dev-tools/github/wingman (analysis_b)
$ uv run python test_analyzer.py test_screenshots/RESPAWN.png --grid

```

Example:
```bash
python test_analyzer.py test_screenshots/RESPAWN.png
```

**Output:**
```
Loading config and image: test_screenshots/RESPAWN.png
Image loaded: 3839x1599 pixels
------------------------------------------------------------
Analyzing frame...

Game State Analysis:
  Is Respawning: True
  Confidence: 100.00%
  Detection Method: ocr

============================================================
✓ RESPAWN SCREEN DETECTED!
============================================================
```

* example run
```
(metalstorm-wingman) PS C:\dev-tools\github\wingman> python test_analyzer.py test_screenshots/RESPAWN.png --grid
Loading test_screenshots/RESPAWN.png...
Image size: 3839x1599
Neither CUDA nor MPS are available - defaulting to CPU. Note: This module is much faster with a GPU.
C:\dev-tools\github\wingman\.venv\Lib\site-packages\torch\utils\data\dataloader.py:668: UserWarning: 'pin_memory' argument is set as true but no accelerator is found, then device pinned memory won't be used.
  warnings.warn(warn_msg)

Full frame analysis:
  Respawning: True
  Confidence: 100.00%
  Method: ocr

✓ Saved grid visualization to output_grid.png

Testing individual regions (1-36):
------------------------------------------------------------
  Region  1:   gameplay | Conf: 0.00%
  Region  2:   gameplay | Conf: 0.00%
  Region  3:   gameplay | Conf: 0.00%
  Region  4:   gameplay | Conf: 0.00%
  Region  5:   gameplay | Conf: 0.00%
  Region  6:   gameplay | Conf: 0.00%
  Region  7:   gameplay | Conf: 0.00%
  Region  8:   gameplay | Conf: 0.00%
  Region  9:   gameplay | Conf: 0.00%
  Region 10:   gameplay | Conf: 0.00%
  Region 11:   gameplay | Conf: 0.00%
  Region 12:   gameplay | Conf: 0.00%
  Region 13:   gameplay | Conf: 0.00%
  Region 14:   gameplay | Conf: 0.00%
  Region 15:   gameplay | Conf: 0.00%
  Region 16:   gameplay | Conf: 0.00%
  Region 17:   gameplay | Conf: 0.00%
  Region 18:   gameplay | Conf: 0.00%
  Region 19:   gameplay | Conf: 0.00%
  Region 20:   gameplay | Conf: 0.00%
  Region 21:   gameplay | Conf: 0.00%
  Region 22:   gameplay | Conf: 0.00%
  Region 23:   gameplay | Conf: 0.00%
  Region 24:   gameplay | Conf: 0.00%
  Region 25:   gameplay | Conf: 0.00%
  Region 26:   gameplay | Conf: 0.00%
  Region 27: ✓ RESPAWN | Conf: 100.00%
  Region 28:   gameplay | Conf: 0.00%
  Region 29:   gameplay | Conf: 0.00%
  Region 30:   gameplay | Conf: 0.00%
  Region 31:   gameplay | Conf: 0.00%
  Region 32:   gameplay | Conf: 0.00%
  Region 33:   gameplay | Conf: 0.00%
  Region 34:   gameplay | Conf: 0.00%
  Region 35:   gameplay | Conf: 0.00%
  Region 36:   gameplay | Conf: 0.00%
------------------------------------------------------------

✓ Respawn detected in region(s): [27]
  Best match: Region 27 (confidence: 100.00%)

Recommendation: Use region=27 for faster analysis
✓ Saved highlighted grid to output_grid_highlighted.png
```

### 2. Test Multiple Images (Batch Mode)

Test all PNG files in the `test_screenshots/` directory:

```bash
python test_analyzer.py --multiple
```

**Output:**
```
Testing 2 images from test_screenshots:
============================================================
✓ missile_lock.png                         | GAMEPLAY | Conf: 0.00%
✓ RESPAWN.png                              | RESPAWN  | Conf: 100.00%
============================================================
```

This quickly shows which screenshots contain respawn text and which are regular gameplay.

### 3. Grid Visualization Mode

See which grid region contains the RESPAWN text:

```bash
python test_analyzer.py test_screenshots/RESPAWN.png --grid
```

**Output:**
- Tests all 36 regions individually
- Shows which region(s) contain "RESPAWN" text
- Saves two annotated images:
  - `output_grid.png` - Shows the 6x6 grid overlay
  - `output_grid_highlighted.png` - Highlights the region with detected text
- Recommends which region number to use in config.yaml

**Grid Layout:**
```
 1  2  3  4  5  6
 7  8  9 10 11 12
13 14 15 16 17 18
19 20 21 22 23 24
25 26 27 28 29 30
31 32 33 34 35 36
```

### 4. Combine Multiple and Grid

Test all images AND save grid visualizations:

```bash
python test_analyzer.py --multiple --grid
```

This processes all test screenshots and saves grid overlays for each one.

## Understanding the Output

### Detection Status
- **RESPAWN** - Respawn screen detected (confidence > 0%)
- **GAMEPLAY** - Normal gameplay (no respawn text found)

### Confidence Score
- **100%** - Text "RESPAWN" clearly detected by OCR
- **0%** - No respawn text found

### Detection Method
- **ocr** - EasyOCR successfully detected the text
- **None** - No detection method succeeded

## Troubleshooting

### OCR Not Detecting Text

If respawn screen isn't being detected:

1. **Check the region setting** in `wingman/config.yaml`:
   ```yaml
   respawn_detection:
     region: 27  # Try different regions if needed
   ```

2. **Run grid visualization** to find the correct region:
   ```bash
   python test_analyzer.py test_screenshots/RESPAWN.png --grid
   ```
   Look for which region number shows "✓ RESPAWN" in the output

3. **Check image quality** - Make sure the screenshot is clear and text is readable

4. **Enable debug mode** in `wingman/config.yaml`:
   ```yaml
   debug:
     show_window: true
   ```
   This saves OCR preprocessing images (`debug_ocr_*.png`) to help diagnose issues

### Performance Issues

If testing is slow:

- **First run is slow** - EasyOCR takes ~10 seconds to initialize on first use
- **Subsequent tests are fast** - The OCR reader stays loaded
- **Batch mode is efficient** - Tests multiple images without reloading OCR

## Common Workflows

### Adding New Test Screenshots

1. Take a screenshot during gameplay (respawn or normal)
2. Save as PNG in `test_screenshots/` directory
3. Run batch test:
   ```bash
   python test_analyzer.py --multiple
   ```

### Calibrating for Different Game States

1. Capture screenshot showing respawn text
2. Run grid visualization:
   ```bash
   python test_analyzer.py screenshot.png --grid
   ```
3. Note which region number contains the text
4. Update `wingman/config.yaml`:
   ```yaml
   respawn_detection:
     region: XX  # Use the region number from step 3
   ```
5. Verify with batch test:
   ```bash
   python test_analyzer.py --multiple
   ```

### Verifying After Code Changes

After modifying `analyzer.py`:

```bash
# Quick test on known respawn image
python test_analyzer.py test_screenshots/RESPAWN.png

# Full regression test on all screenshots
python test_analyzer.py --multiple
```

## Command Reference

| Command | Description |
|---------|-------------|
| `python test_analyzer.py IMAGE` | Test single image |
| `python test_analyzer.py --multiple` | Test all images in test_screenshots/ |
| `python test_analyzer.py IMAGE --grid` | Show grid analysis for one image |
| `python test_analyzer.py --multiple --grid` | Batch test with grid visualizations |

## See Also

- [ADR 003: Grid-Based Screen Scanning Architecture](adr/003-grid-based-screen-scanning-architecture.md)
- [wingman/config.yaml](../wingman/config.yaml) - Configuration settings
- [wingman/analyzer.py](../wingman/analyzer.py) - Source code
