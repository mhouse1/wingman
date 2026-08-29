# MetalStorm Wingman

AI wingman automation for MetalStorm (PC), built to run unattended mission loops, support live manual takeover, and evolve toward squad-level AI tactics.

Current version: v1.8.7 — runs on **Windows** and **Linux** (GNOME Wayland, Ubuntu 24.04), on **CPU only**, from a low-end laptop to a desktop workstation.

![GAME_BATTLE with crop overlays](test_screenshots/GAME_AI.png)
![GAME_BATTLE with crop overlays](test_screenshots/GAME_AI2.png)
![GAME_BATTLE with crop overlays](test_screenshots/GAME_AI3.png)

---

## Vision

One pilot. One or more AI wingmen. A coordinated squad.

Wingman is designed to scale from single-instance automation to multi-instance coordination where each agent can hold a role (aggressive, loiter, target-painting, support) and adapt as match state changes.

---

## Why This Project Exists

Beyond the game itself, Wingman is an R&D reference architecture for AI-driven automation. MetalStorm is the testbed where patterns get built and proven — OCR-driven state machines, replay-based testing, calibration tooling, performance regression tracking — and those patterns are meant to be reproduced into other projects, not imported as a shared library. Other repos (e.g. `mos-docker/tests/automated`, [dojo](https://github.com/mhouse1/dojo)) already reuse the test-harness architecture developed here.

---

## Project Phases

Wingman evolves through deliberate phases, each raising the AI level of the system. The full plan lives in `docs/PROJECT_AI_ROADMAP.md`.

| Phase | Focus | AI Level | Status |
|-------|-------|----------|--------|
| 1–2 | Unattended automation: OCR perception, formal FSM, mission loops, replay-based validation, performance tracking | Scripted automation | ✅ Complete |
| **3** | **Behavior tree for adaptive tactics: the aircraft chooses what to do from live battlefield state instead of following a fixed script — and the start of multi-instance squad coordination** | **Task planning** | **🎯 In progress (active)** |
| 4 | Reinforcement learning — strategy improves from gameplay outcomes | Learning | Future |
| 5 | Deep RL + vision — policies from raw frames | Research | Future |

**Multi-agent squad coordination is not a distant end-phase — it begins during Phase 3 and continues beyond it.** The behavior tree is what makes coordination practical: each Wingman instance can hold a role (aggressive, loiter, target-painting, support) as a tactic configuration of the same tree, so the first squad work is multiple instances flying complementary roles. Later phases deepen coordination (shared target priority, learned policies) rather than introduce it.

### Where Phase 3 stands

The behavior tree (ADR 024, built on `py_trees`) runs **active** in every session. Each tick it freezes an analyzer snapshot (health, ammo, minimap rings, altitude, respawn state) and a priority selector picks the tactic:

- **Engage** — minimap ring-engage geometry: steer toward contacts, orbit when merged (ADR 024 3.1a, ADR 028)
- **MissileEvade** — evasive manoeuvre on incoming-missile detection (ADR 070); live sessions measure 90% vs 68% ten-second survival with the evade on (n=122 engagements)
- **Climb** — terrain avoidance and closed-loop climb-to-operating-altitude, including the mission-start climb prologue (ADR 073)
- **Eject** — missiles-empty eject-and-dive on the debounced ammo verdict, with impulse rotation and telemetry-verified ballistic descent (ADR 056/069)
- **Disengage / Idle / RespawnWait** — supporting tactics and selection-only states

The J20 mission is being rewritten from a hardcoded maneuver script to this tactic-driven model (v1.8.3): geometry, evasion, climb, and eject decisions all belong to the tree, with the scripted roll sequence retired. A few open-loop pieces (afterburner cadence, the fixed mission window) remain and are candidates for later conversion. New tactics enter through a **shadow-first pipeline** (ADR 073): a candidate tactic first runs selection-only, logging what it *would* do against live data; only after shadow evidence holds up does it get actuation. Per-engagement survival stats (ADR 055/070) close the loop with A/B evidence from unattended soaks.

---

## What It Does Today

Wingman flies MetalStorm unattended for hours at a time. A session is: launch, then walk away — the FSM drives lobby → matchmaking → battle → match end → lobby continuously, with the behavior tree making the in-battle decisions and per-session statistics written on exit.

One full match cycle:

1. Lobby detection, popup dismissal, and PLAY/READY click flow.
2. Matchmaking confirmation in GAME_WAITING (CANCEL + fallback logic).
3. GAME_STARTING handling and transition to GAME_BATTLE.
4. Closed-loop climb to operating altitude, then in-battle tactic selection via
   the active behavior tree: ring-engage navigation, missile evasion, climb,
   disengage, eject (see Phase 3 above).
5. Incoming-missile response: flare bursts plus the MISSILE_EVADE_MODE
   manoeuvre (ADR 070).
6. Respawn detection (dual-sensor, ADR 064) and immediate restart the moment
   health returns.
7. Match-end click-through and return to lobby — then the loop repeats.

Manual takeover is always available with maneuver keys (`i`, `j`, `k`, `l`), moving into GAME_BATTLE_MANUAL behavior. Dying while in manual mode returns control to auto and restarts the mission when health comes back, so the aircraft is never left flying uncommanded.

### Current Capabilities

| Capability | Status |
|------------|--------|
| Formal FSM with lobby/waiting/starting/battle/manual/end transitions | ✅ |
| Fully unattended match loop — hours-long sessions with zero input | ✅ |
| Active behavior-tree tactic selection: Engage geometry, Eject, Disengage, MissileEvade, Climb (ADR 024/070/073) | ✅ |
| Tactic-driven J20 mission — scripted geometry retired, tree owns in-battle decisions (v1.8.3) | ✅ |
| Shadow-first tactic pipeline: selection-only validation before actuation (ADR 073) | ✅ |
| Per-engagement survival metric: 10 s survival split evade vs no-evade (ADR 070 V5) | ✅ |
| Incoming missile detection (template matching + OCR fallback) and flare response | ✅ |
| Health/ammo OCR-driven mission behavior | ✅ |
| Dual-sensor respawn detection: overlay OCR with a health-signal fallback (ADR 064) | ✅ |
| Health-gated immediate mission restart, including after a manual-mode death (ADR 059) | ✅ |
| Missiles-empty eject as a first-class FSM state, with impulse-rotation telemetry-verified dive (ADR 056/069) | ✅ |
| Metric HUD telemetry (altitude/speed/flight-path angle) feeding eject, evade, and climb decisions (ADR 038/067) | ✅ |
| Health OCR value-confirmation filter for degraded-read regimes (ADR 063) | ✅ |
| Per-mission and per-session statistics tracker (ADR 055) | ✅ |
| Lobby popup handling and click-through end-state handling | ✅ |
| Per-concern tick-loop handlers and typed orchestration event registry (ADR 060) | ✅ |
| Live HUD overlay snapshot (health/ammo/state, written off the tick loop) | ✅ |
| HSV target tracking with proportional roll correction | ⚙️ off by default |
| Offline crop calibration tooling | ✅ |
| Performance tracking and preview/release chart workflows | ✅ |
| Layered validation: replay harness, runtime gates, real-OCR lanes (ADR 037/044/045) | ✅ |
| Single gate-corpus screenshot set, refreshed unattended by `make p1` (ADR 071/072) | ✅ |
| StrictDoc-managed requirements with source traceability gates (ADR 066) | ✅ |
| No-stuck-keys guarantee: every injectable key released on any exit path (SAF-007) | ✅ |
| Linux support: auto-launch, PipeWire capture, XTest input injection | ✅ |

---

## Hardware Target — CPU Only, Deliberately

Wingman runs entirely on the **CPU**. `respawn_detection.use_gpu` exists and is
set to `false`, and every calibration, performance baseline and regression
threshold in the project was measured that way.

That is a design decision, not an oversight. The target range is **a low-end
laptop through to a desktop workstation**, with no GPU requirement, because:

- The GPU is already busy running MetalStorm. A wingman that costs the game
  frames has made the aircraft harder to fly, not easier.
- One hardware profile means one set of baselines. The 725-session release
  baseline, the ADR 092 leak gate thresholds and the ADR 090 memory-guard limits
  are all calibrated against CPU behaviour, and a second profile would silently
  split every one of them.
- GPU inference is not bit-identical to CPU, and the ADR 044 replay gate and
  ADR 037 real-OCR lane both assert on OCR output.

The foundation comes first. A high-performance GPU profile — batched GPU OCR, a
per-frame fast path for missile detection, and reaction latency bounded by frame
rate rather than by the 1.5 s tick — is designed in
[`docs/hldd/008-gpu-accelerated-realtime-wingman-hldd.md`](docs/hldd/008-gpu-accelerated-realtime-wingman-hldd.md).
It is a design, not a plan; nothing there is scheduled, and the CPU path would
remain the default.

---

## Runtime Performance

Measured across the release baseline — **725 sessions, 604,263 OCR samples**:

| crop | mean | p95 |
|------|------|-----|
| incoming | 0.460 s | 0.373 s |
| respawn | 0.423 s | 0.228 s |
| health | 0.432 s | 0.380 s |
| ammo_flares | 0.375 s | 0.207 s |
| ammo_missiles | 0.384 s | 0.195 s |
| telemetry | 0.666 s | 0.374 s |
| **incoming to flare** | **0.486 s** | — |

Sampling is on a fixed **1.5 s tick**, with 13 thread-local EasyOCR readers
across 33 calibrated crops.

Every session writes per-crop OCR timings and incoming→flare reaction latency to `docs/performance/current/`, which `make wrelease` promotes into `docs/performance/release/` as the comparison baseline. `PerformanceTracker` fails the run if the current session regresses against that baseline beyond the thresholds in `config.yaml`.

Live artifacts (regenerated by `make tp` / `make tp-full`):

- `docs/performance/runtime-performance-trends.preview.html` — current, including uncommitted sessions
- `docs/performance/runtime-performance-trends.html` — released baselines only

The chart below is a historical snapshot covering v1.6.7 through v1.6.19, captured before the Linux migration; it is kept for the long-run trend, not as current data.

![Runtime performance trend v1.6.7–v1.6.19](docs/performance/run_time_performance_tracking.png)

---

## Quick Start

### Prerequisites

```bash
uv sync --all-groups
```

**Linux only (one-time setup):** See `docs/job-aids/010-run-metalstorm-on-linux.md` for the full checklist. The short version:

1. Install MetalStorm via Heroic Games Launcher (Flatpak) with Proton-GE.
2. Install `umu-run` standalone — Makefile variables `UMU_RUN`, `PROTON_ROOT`, `WINE_PREFIX`, `GAME_EXE` point to your install.
3. Run `make r` once to trigger the one-time PipeWire screen-share dialog; subsequent runs skip it automatically. (Not needed while the nested display lane is enabled — it captures its own X server directly and never uses the portal.)
4. Set MetalStorm's Controls mode to **Controller / Joystick** in-game settings (required for pitch input under Wine — see ADR 051).
5. Configure in-game keybindings as described in `docs/job-aids/011-wingman-keybindings.md`.

No `sudo`, no `input` group membership, no root access required.

### Run

```bash
make r     # run Wingman (INFO console only)
make rd    # run with DEBUG logs written to wingman.log
```

On Linux, `make r` automatically launches MetalStorm via `umu-run` if it is not already running, waits for the lobby to appear, then starts Wingman. No manual game launch step is needed.

On Windows, launch MetalStorm manually before running `make r`.

### Using the computer while Wingman runs

By default (`nested.enabled: true` in `wingman/config.yaml`) the game runs on its
own nested X display. Wingman captures and injects there, so its keystrokes
cannot reach whatever you are typing in — you can use the machine normally while
a session runs. Your hotkeys still work, because hotkey observation deliberately
stays on your own display.

```bash
make rd              # honours nested.enabled
make rd NESTED=0     # force the on-screen lane for one run
make nested-status   # is the lane up, and what holds focus
make nested-stop     # tear the nested server down
```

See ADR 099 and `docs/hldd/009-nested-display-isolation-hldd.md` for the design.

---

## Validation

Layered lanes from fast unit checks to runtime-realistic gates:

```bash
make test               # core pytest suite and HTML report
make tp                 # fast preview bundle: test + ADR044/ADR045 gates + performance previews
make tp-full            # full preview bundle: tp + ADR037 PATH1/PATH2 real-OCR lane
make rr-path1-gate      # ADR044 deterministic runtime replay gate (full wingman.main loop + assertions)
make rr-live-path1-gate # ADR045 live-screen gate (desktop presenter + real monitor capture)
make ocr                # ADR037 real-OCR integration tests (PATH1/PATH2)
```

Run `make tp` before proposing a release; `make tp-full` for the complete pre-release sweep.

---

## Runtime Hotkeys

Wingman is normally fully unattended — hotkeys exist for supervision, testing, and manual takeover.

| Key | Action |
|-----|--------|
| `m` | Activate unattended mode (also auto-enabled from config) |
| `u` | Start J20 mission |
| `y` | Start loiter mission |
| `end` | Cancel active mission |
| `i` / `j` / `k` / `l` | Manual maneuver takeover |
| `x` | Toggle weapon loop |
| `p` | Manual padlock cooldown trigger |
| `v` | Save debug screenshot with crop overlays |
| `b` | Inject simulated respawn OCR result (testing) |
| `backspace` | Exit script (runtime mode) |

Hotkeys work on Linux without root or `input` group membership — key injection uses XTest and hotkey listening uses the X11 RECORD extension (see ADR 053).

Note: automated replay/capture test lanes disable hotkeys to avoid accidental interruption during CI-style runs.

---

## Calibration and Crop Workflow

```bash
make calibrate
make calibrate-crop CROP=respawn
make add-crops
```

Calibration references come from the same gate-corpus screenshots the test
lanes use (ADR 072) — after a game UI update, one unattended `make p1` run
refreshes both the test fixtures and the calibration references.

---

## Documentation Index

Roadmap: `docs/PROJECT_AI_ROADMAP.md` · Architecture: `docs/architecture.md` · Contribution guide: `CONTRIBUTING.md`

### Core ADRs — the architecture of Wingman's logic

| Document | Description |
|---|---|
| `docs/adr/024-phase3-behavior-tree-architecture.md` | **The Phase 3 behavior tree**: tactic selector, snapshot model, actuation cutover |
| `docs/adr/025-formalise-game-state-machine.md` | The formal FSM that drives the match loop |
| `docs/adr/060-tick-loop-handlers-and-typed-event-registry.md` | Tick-loop handler objects and the orchestration event registry |
| `docs/adr/021-ocr-pipeline-design-rationale.md` | Why the OCR perception pipeline is built the way it is |
| `docs/adr/023-percentage-coordinate-crop-regions.md` | Percentage-coordinate crop regions — the perception addressing scheme |
| `docs/adr/028-enemy-quadrant-detection-and-nose-orientation.md` | Minimap enemy bearing — the spatial input behind Engage geometry |

### Tactics and flight logic

| Document | Description |
|---|---|
| `docs/adr/070-missile-evade-tactic.md` | MISSILE_EVADE_MODE tactic (d1–d13, live V5 survival evidence) |
| `docs/adr/073-climb-tactic-shadow-first.md` | Climb tactic and the shadow-first validation pipeline for new tactics |
| `docs/adr/056-game-battle-eject-fsm-state.md` | Eject as a first-class FSM state |
| `docs/adr/069-eject-impulse-rotation-and-ballistic-descent.md` | Eject descent: impulse rotation + ballistic phase |
| `docs/adr/059-health-gated-immediate-mission-restart.md` | One restart path: mission restarts when health returns |

### Design documents (HLDD)

| Document | Description |
|---|---|
| `docs/hldd/008-gpu-accelerated-realtime-wingman-hldd.md` | **A GPU-accelerated real-time profile** — batched GPU OCR, per-frame missile detection, and what must not regress. Design only; the CPU path stays the default |

### Perception and detection

| Document | Description |
|---|---|
| `docs/adr/046-incoming-template-matching-replacement.md` | Incoming-missile detection via template matching with OCR fallback |
| `docs/adr/064-dual-sensor-respawn-detection.md` | Dual-sensor respawn detection (supersedes the rejected ADR 062) |
| `docs/adr/063-health-ocr-value-confirmation-filter.md` | Health OCR value-confirmation filter for degraded-read regimes |
| `docs/adr/067-metric-hud-units-pitch-normalization-recalibration.md` | Metric HUD telemetry units and pitch normalization |

### Validation and requirements

| Document | Description |
|---|---|
| `docs/adr/037-timed-screenshot-replay-integration-testing.md` | Replay integration harness with assertion engine |
| `docs/adr/044-runtime-screenshot-driven-automation-lane.md` | Deterministic runtime replay gate (PATH1) |
| `docs/adr/045-dual-lane-runtime-validation-replay-and-live-screen.md` | Dual-lane runtime validation: replay + live screen |
| `docs/adr/071-single-gate-corpus-screenshot-set.md` | One screenshot corpus for all test lanes |
| `docs/adr/066-strictdoc-requirements-adoption.md` | StrictDoc requirements with source traceability |

### Platform and setup

| Document | Description |
|---|---|
| `docs/job-aids/001-setup-and-usage.md` | Setup and usage |
| `docs/job-aids/006-calibrate-crop-regions.md` | Calibration |
| `docs/job-aids/008-performance-regression-workflow.md` | Performance workflow |
| `docs/job-aids/010-run-metalstorm-on-linux.md` | Linux setup: Heroic, umu-run, PipeWire grant |
| `docs/job-aids/011-wingman-keybindings.md` | In-game keybinding configuration (Linux) |
| `docs/adr/049-linux-migration-game-and-automation-layer.md` | Linux migration decisions and implementation summary |
| `docs/adr/050-wayland-screen-capture.md` | PipeWire screen capture on GNOME Wayland |
| `docs/adr/053-linux-one-command-launch.md` | Full Linux input stack: window detection, XTest, XRecord |
