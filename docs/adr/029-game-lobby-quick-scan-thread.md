# ADR 029 — GAME_LOBBY Quick-Scan Thread (supersedes ADR 026)

| Status   | Date       | Wingman Version |
|----------|------------|-----------------|
| Draft    | 2026-05-04 | 1.6.5           |

## Context

ADR 026 documented the `GAME_LOBBY` sequence as it stood at version 1.6.4: a controller-owned `start_auto_mission()` method that spawned a daemon thread per call, polled the play button every 5 s from the main loop, and applied a 10 s stall guard in the main loop.

That design had two problems that surfaced during FSM stability work:

1. **Race conditions on multiple calls**: each `start_auto_mission()` invocation spawned a new `_run` thread. If the main loop fired another call before the previous thread finished its OCR scan, two threads could both detect PLAY and both fire `play_clicked`, causing a double-click or double FSM trigger.
2. **Main-loop coupling**: the stall guard and 5 s polling were woven into the main `while True` loop, making the lobby logic difficult to test in isolation and adding conditional branches to an already-complex loop body.

## Decision

Replace `start_auto_mission()` and the main-loop lobby guards with a single long-lived daemon thread — `_run_game_lobby_quick_scan` — owned by `GameStateAnalyzer`. The thread:

- Starts on the first frame passed to `analyze_frame()`, before any state-specific logic.
- Runs until `cleanup()` sets `_lobby_quick_scan_stop`.
- Sleeps via `_lobby_quick_scan_stop.wait(timeout=1.0)` — stoppable, not `time.sleep`.
- Skips cycles where `game_state` is not `GAME_LOBBY` or `GAME_WAITING`.

### Scan schedule

| Cycle | Condition | Crops submitted |
|-------|-----------|-----------------|
| Every 1 s | `GAME_LOBBY` | `CANCEL`, `UNREADY`, `PLAY`, `READY` (whichever are configured) |
| Every 5 s | `GAME_LOBBY` or `GAME_WAITING` | `INVITED`, `CREATION_FAILED`, `REVEAL_ALL`, `UNLOCK_CLOSE`, `INSPECT`, `event_refresh` |

All futures for a given cycle are submitted to the shared `ThreadPoolExecutor` in one parallel batch before any result is read. Resolution order within each cycle:

1. `CANCEL` / `UNREADY` — if either is detected, fire `cancel_detected` (→ `GAME_STARTING`) and skip PLAY processing for this cycle.
2. `PLAY` / `READY` — if detected and a 60 s per-play-click cooldown has not fired, call `_on_lobby_play_click(crop)` (→ `ctrl.click_crop`) and fire `play_clicked` (→ `GAME_WAITING`).
3. Popup crops — first detected popup calls `_on_lobby_popup_click(popup)` (→ `ctrl.click_crop`).

The 60 s play-click cooldown (`last_play_click_ts`) prevents the thread from re-firing `play_clicked` if the state has not yet advanced after a successful click.

### Callbacks injected from `main.py`

`GameStateAnalyzer` exposes two injection points that `main.py` populates after both `ctrl` and `analyzer` are constructed:

| Attribute | Set to |
|---|---|
| `analyzer._on_lobby_play_click` | `lambda crop: ctrl.click_crop(analyzer.crops[crop], …)` |
| `analyzer._on_lobby_popup_click` | `_handle_lobby_popup(popup)` closure |

`_handle_lobby_popup` handles popup-specific side-effects:
- `REVEAL_ALL`: clicks once immediately, then spawns a thread to click again after 3 s.
- `INVITED`: spawns a thread to wait 1.5 s, re-capture, then click `PLAY`/`READY` if visible.
- All others: single click with 30 s per-popup cooldown (`popup_click_allowed` / `record_popup_click`).

### `start_auto_mission()` removed

`controller.py` no longer contains `start_auto_mission()`. There is no background thread spawned per lobby entry; all OCR is handled by the single persistent quick-scan thread.

### Main-loop GAME_LOBBY handling

The main loop's GAME_LOBBY section is now minimal. On state transition *into* `GAME_LOBBY`:

