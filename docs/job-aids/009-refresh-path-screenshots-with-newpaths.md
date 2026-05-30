# Job Aid 009 - Refresh PATH1 and PATH2 Screenshots with newpaths

| Status   | Date       | Wingman Version |
|----------|------------|-----------------|
| Draft    | 2026-05-28 | 1.6.11          |

## Purpose

Refresh integration-test screenshot fixtures by running Wingman live capture in non-strict collection mode.

Use this when:

- You want to update all PATH1 screenshots from natural gameplay.
- You want to update all PATH2 screenshots from natural gameplay.
- You have changed OCR behavior, crop regions, or runtime state logic and need fresh fixtures.

## Prerequisites

- Wingman environment is configured and runnable.
- Game is running and reachable in the configured monitor/region.
- You are in repository root.

## Commands

Update all PATH1 screenshots:

```bash
make newpaths CAPTURE_PATH=PATH1
```

Update all PATH2 screenshots:

```bash
make newpaths CAPTURE_PATH=PATH2
```

## Expected Behavior

- Capture runs while Wingman plays naturally.
- Screenshots may be captured out of order during live gameplay.
- Files are written to:

```text
test_screenshots/integration_test
```

- Summary output is written to:

```text
tests/test-output/capture_summary_PATH1.json
tests/test-output/capture_summary_PATH2.json
```

## Recommended Validation

After refreshing fixtures, run OCR replay tests:

```bash
make ocr
```

If tests skip, replace remaining placeholder images and rerun.

## Troubleshooting

- If capture appears stuck on a path step, keep Wingman running and continue gameplay until the required visual state/trigger occurs.
- If a screenshot is not updated, inspect the capture summary JSON for that step status and notes.
- If a screenshot quality check fails, ensure the game frame is visible (not black) and monitor region settings are correct.
