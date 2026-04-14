# MetalStorm Wingman

Game automation assistant for MetalStorm (PC). Runs fully unattended across multiple matches — launches missions, detects threats, deploys flares, handles respawns, and restarts the next match automatically.

**Current version:** v1.6.3 | **Phase:** 1-2 (Automation + Text-Based Perception)

---

## What It Does

Once started with a single keypress (`m`), Wingman runs the full match loop without user input:

```
Press M
  → Click play button
  → Wait for "Good Luck" → launch J20 mission
  → Detect INCOMING missile → deploy flares automatically
  → Detect RESPAWN → cancel mission → restart after 4s
  → Detect match end → click play → loop
```

### Capabilities

| Feature | Status |
|---------|--------|
| Full unattended match loop (`m` key) | ✅ Working |
| J20 and Loiter missions | ✅ Working |
| Respawn detection + auto-restart | ✅ Working |
| Incoming missile detection + auto-flare | ✅ Working |
| Health detection → mission restart on death | ✅ Working |
| "Click to Continue" auto-click | ✅ Working |
| Event refresh popup auto-dismiss | ✅ Working |
| Lobby popup handling (Reveal All, Tap Here, Unlock Close, Final Continue, Inspect, Invited, Creation Failed) | ✅ Working |
| UNREADY detection → suppress play button click | ✅ Working |
| Game state machine (LOBBY / WAITING / STARTING / BATTLE / END) | ✅ Working |
| Game-starting stall detection + recovery | ✅ Working |
| Padlock camera loop + weapon fire loop | ✅ Working |
| CPU-only OCR (no GPU required) | ✅ Working |

---

## How It Works

Wingman captures a region of the screen on each loop tick and runs EasyOCR on named crop regions to detect game state. Three OCR threads run in parallel on a background thread pool — the main mission loop is never blocked.

### Screen region system (v1.6.0+)

Crops are defined by name in `config.yaml` as percentage coordinates of the capture frame:

```yaml
# All coords are fractions of frame width/height (0.0–1.0)
crops:
  respawn:              # "RESPAWN" → cancel + restart
    coords: [[0.4188, 0.6978], [0.5292, 0.7233]]
    text: [RESPAWN]
  incoming:             # "INCOMING" → deploy flares
    coords: [[0.4618, 0.2556], [0.5361, 0.2844]]
    text: [MING, ARNING]
  click_to:             # "Click to Continue" → GAME_END_B
    coords: [[0.4097, 0.8956], [0.5687, 0.9289]]
    text: [CLICKTO, LICKTO, CLICK]
  REVEAL_ALL:           # lobby popup → double-click, then wait
    coords: [[0.8132, 0.8633], [0.9375, 0.9011]]
    text: [REVEAL]
  FINAL_CONTINUE:       # post-match continue button
    coords: [[0.8257, 0.9156], [0.9243, 0.9444]]
    text: [CONTINUE]
  ...
```

Crops are scale-independent within a stable capture region. Adding or adjusting a region requires only two coordinates — no grid arithmetic. See [ADR 023](docs/adr/023-percentage-coordinate-crop-regions.md) for the full design.

![GAME_BATTLE crop regions](test_screenshots/GAME_AI.png)

**OCR performance (CPU-only):** avg ~3.25s/cycle. Enabling GPU (CUDA) drops this to <200ms — see [GPU setup guide](docs/TODO-enable-gpu-ocr.md).

---

## Getting Started

See [Job Aid 001 — Setup and Usage](docs/job-aids/001-setup-and-usage.md) for requirements, installation, hotkey reference, configuration options, and troubleshooting.

### Quick start

```powershell
# Install uv if needed
pipx install uv

# Install dependencies
uv sync --all-groups

# Run
make run
```

### Runtime hotkeys

| Key | Action |
|-----|--------|
| `m` | Start unattended mode |
| `u` | Start J20 mission manually |
| `y` | Start loiter mission manually |
| `end` | Cancel current mission |
| `backspace` | Exit |
| `x` | Toggle weapon fire loop |
| `p` | Padlock camera (sets cooldown if pressed manually) |
| `v` | Save debug screenshot with crop overlays |

---

## Calibrating Crop Regions

If the capture window moves or a crop needs adjustment, recalibrate offline using static reference screenshots — no live game required.

```bash
# Recalibrate a single crop
python tests/calibrate.py --crop respawn

# Full calibration loop
python tests/calibrate.py

# Add and calibrate new crops from test_screenshots/to_be_added/
# (filename stem becomes the crop name; calibrated images are moved to test_screenshots/)
python tests/calibrate.py --add-new-crops
```

See [Job Aid 006 — Calibrate Crop Regions](docs/job-aids/006-calibrate-crop-regions.md) for the full calibration workflow.

---

## Where It's Going

| Phase | Goal | Status |
|-------|------|--------|
| 1-2 | Automation + text perception | ✅ Done |
| 2 | Named crop regions + offline calibration tooling | ✅ Done |
| 2 | Lobby popup handling (Reveal All, Tap Here, Unlock Close, Inspect, Invited, Creation Failed) | ✅ Done |
| 2 | Health detection + mission restart on death | ✅ Done |
| 2 (next) | Ammo and enemy distance detection | Planned |
| 3 | Behaviour trees — adaptive tactics based on game state | Planned |
| 4 | Reinforcement learning — bot learns from experience | Future |
| 5-6 | Deep RL + vision, multi-agent swarm tactics | Research |

See [docs/PROJECT_AI_ROADMAP.md](docs/PROJECT_AI_ROADMAP.md) for the full roadmap.

---

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for development workflow, testing expectations, and PR guidance.
