# Wingman — Architecture

## Overview

Wingman is a game automation assistant for MetalStorm. It captures a live screen region, runs OCR-based perception to detect game events (respawn, incoming missiles, end-of-match prompts), and issues keyboard and mouse inputs to execute flight missions without human input.

The design goal is a **non-blocking main loop**: perception is always asynchronous, the main thread never waits on OCR, and hotkeys remain responsive regardless of what the OCR pipeline is doing.

---

## Component Map

```mermaid
flowchart LR
    subgraph main ["main.py — Orchestration & State Machine"]
        C["capture.py\nCapture"] --> A["analyzer.py\nGameStateAnalyzer"]
        A --> Ctrl["controller.py\nController"]
    end
```

| Module | Responsibility |
|---|---|
| `capture.py` | Screen region capture via `mss`. No logic. |
| `analyzer.py` | Perception: OCR detection, game state, caches. No input. |
| `controller.py` | Actuation: keyboard/mouse, missions, hotkeys. No perception. |
| `main.py` | Orchestration: main loop, state transitions, respawn recovery. |

---

## Module Detail

### `Capture`

Owns a single `mss` context created at startup. `get_frame()` grabs the configured region on the configured monitor and returns a BGR `numpy` array, or `None` if the grab fails (monitor disconnected, region out of bounds). Callers must check for `None`.

Must be called from the same thread that constructed the instance — `mss` uses thread-local storage internally. Daemon threads in `controller.py` create their own short-lived `mss()` contexts rather than calling `get_frame()`.

---

### `GameStateAnalyzer`

The perception engine. Receives raw frames from `main.py` and produces structured results. All heavy work runs off the main thread. Implements the context manager protocol (`__enter__` / `__exit__`) so it can be used in a `with` block; `__exit__` calls `cleanup()`.

**Game state flags** (three booleans, computed into a `GameState` enum):

| Flag | Set by | Cleared by |
|---|---|---|
| `_game_starting` | `Controller.start_auto_mission()` | `Controller._set_last_mission()` |
| `_game_lobby` | `Controller.click_grid_region()` (ready button) | `Controller._set_last_mission()` |
| `_game_end_b` | Click-to OCR background thread | `Controller._set_last_mission()` or respawn/incoming detection |

**OCR caches** — three independent thread-safe caches, each written by a background thread and read by the main loop without blocking:

| Cache | Signal | Writer thread | Cooldown |
|---|---|---|---|
| `_ocr_cache` | Respawn (`RESPA` text) | `ThreadPoolExecutor` worker | `ocr_cooldown` (default 0.1s) |
| `_incoming_cache` | Incoming missile (`MING` / `INCOMING` text) | `ThreadPoolExecutor` worker | same |
| `_click_to_cache` | End-of-match prompt (`Click to Continue`) | Dedicated background thread (5s tick) | — |

`incoming_event` (`threading.Event`) is set by the background OCR thread whenever a new incoming result is written. The main loop waits on this event during its sleep interval so flare deployment wakes immediately on detection instead of spinning.

**OCR pipeline** (per frame, when cache is expired):

```mermaid
flowchart TD
    F["Full Frame (BGR numpy)"]
    F --> R["Extract respawn region\ngray + Otsu binary, resize 0.7×"]
    F --> I["Extract incoming region\n4 variants: gray, binary, upscale 1.4×, inverted+upscale"]
    R --> ROCR["EasyOCR → Levenshtein match → respawn cache"]
    I --> IOCR["EasyOCR → text match → incoming cache"]
```

Both extractions come from a single frame capture. Both OCR calls are submitted to a `ThreadPoolExecutor` (2 workers, thread-local `EasyOCR` readers) and run in parallel. See [ADR 012](adr/012-dual-region-ocr-architecture.md).

**Thread-local readers** — each pool thread owns its own `EasyOCR` reader, initialized once on first use behind a serialization lock (prevents model-download races on first run). Always runs on CPU (`use_gpu: false` in config). See [ADR 020](adr/020-cpu-only-ocr-optimizations.md).

