# Wingman — Architecture

| Status | Date       | Wingman Version |
|--------|------------|-----------------|
| Active | 2026-05-11 | 1.6.6           |

## Overview

Wingman is a game automation assistant for MetalStorm. It captures a live screen region, runs EasyOCR-based perception to detect game events, and issues keyboard and mouse inputs to execute flight missions without human input.

The design goal is a **non-blocking main loop**: perception is always asynchronous, the main thread never waits on OCR, and hotkeys remain responsive regardless of what the OCR pipeline is doing.

---

## Component Map

```mermaid
flowchart LR
    subgraph main ["main.py — Orchestration"]
        C["capture.py\nCapture"] --> A["analyzer.py\nGameStateAnalyzer"]
        A --> Ctrl["controller.py\nController"]
        A --> P["performance.py\nPerformanceTracker"]
        P --> main.py
    end
```

| Module | Responsibility |
|---|---|
| `capture.py` | Screen region capture via `mss`. No logic. |
| `analyzer.py` | Perception: OCR detection, FSM ownership, result caches. No input. |
| `controller.py` | Actuation: keyboard/mouse, missions, hotkeys. No perception. |
| `main.py` | Orchestration: main loop, respawn recovery, unattended mode. |
| `performance.py` | Runtime performance tracking: per-crop OCR timing, reaction latency, regression comparison. |

---

## Module Detail

### `Capture`

Owns a single `mss` context created at startup. `get_frame()` grabs the configured region on the configured monitor and returns a BGR `numpy` array, or `None` if the grab fails. Callers must check for `None`.

Must be called from the same thread that constructed the instance — `mss` uses thread-local storage internally. Daemon threads in `controller.py` that need a frame create their own short-lived `mss()` contexts rather than calling `get_frame()`.

---

### `GameStateAnalyzer`

The perception engine and FSM owner. Runs two persistent background threads: a **lobby quick-scan thread** (active in `GAME_LOBBY` / `GAME_WAITING`) and a **background OCR thread** (active in `GAME_BATTLE` / `GAME_BATTLE_MANUAL`). The main loop reads from result caches synchronously without blocking.

**Game state machine** — owned by `GameStateAnalyzer` via the `transitions` library. Trigger methods (`play_clicked`, `cancel_detected`, …) are injected onto the instance by `Machine.__init__`. All callers use `_trigger()` for thread-safe dispatch under `_state_lock`.

