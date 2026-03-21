# MetalStorm Wingman

Game automation assistant for MetalStorm (PC). Runs fully unattended across multiple matches — launches missions, detects threats, deploys flares, handles respawns, and restarts the next match automatically.

**Current version:** v1.5.1 | **Phase:** 1-2 (Automation + Text-Based Perception)

---

## What It Does Today

Once started with a single keypress (`m`), Wingman runs the full match loop without user input:

```
Press M
  → Click play button
  → Wait for "Good Luck" → launch J20 mission
  → Detect INCOMING missile → deploy flares automatically
  → Detect RESPAWN → cancel mission → restart after 4s
  → Detect match end → click play → loop
```

### Current Capabilities

| Feature | Status |
|---------|--------|
| J20 and Loiter missions | ✅ Working |
| Full unattended match loop (M key) | ✅ Working |
| Respawn detection + auto-restart | ✅ Working |
| Incoming missile detection + auto-flare | ✅ Working |
| "Click to Continue" auto-click | ✅ Working |
| Game state machine (LOBBY / BATTLE / END / STARTING) | ✅ Working |
| Padlock camera loop + weapon fire loop | ✅ Working |

### How It Works

Wingman uses EasyOCR to read four text regions from the screen via an 8×8 grid:

| Region | Detects | Action |
|--------|---------|--------|
| 44 | `RESPAWN` | Cancel mission → restart after 4s |
| 21 | `INCOMING` | Deploy flares |
| 60 | `CLICK TO CONTINUE` | Auto-click play button |
| 16 | `GOOD LUCK` | Launch J20 after 10s delay |

Three OCR threads run in parallel on a background thread pool. The main mission loop is never blocked.

**Current OCR performance (CPU-only):** avg 3.25s/cycle, worst case 4.6s.
Enabling GPU (CUDA) drops this to <200ms — see [GPU setup guide](docs/TODO-enable-gpu-ocr.md).

---

## Where It's Going

Wingman is an evolving prototype. The current text-based perception is Phase 1-2. Future phases add visual game state awareness and adaptive tactics:

| Phase | Goal | Status |
|-------|------|--------|
| 1-2 | Automation + text perception (respawn, incoming, state machine) | ✅ Done |
| 2 (remainder) | Health, ammo, enemy distance detection | Planned |
| 3 | Behavior trees — adaptive tactics based on game state | Planned |
| 4 | Reinforcement learning — bot learns from experience | Future |
| 5-6 | Deep RL + vision, multi-agent swarm tactics | Research |

See [docs/PROJECT_AI_ROADMAP.md](docs/PROJECT_AI_ROADMAP.md) for the full roadmap with implementation details and cost-benefit analysis.

---

## Getting Started

See [docs/job-aids/job-aid-setup-and-usage.md](docs/job-aids/job-aid-setup-and-usage.md) for:
- Requirements and installation
- Quick start steps
- Full hotkey reference
- Configuration options
- Testing commands
- Troubleshooting

---

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for development workflow, testing expectations, and PR guidance.
