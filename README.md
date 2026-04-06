# MetalStorm Wingman

Game automation assistant for MetalStorm (PC). Runs fully unattended across multiple matches — launches missions, detects threats, deploys flares, handles respawns, and restarts the next match automatically.

**Current version:** v1.6.0 | **Phase:** 1-2 (Automation + Text-Based Perception)

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
| "Click to Continue" auto-click | ✅ Working |
| Event refresh popup auto-dismiss | ✅ Working |
| Game state machine (LOBBY / STARTING / BATTLE / END) | ✅ Working |
| Game-starting stall detection + recovery | ✅ Working |
| Padlock camera loop + weapon fire loop | ✅ Working |
| CPU-only OCR (no GPU required) | ✅ Working |

---

## How It Works

Wingman captures a region of the screen on each loop tick and runs EasyOCR on named crop regions to detect game state. Three OCR threads run in parallel on a background thread pool — the main mission loop is never blocked.

### Screen region system (v1.6.0)

Crops are defined by name in `config.yaml` as percentage coordinates of the capture frame:

```yaml
# [[x1_pct, y1_pct], [x2_pct, y2_pct]] — fractions of frame width/height (0.0–1.0)
crops:
  respawn:   [[0.44, 0.55], [0.62, 0.70]]   # "RESPAWN" → cancel + restart
  incoming:  [[0.00, 0.06], [0.22, 0.19]]   # "INCOMING" → deploy flares
  click_to:  [[0.28, 0.72], [0.72, 0.84]]   # "Click to Continue" → click play
  good_luck: [[0.24, 0.38], [0.76, 0.54]]   # "Good Luck" → launch mission
  ...
```

Crops are scale-independent within a stable capture region. Adding or adjusting a region requires only two coordinates — no grid arithmetic. See [ADR 023](docs/adr/023-percentage-coordinate-crop-regions.md) for the full design.

**OCR performance (CPU-only):** avg ~3.25s/cycle. Enabling GPU (CUDA) drops this to <200ms — see [GPU setup guide](docs/TODO-enable-gpu-ocr.md).

---

## Getting Started

See [docs/job-aids/job-aid-setup-and-usage.md](docs/job-aids/job-aid-setup-and-usage.md) for requirements, installation, hotkey reference, configuration options, and troubleshooting.

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
| `x` | Toggle weapon loop |
| `v` | Save debug screenshot with crop overlays |

---

## Calibrating Crop Regions

If the capture window moves or a crop needs adjustment, recalibrate offline using static reference screenshots — no live game required.

```bash
# Recalibrate a single crop
python tests/calibrate.py --crop respawn

# Full calibration loop
python tests/calibrate.py
```

See [docs/job-aids/job-aid-calibrate-crop-regions.md](docs/job-aids/job-aid-calibrate-crop-regions.md) for the full calibration workflow.

---

## Where It's Going

| Phase | Goal | Status |
|-------|------|--------|
| 1-2 | Automation + text perception (current) | ✅ Done |
| 2 (remainder) | Named crop regions + offline calibration tooling | 🔄 In progress |
| 2 (next) | Health, ammo, enemy distance detection | Planned |
| 3 | Behaviour trees — adaptive tactics based on game state | Planned |
| 4 | Reinforcement learning — bot learns from experience | Future |
| 5-6 | Deep RL + vision, multi-agent swarm tactics | Research |

See [docs/PROJECT_AI_ROADMAP.md](docs/PROJECT_AI_ROADMAP.md) for the full roadmap.

---

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for development workflow, testing expectations, and PR guidance.