**Lock safety** — `_background_ocr_lock` is acquired with a 5-second timeout on the main-loop path. If a background thread stalls holding the lock, the main loop logs a warning and skips the frame rather than blocking. See [ADR 022](adr/022-concurrency-safety-patterns.md).

---

### `Controller`

The actuation layer. Holds the mission lock, fires keys/clicks, and manages the game-starting loop.

**Key state:**

| Field | Purpose |
|---|---|
| `_mission_lock` | Mutex: only one mission runs at a time |
| `_mission_cancel` | Event: set to cooperatively stop a running mission |
| `_mission_complete` | Event: set when a mission finishes normally |
| `_last_mission` | String (`"j20"` / `"loiter"`): used by `restart_last_mission()` |
| `_auto_respawn_restart` | Bool: cleared by `End` key press; restored on mission start |

**Mission execution** (`mission_j20`):

```
nose_up (2s)
    → start padlock loop (background, every 6s)
    → start weapon fire loop (background, every 1s)
    → afterburner (20s)
    → roll_right
    → ... additional maneuvers
    → loops until cancelled or complete
```

All maneuvers check `_mission_cancel` at each step. Loops use interruptible sleeps (10Hz polling). The mission lock is released in a `finally` block using `if locked(): release()` — never `try/except RuntimeError: pass`. See [ADR 022](adr/022-concurrency-safety-patterns.md).

**Game-starting loop** (`_start_game_starting_loop`):

Runs as a daemon thread from `GAME_STARTING` entry until either Good Luck is detected or a 120s timeout fires.

```
while _game_starting:
    press J20 key (MISSION_J20_KEY)
    check region 30 for event-refresh popup → dismiss if found
    submit async OCR scan for "Good Luck" in region 16
    wait up to 5s (breaks early on Good Luck detection)

on Good Luck detected:
    wait 13s
    launch mission_j20()
```

---

### `main.py` — Orchestration

The main loop runs at `loop_interval_sec` (default 1.5s). Each iteration:

1. Capture frame — skip cycle if `None` (monitor error)
2. Call `analyzer.analyze_frame()` — returns immediately with cached state
3. Check for game state transition → handle `GAME_LOBBY` (start auto mission if unattended)
4. Check for new incoming missile result → deploy flares burst (3×, fire-and-forget thread)
5. Check for respawn — drive the `RespawnState` sub-machine
6. Check for click-to-continue → cancel mission, click continue region
7. Block on `analyzer.incoming_event.wait(timeout=remaining)` for the rest of the interval — wakes immediately when background OCR writes a new incoming result

---

## Game State Machine

Four states. Transitions are event-driven, never time-based. See [ADR 015](adr/015-game-state-machine.md).

```mermaid
stateDiagram-v2
    [*] --> GAME_LOBBY : startup (_game_lobby = True by default)
    GAME_LOBBY --> GAME_STARTING : play button clicked (start_auto_mission)
    GAME_STARTING --> GAME_BATTLE : Good Luck detected + 13s wait → mission_j20 launched
    GAME_BATTLE --> GAME_END_B : click-to OCR detects "Click to Continue"
    GAME_END_B --> GAME_LOBBY : continue region clicked
    GAME_END_B --> GAME_BATTLE : respawn or incoming detected (clears _game_end_b)
    GAME_BATTLE --> GAME_LOBBY : (unattended cycle: GAME_END_B → GAME_LOBBY → auto-restart)
```

**OCR gating by state:**

| Scan | `GAME_BATTLE` | `GAME_END_B` | `GAME_LOBBY` | `GAME_STARTING` |
|---|---|---|---|---|
| Respawn OCR | ✅ | ✅ | ❌ | ❌ |
| Incoming OCR | ✅ | ✅ | ❌ | ❌ |
| Click-to OCR | ✅ (5s tick) | ❌ | ❌ | ❌ |

---

## Respawn Recovery State Machine

A secondary state machine runs inside `main.py`, independent of `GameState`.

