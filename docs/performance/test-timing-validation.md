# Test Timing Validation (Level 5)

## Overview
The automated test suite includes **Level 5: Performance Validation** as the final test stage. This test validates execution times of all previous tests against baseline durations extracted from `tests/test-output/report.html`.

## How It Works

**Level 5 test runs last** and:
1. Collects timing data from all previous tests (Levels 1-4) via pytest hooks
2. Compares actual durations against baseline in `tests/test_timing_baseline.yaml`
3. **Warning mode (default)**: Reports deviations but passes
4. **Strict mode** (`--strict-timing` flag): Fails if any test exceeds tolerance

## Modes

### Warning Mode (Default)
```bash
make test
```
- Reports timing violations without failing
- Useful for local development
- Allows for machine/environment variability

### Strict Mode (CI)
```bash
pytest --strict-timing
# or with make:
make test -- --strict-timing
```
- Fails tests that exceed timing tolerance
- Recommended for CI environments with consistent hardware
- Catches performance regressions early

## Baseline File Format

```yaml
test_durations:
  test_wingman_smoke_launch: 0
  test_level1_static_screenshot: 17
  test_respawn_detection_positive: 13
  test_respawn_detection_negative: 21
  test_level2_live_capture: 6
  test_level3_unit_ocr: 14
  test_level4_region33_contains_lick_to_c: 2

tolerance: 5  # seconds
```

## Configuration via pytest.ini

To enable strict timing by default in CI environments, add to `pytest.ini`:

```ini
[pytest]
addopts = --strict-timing
```

Or enable only on specific environments:

### All Tests Within Tolerance
```
============================================================
[Level 5] Performance Validation
============================================================
Mode: WARNING (reports only)
Tolerance: ±5s
------------------------------------------------------------
✓ OK         test_wingman_smoke_launch                     Expected:   0.0s  Actual:   0.0s  Δ  +0.0s
✓ OK         test_level1_static_screenshot                 Expected:  17.0s  Actual:  16.8s  Δ  -0.2s
✓ OK         test_level3_unit_ocr                          Expected:  14.0s  Actual:  13.5s  Δ  -0.5s
------------------------------------------------------------

✓ All 3 tests within expected timing ranges
============================================================
```

### Timing Violations Detected
```
⚠️  VIOLATION test_level3_unit_ocr                          Expected:  14.0s  Actual:  22.3s  Δ  +8.3s

⚠️  1 timing violation(s) detected:
  • test_level3_unit_ocr
    Expected: 14.0s ± 5s
    Actual:   22.3s
    Deviation: 8.3s over tolerance

💡 Tip: Use pytest --strict-timing to fail on timing violations (useful for CI)
```

## Interpreting Slow Runs

A timing violation does not always indicate a code regression. OCR tests are CPU-bound and sensitive to system load at the time of the run.

**Observed pattern (2026-03-23):** All OCR tests ran 4–5× slower than baseline in a single session (`test_respawn_detection_positive`: 12s baseline → 60s actual). Every test was affected uniformly. Re-running immediately returned to nominal times.

**Rule of thumb:** if *all* OCR tests are slow in the same run and they pass on a re-run, the cause is CPU load (background processes, antivirus scan, OS update, thermal throttling), not a code change. A genuine regression typically affects one or two specific tests, not the entire suite uniformly.

**Before investigating a timing violation:**
1. Re-run the test in isolation: `uv run pytest tests/test_automated_levels.py -k <test_name>`
2. Check CPU load during the run (Task Manager / `htop`)
3. Only treat it as a regression if it reproduces consistently on a lightly loaded machine

## Updating the Baseline

When test performance changes legitimately (e.g., optimization or hardware upgrades):

1. Run tests normally: `make test`
2. Extract durations from Level 5 output or `tests/test-output/report.html`
3. Update `tests/test_timing_baseline.yaml` with new expected values
4. Commit the updated baseline

## Use Cases

- **Performance Regression Detection**: Catch slowdowns during development
- **CI/CD Validation**: Strict mode in CI ensures consistent performance
- **Hardware Comparison**: Verify tests run within acceptable ranges on different machines
- **Optimization Tracking**: Measure improvements after performance work

## Integration with Test Levels

This follows the automated test level progression:
- **Level 1**: Static screenshot analysis
- **Level 2**: Live screen capture
- **Level 3**: Background OCR unit test
- **Level 4**: Region 33 continue-text OCR
- **Level 5**: Performance validation ← validates all previous levels