```python
if current_game_state == GameState.GAME_LOBBY:
    game_waiting_since = 0.0
    if prev_game_state is not None:
        ctrl.cancel_mission()
```

There is no `game_lobby_since` timer, no 5 s polling call to `start_auto_mission`, and no 10 s stall guard. The quick-scan thread handles all detection and clicking.

### `unattended_active` no longer gates PLAY-clicking

ADR 026 described the play-click as conditional on `unattended_active.is_set()`. The quick-scan thread does not check `unattended_active`; it clicks PLAY whenever detected. `unattended_active` is still set from config (`unattended_mode: true`) and from the M-key handler, but its effect is now limited to enabling other unattended behaviours in the main loop (none currently active in `GAME_LOBBY`).

### CANCEL scan in GAME_WAITING

CANCEL scanning in `GAME_WAITING` remains in the main loop (every 3 s, guarded by `last_cancel_scan_ts`), which also handles the 180 s `waiting_timeout`. The quick-scan thread only runs popup scans (every 5 s) in `GAME_WAITING`; it does not re-scan lobby crops while in that state.

### Entry conditions (updated)

Four triggers reach `GAME_LOBBY` — same as ADR 026 with one addition:

| Trigger | Source state(s) | Cause |
|---------|-----------------|-------|
| `continue_clicked` | `GAME_END_B`, `GAME_BATTLE_MANUAL` | Click-through complete; or manual-takeover session ended |
| `waiting_timeout` | `GAME_WAITING` | 180 s with no CANCEL detected |
| `starting_give_up` | `GAME_STARTING_STALLED` | Stall loop gave up waiting for Good Luck |
| `manual_reset` | any | M key or End key forced recovery |

`GAME_BATTLE_MANUAL` → `GAME_LOBBY` via `continue_clicked` is new; ADR 026 listed only `GAME_END_B` as the source for that trigger.

### Popup crop list (updated)

`TAP_HERE_TO_CONTINUE` and `FINAL_CONTINUE` are no longer in the quick-scan popup list. Both appear in `_STATE_CROPS[GAME_LOBBY]` (for debug overlay), but are not actively clicked by the thread. `FINAL_CONTINUE` in the click-through path is handled by `_click_through_game_end()` in `main.py` under `GAME_END_B`.

Active popup crops scanned in the quick-scan thread:

| Popup crop | Action |
|---|---|
| `INVITED` | Click → wait 1.5 s → re-scan for PLAY/READY → click if found |
| `CREATION_FAILED` | Single click with 30 s cooldown |
| `REVEAL_ALL` | Click → wait 3 s → click again |
| `UNLOCK_CLOSE` | Single click with 30 s cooldown |
| `INSPECT` | Single click with 30 s cooldown |
| `event_refresh` | Click `event_refresh_dismiss` crop |

## Consequences

**Positive**

- Single thread eliminates the concurrent `_run` thread race from `start_auto_mission()`.
- Main loop is simpler — no lobby-specific guard branches.
- Thread lifetime is predictable: started once, stopped by `cleanup()`.
- All lobby OCR is parallel within each 1 s cycle (one executor batch submission).

**Negative / Trade-offs**