```mermaid
stateDiagram-v2
    [*] --> IDLE
    IDLE --> RESPAWNING : respawn detected\n(cancel mission, start 20s fallback timer)
    RESPAWNING --> PENDING_RESTART : respawn screen clears\n(restart_not_before = now + 4s)
    RESPAWNING --> IDLE : fallback timeout (20s) elapsed\nrestart_last_mission() attempted
    PENDING_RESTART --> IDLE : 4s delay elapsed + lock free\nrestart_last_mission() → success
```

See [ADR 011](adr/011-respawn-mission-restart-flowchart.md) for the full decision tree.

---

## Threading Model

```mermaid
flowchart TD
    MT["Main Thread\nmain.py loop"]
    MT --> TP["ThreadPoolExecutor\n2 workers"]
    TP --> W0["Worker 0: respawn region OCR\nthread-local EasyOCR reader"]
    TP --> W1["Worker 1: incoming region OCR\nthread-local EasyOCR reader"]
    MT --> CT["Click-to Thread\ndaemon, 5s tick\nstoppable via _click_to_stop event"]
    MT --> GST["Game-Starting Loop Thread\ndaemon, active during GAME_STARTING only"]
    MT --> MRT["Mission Runner Thread\ndaemon, guarded by _mission_lock"]
    MRT --> PLT["Padlock Loop Thread\ndaemon, active during mission"]
    MRT --> WFT["Weapon Fire Loop Thread\ndaemon, active during mission"]
    MT --> FBT["Flare Burst Thread\ndaemon, fire-and-forget on incoming"]
    MT --> HLT["Hotkey Listener Thread\nkeyboard library, always running"]
```

All worker threads are `daemon=True` — they do not prevent interpreter shutdown. `cleanup()` sets `_click_to_stop` to signal the click-to thread, then shuts down the `ThreadPoolExecutor`. `cleanup()` is called from the main thread's `finally` block and is also invoked by `__exit__` when using `GameStateAnalyzer` as a context manager.

---

## Unattended Mode

When `unattended_mode: true` in config (or activated by pressing `M`), the main loop auto-triggers `start_auto_mission()` on every `GAME_LOBBY` state entry. This closes the loop for fully automated play:

```mermaid
flowchart LR
    GB[GAME_BATTLE] -->|click-to detected| GE[GAME_END_B]
    GE -->|continue region clicked| GL[GAME_LOBBY]
    GL -->|unattended: start_auto_mission| GS[GAME_STARTING]
    GS -->|Good Luck + 13s| GB
```

---

## Grid System

The capture region is divided into an N×N grid (default 8×8 = 64 regions, numbered 1–64 row-major). All region references in config and code use these 1-based grid numbers. The grid is the addressing scheme for:

- Identifying which screen area to OCR (respawn region, incoming region, click-to region)
- Clicking UI elements (ready button, continue button)
- Debugging (V key screenshots with grid overlay)

Region extraction formula: `row = (n - 1) // cols`, `col = (n - 1) % cols`.

See [ADR 003](adr/003-grid-based-screen-scanning-architecture.md).

---

## Configuration

All tunable values live in `wingman/config.yaml`. Nothing is hardcoded except key bindings (defined as module-level constants in `controller.py`).

Key config sections:

| Section | Controls |
|---|---|
| `region` | Capture area (left, top, width, height) and monitor index. Calibrated for 1920×1200; recalibrate using the V key screenshot + grid overlay. |
| `unattended_mode` | Enable/disable fully automated play |
| `loop_interval_sec` | Main loop frequency |
| `mission` | Restart delays, retry intervals, respawn fallback timeout |
| `respawn_detection` | Grid size, region numbers, OCR cooldown, subgrid crop parameters, `use_gpu` flag |
| `controls` | Ready button region, good-luck region, event-refresh region |
| `debug` | Grid overlay, screenshot output directory |

---

## Key Flows

### Startup

```
load config
init Capture (mss context)
init GameStateAnalyzer (sets _game_lobby = True)
init Controller (registers hotkeys)
if unattended_mode → set unattended_active event
enter main loop
```

