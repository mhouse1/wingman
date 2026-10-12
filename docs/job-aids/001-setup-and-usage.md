# Job Aid 001 — Setup and Usage

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
| `n` | Start unattended mode (clicks play, waits for game start, auto-launches the mission `mission.default_mission` names: SU-30 as shipped, or `j20` / `jas39` / `f111`) |
| `u` | Start the configured mission (`mission.default_mission`) manually |
| `y` | Start loiter mission manually |
| `o` | Start SU-30 mission manually (takes over from a running mission) |
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
| `mission.default_mission` | Mission launched at battle entry, by `u` and by the no-prior-mission restart: `su30` (shipped), `j20`, `jas39`, `survey` (see Survey Flights below; set with `make survey`) or `f111` (the F-111: the su30 sequence plus the wing sweep on `w`, ADR 149). A respawn restarts whichever ran last (`_last_mission`: also `loiter`) |
| `f111_mission` | F-111 mission numbers: level-off `climb_alt_m`, nose angle, `unsweep_alt_m`, `unsweep_timeout_s` (30 s), `alt_floor_m` (ADR 147 per-mission floor) |

If detection is unstable, verify the capture region and grid indices first.

---

## Survey Flights

A survey flight flies an arena in straight passes so that the recording can be turned into a terrain map
([Design 017](../hldd/017-terrain-map-from-flight-footage-hldd.md); its "Where this stands" section has
the current state). The survey carries no weapons and no defence, so it is only for a match with nobody
else in it.

Setup, as flown on 2026-10-04:

| | |
|---|---|
| Aircraft | MiG-29 |
| Match | A custom 1 on 1 match with "Fill with bots" turned off |
| Mode and map | Team Deathmatch on Crimson Canyon |

Steps:

1. `make survey` switches survey mode on for this machine and prints `Survey mode: ON`. Run it again to go
   back to the normal mission. It only writes the untracked `wingman/config.local.yaml`, and a session
   already running keeps the mission it started with.
2. Set up the match above in the game, then `make r1 v`. The `v` records the session; the recording is the
   survey's footage.
3. Stop with `z`, as for any session. Wingman finishes the round and exits at the lobby.

What to expect while it flies:

- Every 15 s on a pass wingman opens the game's full map (`m`) for about a second to read where the aircraft
  is, and logs `SURVEY POS: east=... north=... r=...`. `m` is the game's own key. If you open the map
  yourself during a survey, wingman closes it again within a second or so, because a map left open hides
  the instruments it flies on.
- Each battle flies its passes 45 degrees further round than the last, and each turn back at the edge moves
  the next pass about a third of the arena's radius over.
- `SURVEY:` lines in `wingman.log` give the state, pass, heading, altitude and distance from the arena's
  centre every five seconds.

After a flight, from the repository root:

```bash
uv run --project scripts/mapping-spike python scripts/mapping-spike/survey_track.py track.png wingman.log
scripts/mapping-spike/survey_swing.sh wingman.log
```

The first draws where the passes went and prints the share of the arena covered. The second prints how much
the altitude swung inside each pass.

The game window may be behind other windows while this runs, provided version 4 of the
`wingman-window-left` desktop extension is loaded (`gnome-extensions info wingman-window-left@wingman.local`
shows `Version: 4`; it loads at login). Without it the game drops to one frame a second when covered.

---

## Testing

Common commands:

```bash
make test
make test1
make test2
```

Direct pytest example:

```bash
uv run pytest tests/test_automated_levels.py --html=tests/test-output/report.html --self-contained-html
```

See [Job Aid 004 — How to Test the Game State Analyzer](004-how-to-test-analyzer.md) for analyzer-specific test guidance.

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