See [FSM section](#game-state-machine) below for states and transitions.

**Named crop regions** — OCR targets are defined in `config.yaml` as named entries with percentage coordinates of the capture frame. At runtime they are resolved to pixel `CropCoords` and stored in `analyzer.crops`. The set of crops scanned on each frame is gated by the current FSM state (see `_STATE_CROPS` in `analyzer.py`).

**Result caches** — background OCR workers write to thread-safe caches; the main loop reads them without blocking:

| Cache / Event | Signal | Used for |
|---|---|---|
| `_last_result` | Full `analyze_frame` dict | All per-frame state reads |
| `alive_event` | `threading.Event` — set on health dead→alive | Immediate mission restart |
| `incoming_event` | `threading.Event` — set when new incoming OCR result written | Wake main loop for flare deploy |
| `_easyocr_*` per crop | Per-crop OCR text result | Queried by `analyze_frame` |

**OCR pipeline** — per crop, when cache is expired:

```mermaid
flowchart TD
    F["Full Frame (BGR numpy)"]
    F --> E["Extract named crop region\n(percentage coords → pixel slice)"]
    E --> P["Preprocessing variants\n(gray, binary, upscale, invert)"]
    P --> OCR["EasyOCR → Levenshtein match → cache write"]
```

Multiple crops are submitted to the `ThreadPoolExecutor` (13 workers) and run in parallel. Only crops relevant to the current FSM state are submitted. See [ADR 023](adr/023-percentage-coordinate-crop-regions.md).

**Thread-local readers** — each pool thread owns its own `EasyOCR` reader, initialized once on first use behind a serialization lock (prevents model-download races on first run). Always runs on CPU (`use_gpu: false` in config). See [ADR 020](adr/020-cpu-only-ocr-optimizations.md).

**Health ceiling filter** — `_apply_health_ceiling_filter` maintains a rolling window of recent health readings and rejects any value more than `HEALTH_SPIKE_FACTOR` (1.5×) above the established ceiling. Window size is 10 readings. On `GAME_BATTLE` entry or a False→True alive transition (respawn), the window and ceiling are cleared so the first post-respawn reading sets a fresh baseline. See [ADR 030](adr/030-health-ceiling-from-repeated-readings.md).

**Lock safety** — `_background_ocr_lock` is acquired with a 5-second timeout on the main-loop path. If a background thread stalls holding the lock, the main loop skips the frame rather than blocking. See [ADR 022](adr/022-concurrency-safety-patterns.md).

---

### `Controller`

The actuation layer. Holds the mission lock, fires keys/clicks, manages the game-starting loop, and registers all keyboard hotkeys. `cleanup()` calls `keyboard_module.unhook_all()` on shutdown.

**Key state:**

| Field | Purpose |
|---|---|
| `_mission_lock` | Mutex: only one mission runs at a time |
| `_mission_cancel` | Event: set to cooperatively stop a running mission |
| `_auto_respawn_restart` | Bool: cleared by `End` key or maneuver key press; restored when a mission starts |
| `_eject_stop` | Event: set by `End` key or respawn detection to abort `eject_and_dive` early |
| `_game_battle_since` | Timestamp of last `GAME_BATTLE` entry; 2s maneuver-key grace period suppresses false manual-takeover triggers at mission start |
| `_last_mission` | String (`"j20"` / `"loiter"`): used by `restart_last_mission()` |
| `_target_painting_mode` | Bool: when True, J20 mission includes target-lock painting phase |

**Mission execution** (`mission_j20`):

```
acquire _mission_lock
nose_up (2s)
    → start padlock loop (background daemon, every 6s)
    → start weapon fire loop (background daemon, every 1s)
    → afterburner cycles + roll maneuvers (loop)
    → checks _mission_cancel at each step
release _mission_lock (in finally, guarded with if locked(): release())
```

**Eject and dive** (`eject_and_dive`):

Runs as a daemon thread when missiles are empty or respawn is forced. Holds `NOSE_DOWN + AFTERBURNER` for 10s, then holds afterburner until respawn is detected or 120s elapses. Cancelled early by `_eject_stop` (set by `End` key or respawn OCR detection). Injected key presses carry `is_injected=True` so the keyboard listener skips them — they are not treated as manual maneuver key presses.

**Game-starting loop** (`_start_game_starting_loop`):

Runs as a daemon thread, started by `on_enter_GAME_STARTING`. Polls for "Good Luck" and fires `good_luck_detected` when found. After 10s, arms the health-scan fallback: if `game_battle_alive` becomes True (health OCR detects the aircraft is alive) before Good Luck appears, fires `good_luck_detected` immediately. On 120s timeout, fires `starting_timeout` → `GAME_STARTING_STALLED`. See [ADR 032](adr/032-game-battle-alive-fallback-trigger.md).

```
while state == GAME_STARTING:
    press J20 key
    scan for event-refresh popup → dismiss if found
    scan good_luck crop via OCR
    if detected → wait 13s → launch mission_j20() → fire good_luck_detected trigger
    if 10s elapsed → arm health-scan fallback
    if game_battle_alive (fallback) → launch mission_j20() → fire good_luck_detected
    else wait 5s

on 120s timeout → fire starting_timeout trigger → GAME_STARTING_STALLED
```

**Manual takeover (GAME_BATTLE_MANUAL):**

Any registered maneuver key pressed during `GAME_BATTLE` (outside the 2s entry grace period, and not injected by the bot) fires `manual_takeover` → `GAME_BATTLE_MANUAL`. In this state the bot does not restart missions automatically. Pressing `End` or detecting respawn returns to `GAME_BATTLE` via `respawn_reset` or transitions to `GAME_LOBBY` via `continue_clicked`.

---

### `PerformanceTracker`

Implemented in `performance.py`. Collects per-crop OCR timing and incoming → flare reaction latency during live sessions and emits two outputs:

1. **Round-end histogram** — logged at each `GAME_LOBBY` entry (buffer-check: skips if no battle data). Shows per-crop bucket distribution plus mean and p95.
2. **Session-end comparison** — logged after clean `ThreadPoolExecutor` shutdown. Writes a JSON run file to `docs/performance/current/`, then emits:
   - Block 1: this session vs accumulated current-period aggregate (always)
   - Block 2: current-period aggregate vs release baseline (gated on 5 sessions + 1,000 incoming cycles)

**Wiring:**

| Call site | Method | Thread |
|-----------|--------|--------|
| `analyzer._run_ocr_in_background()` after each `future.result()` | `record_ocr_crop(crop, seconds)` | Background OCR thread |
| `main._deploy_flares_on_new_incoming()` before flare burst | `record_reaction(seconds)` | Main thread |
| `main` GAME_LOBBY transition block | `on_enter_game_lobby()` | Main thread |
| `analyzer.cleanup()` after executor shutdown | `on_session_end()` | Main thread |

**Thread safety:** a single `threading.Lock` guards all buffers. `record_ocr_crop` (background thread) uses bare `with lock`. `record_reaction` and `on_enter_game_lobby` (main thread) use `acquire(timeout=0.1)` and skip gracefully on timeout per the lock-on-main-loop pattern in [CLAUDE.md](../CLAUDE.md).

**Folder layout:**

```
docs/performance/
  current/   ← gitignored; one JSON per clean session, accumulates between releases
  release/   ← committed; replaced by make wrelease (copies all of current/ here)
```

See [ADR 031](adr/031-round-end-histogram-reporting.md) and [Job Aid 008](job-aids/008-performance-regression-workflow.md).

---

### `main.py` — Orchestration

The main loop runs at `loop_interval_sec` (default 1.5s), blocking on `incoming_event` between ticks so flare deployment wakes immediately on new OCR data. Each iteration:

1. Capture frame — skip cycle if `None`
2. Call `analyzer.analyze_frame()` — returns cached state immediately
3. Detect FSM state transition → run state-entry side effects (cancel mission on `GAME_LOBBY` entry, `tracker.on_enter_game_lobby()` for round histogram, auto-trigger `start_auto_mission` in unattended mode)
4. Run per-state timed checks:
   - `GAME_LOBBY`: retry PLAY scan every 5s; stall guard (10s) force-clicks PLAY if OCR keeps failing
   - `GAME_WAITING`: scan for CANCEL every 3s; re-click PLAY only if PLAY is actually visible again; 180s timeout → `waiting_timeout`
   - `GAME_END_B`: stall guard (30s) fires `manual_reset` if click-through gets stuck
5. Check `alive_event` → call `_handle_alive_transition()` for immediate mission restart
6. Check ammo events → eject-and-dive if missiles empty
7. Check respawn detection → drive `RespawnState` sub-machine
8. Check for low flares → log warning
9. Check enemy proximity → `disengage_roll_right` after 30s with no enemy detection

---

## Game State Machine

Seven states managed by the `transitions` library inside `GameStateAnalyzer`. All trigger calls go through `_trigger()` which holds `_state_lock`. `ignore_invalid_triggers=False` — invalid transitions raise `MachineError` immediately. See [ADR 025](adr/025-formalise-game-state-machine.md).

```mermaid
stateDiagram-v2
    [*] --> GAME_LOBBY : startup

    GAME_LOBBY --> GAME_WAITING : play_clicked
    GAME_LOBBY --> GAME_STARTING : cancel_detected

    GAME_WAITING --> GAME_STARTING : cancel_detected
    GAME_WAITING --> GAME_LOBBY : waiting_timeout

    GAME_STARTING --> GAME_BATTLE : good_luck_detected
    GAME_STARTING --> GAME_STARTING_STALLED : starting_timeout

    GAME_STARTING_STALLED --> GAME_STARTING : starting_recovery
    GAME_STARTING_STALLED --> GAME_LOBBY : starting_give_up

    GAME_BATTLE --> GAME_END_B : click_to_detected
    GAME_BATTLE --> GAME_BATTLE_MANUAL : manual_takeover

    GAME_BATTLE_MANUAL --> GAME_BATTLE : respawn_reset
    GAME_BATTLE_MANUAL --> GAME_END_B : click_to_detected
    GAME_BATTLE_MANUAL --> GAME_LOBBY : continue_clicked

    GAME_END_B --> GAME_LOBBY : continue_clicked
    GAME_END_B --> GAME_BATTLE : respawn_detected

    GAME_LOBBY --> GAME_LOBBY : manual_reset (from any state)
```

**FSM callbacks wired by `main.py`:**

| Hook | Action |
|---|---|
| `on_enter_GAME_LOBBY` | `ctrl.cancel_mission()`; clear health window and ceiling; `tracker.on_enter_game_lobby()` (round histogram if buffer non-empty) |
| `on_enter_GAME_STARTING` | `ctrl._start_game_starting_loop()` |
| `on_enter_GAME_BATTLE` | Clear health window + ceiling; set `alive_event` if health already ≥ 1 |
| `on_enter_GAME_BATTLE_MANUAL` | `ctrl.cancel_mission()`; suppress auto-restart |

**OCR gating by state** — only crops listed here are scanned on each frame:

| State | Crops scanned |
|---|---|
| `GAME_BATTLE` | `respawn`, `incoming`, `click_to`, `HEALTH`, `AMMO_FLARES`, `AMMO_MISSILE`, `ENEMY_CLOSE_BY` |
| `GAME_BATTLE_MANUAL` | `respawn`, `incoming`, `click_to`, `HEALTH`, `AMMO_FLARES`, `AMMO_MISSILE`, `ENEMY_CLOSE_BY` |
| `GAME_END_B` | `click_to`, `FINAL_CONTINUE` |
| `GAME_LOBBY` | `PLAY`, `READY`, `UNREADY`, `CANCEL`, `CREATION_FAILED`, `INSPECT`, `INVITED`, `REVEAL_ALL`, `TAP_HERE_TO_CONTINUE`, `UNLOCK_CLOSE`, `FINAL_CONTINUE`, `SILVER` |
| `GAME_WAITING` | `PLAY`, `READY`, `CANCEL` |
| `GAME_STARTING` / `GAME_STARTING_STALLED` | `good_luck` |

---

## Respawn Recovery State Machine

A secondary state machine in `main.py`, independent of the FSM. Handles the respawn screen appearing and disappearing.

```mermaid
stateDiagram-v2
    [*] --> IDLE

    IDLE --> RESPAWNING : respawn OCR detected\n(cancel mission, set fallback timer)
    RESPAWNING --> IDLE : fallback timeout elapsed\n(restart_last_mission attempted)
    RESPAWNING --> PENDING_RESTART : respawn screen clears\n(restart_not_before = now + 4s)
    PENDING_RESTART --> IDLE : 4s delay elapsed + lock free\n(restart_last_mission → success)
```

Note: health dead→alive transitions bypass this machine and restart immediately via `alive_event` + `_handle_alive_transition()`. See [ADR 011](adr/011-respawn-mission-restart-flowchart.md).

---

## Threading Model

```mermaid
flowchart TD
    MT["Main Thread\nmain.py loop"]
    MT --> TP["ThreadPoolExecutor\n13 workers — parallel OCR per crop"]
    TP --> W0["Worker N: crop OCR\nthread-local EasyOCR reader"]
    MT --> BOCT["Background OCR Thread\ndaemon — continuous perception in GAME_BATTLE / BATTLE_MANUAL"]
    MT --> LQST["Lobby Quick-Scan Thread\ndaemon — GAME_LOBBY / GAME_WAITING popup + play detection"]
    MT --> GST["Game-Starting Loop Thread\ndaemon — active during GAME_STARTING / STALLED"]
    MT --> MRT["Mission Runner Thread\ndaemon — guarded by _mission_lock"]
    MRT --> PLT["Padlock Loop Thread\ndaemon — active during mission"]
    MRT --> WFT["Weapon Fire Loop Thread\ndaemon — active during mission"]
    MT --> EDT["Eject-and-Dive Thread\ndaemon — on missiles empty or forced respawn"]
    MT --> FBT["Flare Burst Thread\ndaemon — fire-and-forget on INCOMING"]
    MT --> HLT["Hotkey Listener Thread\nkeyboard library — always running"]
```

All worker threads are `daemon=True`. `analyzer.cleanup()` sets `_background_ocr_stop` and `_lobby_quick_scan_stop` Events, then shuts down the `ThreadPoolExecutor`. `controller.cleanup()` calls `keyboard_module.unhook_all()`. Both are called from the main thread's `finally` block.

Long-running daemon threads are stoppable via `threading.Event` — no bare `while True: time.sleep()` loops. See [CLAUDE.md](../CLAUDE.md) and [ADR 022](adr/022-concurrency-safety-patterns.md).

---

## Unattended Mode

Activated by pressing `M` or setting `unattended_mode: true` in config. When active, `GAME_LOBBY` entry automatically calls `start_auto_mission()`.

```mermaid
flowchart LR
    GB[GAME_BATTLE] -->|click_to_detected| GE[GAME_END_B]
    GE -->|continue_clicked| GL[GAME_LOBBY]
    GL -->|unattended: start_auto_mission| GW[GAME_WAITING]
    GW -->|cancel_detected| GS[GAME_STARTING]
    GS -->|good_luck_detected + 13s| GB
```

---

## Configuration

All tunable values live in `wingman/config.yaml`. Key bindings are module-level constants in `controller.py`.

| Section | Controls |
|---|---|
| `region` | Capture area (left, top, width, height) and monitor index |
| `crops` | Named OCR regions as percentage coordinates; recalibrate with `make calibrate` |
| `unattended_mode` | Enable/disable fully automated play |
| `loop_interval_sec` | Main loop tick rate |
| `mission` | Restart delays, retry intervals, respawn fallback timeout |
| `ocr` | `use_gpu` flag, cooldown, preprocessing parameters |
| `j20_mission` | `target_painting_mode` flag |
| `performance` | Runtime tracking: `enabled`, `output_dir`, regression `min_sessions`, `min_cycles`, `threshold_pct` |

---

## Key Flows

### Startup

```
load config
init Capture (mss context)
init PerformanceTracker (loads config, sets session_start timestamp)
init GameStateAnalyzer (FSM starts at GAME_LOBBY; pre-warm 13 OCR workers; tracker injected)
init Controller (registers all hotkeys)
if unattended_mode → set unattended_active event
enter main loop → GAME_LOBBY detected → start_auto_mission()
```

### Normal Game Cycle (Unattended)

```
[GAME_LOBBY]
  → cancel_mission() + health window reset  (on_enter_GAME_LOBBY callback)
  → tracker.on_enter_game_lobby() → emit round histogram if battle data buffered
  → lobby quick-scan thread: detects PLAY/READY → click play → play_clicked → GAME_WAITING

[GAME_WAITING]
  → scan for CANCEL every 3s
  → CANCEL visible → fire cancel_detected → GAME_STARTING

[GAME_STARTING]
  → _start_game_starting_loop() thread starts
  → press J20 key every 5s
  → scan good_luck crop
  → Good Luck detected → wait 13s → launch mission_j20()
  → fire good_luck_detected → GAME_BATTLE

[GAME_BATTLE]
  → background OCR thread runs continuous parallel OCR
  → padlock + weapon fire + afterburner loops
  → click_to detected → fire click_to_detected → GAME_END_B

[GAME_END_B]
  → _click_through_game_end(): click x7, click PLAY
  → fire continue_clicked → GAME_LOBBY
  → cycle repeats
```

### Missiles Empty

```
AMMO_MISSILE OCR reads 0 in GAME_BATTLE
  → fire cancel_mission()
  → spawn eject_and_dive thread
      → NOSE_DOWN + AFTERBURNER for 10s
      → hold AFTERBURNER until respawn detected or 120s timeout
  → alive_event fires on respawn → _handle_alive_transition() → restart_last_mission()
```

### Manual Takeover

```
Player presses NOSE_UP / NOSE_DOWN / ROLL_LEFT / ROLL_RIGHT during GAME_BATTLE
  (outside 2s grace period, not injected by bot)
  → maneuver_key_pressed handler fires manual_takeover trigger
  → FSM: GAME_BATTLE → GAME_BATTLE_MANUAL
  → on_enter_GAME_BATTLE_MANUAL: cancel_mission(); suppress auto-restart
  → OCR continues (health, ammo, incoming all still monitored)
  → player presses End → cancel_mission(); _auto_respawn_restart = False
  → click_to_detected or continue_clicked returns to GAME_LOBBY
```

---

## ADR Index

| ADR | Decision |
|---|---|
| [001](adr/001-easyocr-for-screen-number-detection.md) | EasyOCR for text detection |
| [002](adr/002-keyboard-library-for-game-input.md) | `keyboard` library for game input |
| [003](adr/003-grid-based-screen-scanning-architecture.md) | Original grid-based screen region addressing (superseded by ADR 023) |
| [004](adr/004-background-ocr-threading-for-non-blocking-analysis.md) | Non-blocking OCR via background threading |
| [005](adr/005-multi-instance-architecture-for-android-emulators.md) | Multi-instance architecture |
| [006](adr/006-multi-monitor-screen-selection.md) | Multi-monitor support |
| [007](adr/007-ocr-time-reduction.md) | OCR performance optimizations |
| [008](adr/008-levenshtein-distance-for-ocr-text-matching.md) | Levenshtein distance for fuzzy OCR matching |
| [009](adr/009-sequential-ocr-outperforms-parallel.md) | Sequential vs parallel OCR tradeoffs |
| [010](adr/010-respawn-incoming-ocr-threading-fix.md) | Threading fix for respawn + incoming OCR |
| [011](adr/011-respawn-mission-restart-flowchart.md) | Respawn → restart state machine |
| [012](adr/012-dual-region-ocr-architecture.md) | Single-frame dual-region OCR pipeline |
| [013](adr/013-automated-test-architecture.md) | Test architecture |
| [014](adr/014-mouse-click-via-win32-mouse-event.md) | Win32 `mouse_event` for click injection |
| [015](adr/015-game-state-machine.md) | Original game state machine (superseded by ADR 025) |
| [016](adr/016-ocr-multiprocessing-to-threading-migration.md) | Multiprocessing → threading migration |
| [017](adr/017-ocr-performance-gpu-vs-template-matching.md) | GPU OCR vs template matching |
| [018](adr/018-adb-input-injection-and-remote-control-architecture.md) | ADB input injection for remote control |
| [019](adr/019-incoming-region-subgrid-ocr-optimization.md) | Subgrid crop optimization for incoming OCR |
| [020](adr/020-cpu-only-ocr-optimizations.md) | CPU-only OCR: skip GPU probe, workers=0, thread pool |
| [021](adr/021-ocr-pipeline-design-rationale.md) | OCR pipeline advanced patterns rationale |
| [022](adr/022-concurrency-safety-patterns.md) | Lock release in finally, stoppable threads, lock timeouts |
| [023](adr/023-percentage-coordinate-crop-regions.md) | Named crop regions with percentage coordinates |
| [024](adr/024-phase3-behavior-tree-architecture.md) | Phase 3 behavior tree architecture (Draft) |
| [025](adr/025-formalise-game-state-machine.md) | Formal FSM via `transitions` library |
| [026](adr/026-game-lobby-state-machine-sequence.md) | GAME_LOBBY state machine sequence (superseded by ADR 029) |
| [027](adr/027-j20-target-painting-mode.md) | J20 target painting mode |
| [028](adr/028-enemy-quadrant-detection-and-nose-orientation.md) | Enemy quadrant detection and nose orientation (Draft) |
| [029](adr/029-game-lobby-quick-scan-thread.md) | GAME_LOBBY dedicated quick-scan background thread |
| [030](adr/030-health-ceiling-from-repeated-readings.md) | Health ceiling spike filter from rolling OCR window |
| [031](adr/031-round-end-histogram-reporting.md) | Round-end OCR timing histogram and reaction latency tracking (Accepted) |
| [032](adr/032-game-battle-alive-fallback-trigger.md) | `game_battle_alive` fallback trigger for GAME_STARTING → GAME_BATTLE |