On first loop iteration: `game_state == GAME_LOBBY` → if unattended, `start_auto_mission()` fires immediately.

### Normal Game Cycle (Unattended)

```
GAME_LOBBY detected
  → cancel_mission() (cleanup any stale state)
  → start_auto_mission()
      → sleep 5s (game settle delay)
      → click ready button
      → _game_starting = True
      → _start_game_starting_loop()

[game_starting loop, ~30–90s]
  → press J20 key every 5s
  → OCR scan for "Good Luck" in region 16
  → Good Luck detected → wait 13s → launch mission_j20()
  → _game_starting = False → GAME_BATTLE

[mission running, 5–10min]
  → padlock camera every 6s
  → fire weapon every 1s
  → afterburner, maneuvers...

[game ends]
  → click-to OCR detects "Click to Continue" → GAME_END_B
  → click continue region → _game_lobby = True → GAME_LOBBY
  → (cycle repeats)
```

### Respawn Recovery

```
respawn OCR detects "RESPA"
  → cancel_mission() → wait for lock release (up to 5s)
  → RespawnState = RESPAWNING
  → restart_not_before = now + 20s (fallback)

[respawn screen clears]
  → RespawnState = PENDING_RESTART
  → restart_not_before = now + 4s

[4s elapsed, lock free]
  → restart_last_mission()
  → RespawnState = IDLE
```

---

## ADR Index

| ADR | Decision |
|---|---|
| [001](adr/001-easyocr-for-screen-number-detection.md) | EasyOCR chosen for text detection |
| [002](adr/002-keyboard-library-for-game-input.md) | `keyboard` library for game input |
| [003](adr/003-grid-based-screen-scanning-architecture.md) | Grid-based screen region addressing |
| [004](adr/004-background-ocr-threading-for-non-blocking-analysis.md) | Non-blocking OCR via background threading |
| [005](adr/005-multi-instance-architecture-for-android-emulators.md) | Multi-instance architecture |
| [006](adr/006-multi-monitor-screen-selection.md) | Multi-monitor support |
| [007](adr/007-ocr-time-reduction.md) | OCR performance optimizations |
| [008](adr/008-levenshtein-distance-for-ocr-text-matching.md) | Levenshtein distance for fuzzy OCR matching |
| [009](adr/009-sequential-ocr-outperforms-parallel.md) | Sequential vs parallel OCR tradeoffs |
| [010](adr/010-respawn-incoming-ocr-threading-fix.md) | Threading fix for respawn+incoming OCR |
| [011](adr/011-respawn-mission-restart-flowchart.md) | Respawn → restart state machine |
| [012](adr/012-dual-region-ocr-architecture.md) | Single-frame dual-region OCR pipeline |
| [013](adr/013-automated-test-architecture.md) | Test architecture |
| [014](adr/014-mouse-click-via-win32-mouse-event.md) | Win32 `mouse_event` for click injection |
| [015](adr/015-game-state-machine.md) | Game state machine design |
| [016](adr/016-ocr-multiprocessing-to-threading-migration.md) | Multiprocessing → threading migration |
| [017](adr/017-ocr-performance-gpu-vs-template-matching.md) | GPU OCR vs template matching |
| [018](adr/018-adb-input-injection-and-remote-control-architecture.md) | ADB input injection for remote control |
| [019](adr/019-incoming-region-subgrid-ocr-optimization.md) | Subgrid crop optimization for incoming OCR |
| [020](adr/020-cpu-only-ocr-optimizations.md) | CPU-only OCR: skip GPU probe, workers=0, 2-worker pool |
| [021](adr/021-ocr-pipeline-design-rationale.md) | OCR pipeline advanced patterns rationale |
| [022](adr/022-concurrency-safety-patterns.md) | Concurrency safety: lock release, stoppable threads, lock timeouts |

---

## Known Architectural Debt

No open P1–P2 items. P3 (no production instrumentation) and P5 (no end-to-end tests) are open in the 2026-03-24 review cycle. See [code-review/002-2026-03.md](code-review/002-2026-03.md).