- The 1 s scan interval is fixed; the old `start_auto_mission()` could scan immediately on entry (it was called directly from the main loop's first GAME_LOBBY iteration). The quick-scan thread waits up to 1 s before its first scan.
- `unattended_active` is now effectively inert with respect to lobby automation. If a future requirement needs to suppress automated play-clicking (e.g., semi-manual mode), the thread will need a check added.

## Known Issues

### 1. UNREADY fires wrong FSM trigger — advances to `GAME_STARTING` instead of `GAME_WAITING`

**Game context**: in multiplayer mode the PLAY button reads "READY". Clicking it changes the button to "UNREADY" (this player is ready; waiting for others). Once all squad members are ready, the button changes to "WAITING" and CANCEL becomes visible — at which point matchmaking is confirmed active.

In `_run_game_lobby_quick_scan`, the CANCEL/UNREADY resolution block fires the same trigger for both crops:

```python
for crop in ("CANCEL", "UNREADY"):
    ...
    if detected:
        self._trigger("cancel_detected")   # ← fires for UNREADY too → GAME_STARTING
        handled = True
        break
```

Advancing to `GAME_STARTING` on UNREADY is incorrect. `GAME_STARTING` waits for a "Good Luck" screen; it has no CANCEL confirmation step and no recovery path if other squad members take a long time to ready up (only `starting_give_up` after its own stall timeout).

The semantically correct transition is `play_clicked` → `GAME_WAITING`. The UNREADY state means "READY was already clicked" — equivalent to what `play_clicked` normally signals. `GAME_WAITING` then:
- Scans for CANCEL every 3 s (confirming matchmaking started once all members ready up)
- Fires `cancel_detected` → `GAME_STARTING` naturally when CANCEL appears
- Falls back to `GAME_LOBBY` via `waiting_timeout` after 180 s if the squad never fully readies

**Fix**: when `crop == "UNREADY"` is detected, fire `play_clicked` (→ `GAME_WAITING`) instead of `cancel_detected`; fire `cancel_detected` only when `crop == "CANCEL"`.

### 2. `last_play_click_ts` not reset on GAME_LOBBY re-entry — stale cooldown ✓ Fixed

`last_play_click_ts` was a local variable initialised to `0.0` when the thread started. It was updated when a PLAY click fired but never reset when the FSM re-entered `GAME_LOBBY`. If the sequence was:

```
GAME_LOBBY → play_clicked → GAME_WAITING → waiting_timeout → GAME_LOBBY
```

…and the round-trip took less than 60 s, the thread's cooldown suppressed the PLAY click for the remainder of the 60 s window.

**Fix applied**: promoted to `self._last_lobby_play_click_ts` (instance variable, initialised to `0.0` in `__init__`). `on_enter_GAME_LOBBY` now resets it to `0.0` before calling `_on_cancel_mission`, so any re-entry starts with a clean cooldown. The thread reads and writes `self._last_lobby_play_click_ts` instead of the former local.

### 3. Frame freshness not validated ✓ Fixed

Both the click-to thread and the lobby quick-scan thread read `_click_to_latest_frame`, written by `analyze_frame()`. There was no timestamp on the frame. If `analyze_frame()` stopped being called (main loop suspended, frame capture failing), the lobby thread would repeatedly OCR a stale frame — potentially re-detecting and re-clicking the same popup many times before the per-popup cooldown fired.

**Fix applied**: added `self._click_to_frame_ts = 0.0` (initialised in `__init__`). `analyze_frame()` now sets it to `time.time()` inside the existing `_click_to_frame_lock` acquisition. The lobby quick-scan thread reads both values under the lock and skips the cycle with a debug log if the frame is older than 3 s.

## Alternatives Considered

**Retain `start_auto_mission()` with a guard flag** — add a mutex to prevent concurrent runs. Rejected: the main-loop stall guard would still need to exist, and two separate mechanisms for the same task is harder to reason about than one.

**Move CANCEL scan for GAME_WAITING into the quick-scan thread** — considered but not done. The main-loop 3 s CANCEL scan also owns the `game_waiting_since` 180 s timeout and the PLAY re-click on CANCEL-absent. Moving all of that into the thread would require the thread to own more state. Left as main-loop responsibility for now.

## References

- [wingman/analyzer.py#L1000](../../wingman/analyzer.py#L1000) — `_run_game_lobby_quick_scan`
- [wingman/analyzer.py#L683](../../wingman/analyzer.py#L683) — thread start in `analyze_frame`
- [wingman/analyzer.py#L642](../../wingman/analyzer.py#L642) — `_lobby_quick_scan_stop.set()` in `cleanup`
- [wingman/main.py#L131](../../wingman/main.py#L131) — `_on_lobby_play_click` injection
- [wingman/main.py#L135](../../wingman/main.py#L135) — `_handle_lobby_popup` and `_on_lobby_popup_click` injection
- [wingman/main.py#L287](../../wingman/main.py#L287) — main-loop GAME_LOBBY transition handler
- [ADR 026](026-game-lobby-state-machine-sequence.md) — superseded; describes the `start_auto_mission()` architecture
- [ADR 025](025-formalise-game-state-machine.md) — FSM formalisation; full transition table
