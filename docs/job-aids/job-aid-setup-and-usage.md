# Job Aid: Setup and Usage

## Requirements

- Windows 10/11
- Python `>=3.10`
- Game running in a predictable display layout
- Astral `uv` package manager (recommended)

---

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

```bash
make run
```

Or if you need debug logging:

```bash
uv run python -m wingman.main --log-level DEBUG
```

---

## Runtime Hotkeys

Default hotkeys are defined in `wingman/controller.py`.

| Key | Action |
|-----|--------|
| `m` | Start unattended mode (clicks play, waits for game start, auto-launches J20) |
| `u` | Start J20 mission manually |
| `y` | Start loiter mission manually |
| `end` | Cancel current mission |
| `backspace` | Exit script |
| `x` | Toggle weapon loop |
| `p` | Padlock camera (sets 10s cooldown on padlock loop) |
| `v` | Capture screenshot with grid overlay (saved to `tests/test-output`) |
| `b` | Simulate respawn detection (testing) |

---

## Configuration

Main config file: `wingman/config.yaml`

| Setting | Description |
|---------|-------------|
| `loop_interval_sec` | Main loop cadence |
| `region.left/top/width/height` | Screen capture region |
| `region.monitor` | Monitor index |
| `respawn_detection.grid_size` | OCR grid size (default 8x8) |
| `respawn_detection.region` | Grid region index for `RESPAWN` detection (default 44) |
| `respawn_detection.incoming_region` | Grid region index for `INCOMING` detection (default 21) |
| `respawn_detection.ocr_cooldown` | OCR scheduling interval |
| `mission.restart_delay_after_unlock` | Delay before mission restart after respawn (default 4s) |
| `mission.weapon_loop_interval` | Firing loop interval |

If detection is unstable, verify the capture region and grid indices first.

---

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

See [how-to-test-analyzer.md](how-to-test-analyzer.md) for analyzer-specific test guidance.

---

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| Hotkeys do not respond | Run terminal/editor with sufficient keyboard hook permissions |
| OCR is slow | Use `--log-level DEBUG` and check `Analyzer: Parallel OCR Timings` in logs; consider enabling GPU (see [TODO-enable-gpu-ocr.md](../TODO-enable-gpu-ocr.md)) |
| No detection occurring | Validate capture region and grid region indices in `config.yaml` |
| Launcher can't find `uv` | Use manual command: `uv run python -m wingman.main` |

---

## Performance and Architecture Docs

- [docs/performance/](../performance/) — OCR timing benchmarks and tracking
- [docs/adr/012-dual-region-ocr-architecture.md](../adr/012-dual-region-ocr-architecture.md) — Dual-region OCR design
- [docs/adr/016-ocr-multiprocessing-to-threading-migration.md](../adr/016-ocr-multiprocessing-to-threading-migration.md) — Threading migration (v1.5.0)
- [docs/TODO-enable-gpu-ocr.md](../TODO-enable-gpu-ocr.md) — GPU enablement guide

---

## Safety Notes

- Use responsibly and follow the game terms/policies applicable to your account.
- This project sends keyboard/mouse inputs automatically; test in controlled scenarios first.
