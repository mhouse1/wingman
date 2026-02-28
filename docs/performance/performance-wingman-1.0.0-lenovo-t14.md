# Wingman Performance Report

**Version:** 1.0.0
**Date:** 2026-02-28

## Test Environment
- Device: Lenovo T14 laptop
- Display: Laptop built-in display
- OS: Windows
- Python environment: .venv (no GPU acceleration)

## Results

### Level 1: Static Screenshot Test
- **Command:**
  ```bash
  uv run python test_analyzer.py test_screenshots/RESPAWN.png --grid
  ```
- **Torch warning:**
  > 'pin_memory' argument is set as true but no accelerator is found, then device pinned memory won't be used.
- **Detection:**
  - Respawn detected in region(s): [27]
  - Best match: Region 27 (confidence: 100.00%)
  - Recommendation: Use region=27 for faster analysis
- **Output files:**
  - output_grid.png
  - output_grid_highlighted.png
- **Total runtime:** 15.39 seconds


### Level 2: Live Capture Test
- **Command:**
  ```bash
  uv run python test_analyzer.py --capture
  ```
- **Warning:**
  > `VIRTUAL_ENV=.venv-1` does not match the project environment path `.venv` and will be ignored; use `--active` to target the active environment instead
- **Capture:**
  - Screenshot saved to live_capture.png
  - Region: (0, 0, 1920, 1200) on monitor 1
  - Image size: 1920x1200
- **Torch warning:**
  > Neither CUDA nor MPS are available - defaulting to CPU. Note: This module is much faster with a GPU.
- **Detection:**
  - Respawning: False
  - Confidence: 0.00%
  - Method: None
  - Respawn NOT detected in any region
- **Output files:**
  - output_grid.png
  - output_grid_highlighted.png (not highlighted, since no respawn detected)
- **Total runtime:** 6.87 seconds

### Level 3: Direct OCR Background Test
- **Command:**
  ```bash
  uv run python test_analyzer.py --unit-ocr
  ```
- **Warning:**
  > `VIRTUAL_ENV=.venv-1` does not match the project environment path `.venv` and will be ignored; use `--active` to target the active environment instead
- **Torch warning:**
  > Neither CUDA nor MPS are available - defaulting to CPU. Note: This module is much faster with a GPU.
  > 'pin_memory' argument is set as true but no accelerator is found, then device pinned memory won't be used.
- **OCR log excerpt:**
  ```
  text clean: ISAF (original: (ISAF] )
  text clean: JJQISI (original: @J@JQ@Is]i )
  text clean:  (original: 451. )
  text clean: DIT (original: Dit )
  text clean:  (original: 167 )
  text clean:  (original: 120 )
  text clean:  (original: ^ * *4+ )
  text clean: ISAFPJEH (original: [ISAF] @pJ@@@Eh )
  text clean: LY (original: Ly )
  text clean: BOPWUSTACHEMAX (original: [BoP] WustacheMax )
  text clean: STICKMON (original: stickmon )
  text clean: ISAFPPMDGI (original: [ISAF] @ppmdgi )
  text clean: DESTROYER (original: Destroyer )
  text clean: DOUBLEKILL (original: Double Kill )
  text clean: DESTROYED (original: DESTROYED )
  text clean: RESPAWN (original: RE S PA WN 7 )
  [UnitTest] _run_ocr_in_background result: is_respawning=True, confidence=100.00%, method=ocr
  [UnitTest] OCR runtime: 8.91 seconds
  ```

## Notes
- No GPU acceleration was available; performance may improve with CUDA or MPS.
- The analyzer correctly identified the respawn region with high confidence in Level 1.
- In Level 2, no respawn was detected in the live capture (expected if not on respawn screen).
- The runtime includes EasyOCR and grid-based analysis for all 36 regions.

---

For future performance tracking, update this document with new version numbers, hardware, and runtime results.
