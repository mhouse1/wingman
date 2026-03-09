# MetalStorm Wingman

Prototype automation assistant for MetalStorm (PC) with mission hotkeys, respawn handling, and OCR-based incoming missile detection.

## What This Project Does

- Captures a game region from your screen.
- Runs OCR-based analysis for:
	- `RESPAWN` detection
	- `INCOMING`/`MING` missile warning detection
- Executes scripted mission actions with keyboard hotkeys.
- Auto-deploys flares when incoming text is detected.
- Supports mission restart flow after respawn.

## Current Status

This is an active prototype. Controls, timings, and OCR behavior are still being tuned.

## Requirements

- Windows 10/11
- Python `>=3.10`
- Game running in a predictable display layout
- Astral `uv` package manager (recommended)

## Quick Start (Windows)

1. Install `uv` if needed:

```powershell
pipx install uv
```

2. From the repository root, sync dependencies:

```powershell
uv sync --all-groups
```

3. Launch Wingman:

- Easiest: double-click `wingman.bat`
- Or run manually:

```powershell
uv run python -m wingman.main --log-level DEBUG
```

## Runtime Hotkeys

Default hotkeys are defined in `wingman/controller.py`.

- `u`: Start J20 mission
- `y`: Start loiter mission
- `end`: Cancel current mission
- `backspace`: Exit script
- `x`: Toggle weapon loop
- `v`: Capture screenshot with grid overlay (saved to `tests/test-output`)
- `b`: Simulate respawn detection (testing)

## Configuration

Main config file: `wingman/config.yaml`

Important settings:

- `loop_interval_sec`: main loop cadence
- `region.left/top/width/height`: capture region
- `region.monitor`: monitor index
- `respawn_detection.grid_size`: OCR grid size
- `respawn_detection.region`: region index for `RESPAWN`
- `respawn_detection.incoming_region`: region index for `INCOMING`
- `respawn_detection.ocr_cooldown`: OCR scheduling interval
- `mission.restart_delay_after_unlock`: delay before mission restart after respawn
- `mission.weapon_loop_interval`: firing loop interval

If detection is unstable, verify the capture region and grid indices first.

## Testing

Common commands:

```bash
make test
make test1
make test2
make test-perf
```

Direct pytest example:

```bash
uv run pytest tests/test_automated_levels.py --html=tests/test-output/report.html --self-contained-html
```

## Performance and OCR Docs

- `docs/performance/`
- `docs/dual-region-ocr-architecture.md`
- `docs/how-to-test-analyzer.md`
- `docs/job-aid-enable-gpu-ocr.md`

## Troubleshooting

- If hotkeys do not respond, run terminal/editor with sufficient keyboard hook permissions.
- If OCR is slow, use `--log-level DEBUG` and check `Analyzer: Parallel OCR Timings` in logs.
- If no detection occurs, validate capture region and incoming/respawn grid regions in `config.yaml`.
- If launcher fails to find `uv`, use manual command: `uv run python -m wingman.main`.

## Safety Notes

- Use responsibly and follow the game terms/policies applicable to your account.
- This project sends keyboard/mouse inputs automatically; test in controlled scenarios first.

## Contributing

Please see `CONTRIBUTING.md` for development workflow, testing expectations, and PR guidance.