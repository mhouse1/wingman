# Automated Test Levels for Game State Analyzer

This document describes the two levels of automated testing for the Game State Analyzer, including the commands and their purposes.

## Level 1: Static Screenshot Test

**Purpose:**
- Validates the analyzer's ability to detect the respawn state using a known, static screenshot.
- Ensures code changes do not break detection on reference images.

**Command:**
```bash
uv run python test_analyzer.py test_screenshots/RESPAWN.png --grid
```

**What it does:**
- Runs the analyzer on the provided screenshot (`test_screenshots/RESPAWN.png`).
- Performs grid-based analysis and saves visualizations.
- Reports which grid region(s) contain the respawn text and the confidence score.
- Useful for regression testing and CI pipelines.

## Level 2: Live Capture Test

**Purpose:**
- Validates the analyzer's ability to detect the respawn state using a fresh, live screenshot from the configured monitor and region.
- Ensures the end-to-end pipeline works in the current environment.

**Command:**
```bash
uv run python test_analyzer.py --capture
```

**What it does:**
- Captures a new screenshot from the configured monitor and region.
- Runs the analyzer and grid-based analysis on the captured image.
- Saves the screenshot and visualizations for review.
- Useful for hardware-in-the-loop and environment validation.

## Summary Table

| Level | Command                                                        | Use Case                        |
|-------|----------------------------------------------------------------|---------------------------------|
| 1     | uv run python test_analyzer.py test_screenshots/RESPAWN.png --grid | Regression/static image testing |
| 2     | uv run python test_analyzer.py --capture                       | Live/hardware-in-the-loop test  |

## Output Files

- `output_grid.png`: Shows the 6x6 grid overlay on the analyzed image, helping visualize how the analyzer divides the screen for region-based detection.
- `output_grid_highlighted.png`: Highlights the specific grid region(s) where the respawn text was detected with the highest confidence. This makes it easy to see exactly where the analyzer found the target state and is useful for calibrating the region setting in config.yaml.

## Notes
- Both levels save grid overlay images for visual inspection.
- Level 1 is suitable for automated CI; Level 2 is best for manual or semi-automated validation on real hardware.
- Ensure your config.yaml is set up correctly for your monitor and region before running Level 2.
