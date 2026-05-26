# MetalStorm Wingman

AI wingman automation for MetalStorm (PC), built to run unattended mission loops, support live manual takeover, and evolve toward squad-level AI tactics.

Current version: v1.6.10

![GAME_BATTLE with crop overlays](test_screenshots/GAME_AI.png)
![GAME_BATTLE with crop overlays](test_screenshots/GAME_AI2.png)
![GAME_BATTLE with crop overlays](test_screenshots/GAME_AI3.png)

---

## Vision

One pilot. One or more AI wingmen. A coordinated squad.

Wingman is designed to scale from single-instance automation to multi-instance coordination where each agent can hold a role (aggressive, loiter, target-painting, support) and adapt as match state changes.

---

## What It Does Today

Press `m` and Wingman can run the full loop:

1. Lobby detection and PLAY/READY click flow.
2. Matchmaking confirmation in GAME_WAITING (CANCEL + fallback logic).
3. GAME_STARTING handling and transition to GAME_BATTLE.
4. Mission execution (J20/Loiter), incoming response, health/ammo handling.
5. Respawn detection and restart flow.
6. Match-end click-through and return to lobby.

Manual takeover is always available with maneuver keys (`i`, `j`, `k`, `l`), moving into GAME_BATTLE_MANUAL behavior.

### Current Capabilities

| Capability | Status |
|------------|--------|
| Formal FSM with lobby/waiting/starting/battle/manual/end transitions | ✅ |
| Unattended loop trigger (`m`) and mission hotkeys (`u`, `y`) | ✅ |
| Incoming missile detection and flare response | ✅ |
| Health/ammo OCR-driven mission behavior | ✅ |
| Respawn detection with restart controls | ✅ |
| Lobby popup handling and click-through end-state handling | ✅ |
| Offline crop calibration tooling | ✅ |
| Performance tracking and preview/release chart workflows | ✅ |
| Replay integration harness with assertion engine (ADR 037) | ✅ |
| Real screenshot fixtures for full replay PATH1/PATH2 | In Progress |

---

## Why Replay Integration Matters

This project now includes a timed replay integration harness (ADR 037) that validates:

- transition sequence correctness
- transition settle-time budgets
- action-intent traces in replay mode without real OS input

Current replay assets:

- path config: `tests/replay_paths/adr037_paths.yaml`
- smoke integration command: `make y`

`make y` currently runs a temporary smoke lane while full screenshot fixtures for grounded PATH1/PATH2 are being built.

---

## Quick Start

### Setup

```bash
uv sync --all-groups
```

### Run

```bash
make r
make rd
```

- `make r`: run Wingman normally
- `make rd`: run with DEBUG logs in `wingman.log`

### Core Validation Commands

```bash
make test
make tp
make y
```

- `make test`: main automated test report workflow
- `make tp`: test + performance preview artifacts
- `make y`: replay smoke integration gate

---

## Runtime Hotkeys

| Key | Action |
|-----|--------|
| `m` | Start unattended mode |
| `u` | Start J20 mission |
| `y` | Start loiter mission |
| `end` | Cancel active mission |
| `i` / `j` / `k` / `l` | Manual maneuver takeover |
| `x` | Toggle weapon loop |
| `p` | Manual padlock cooldown trigger |
| `v` | Save debug screenshot with crop overlays |
| `b` | Inject simulated respawn OCR result (testing) |
| `backspace` | Exit script |

---

## Calibration and Crop Workflow

```bash
make calibrate
make calibrate-crop CROP=respawn
make add-crops
```

---

## Roadmap and Aspirations

The near-term path is to complete robust replay-path fixture coverage and then move up the AI stack:

- Phase 2 complete: stable automation + OCR perception + FSM + performance tooling
- Phase 3 planned: behavior-tree tactical decisions
- Future: reinforcement learning, deep vision policy learning, multi-agent coordination

Roadmap doc:

- `docs/PROJECT_AI_ROADMAP.md`

ADR and implementation roadmap references:

- `docs/adr/037-timed-screenshot-replay-integration-testing.md`
- `docs/workflow/003-adr037-replay-screenshot-roadmap.md`

---

## Documentation Index

- Setup and usage: `docs/job-aids/001-setup-and-usage.md`
- Calibration: `docs/job-aids/006-calibrate-crop-regions.md`
- Performance workflow: `docs/job-aids/008-performance-regression-workflow.md`
- Contribution guide: `CONTRIBUTING.md`
