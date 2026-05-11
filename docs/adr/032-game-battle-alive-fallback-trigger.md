# ADR 032 — `_game_battle_alive` Fallback Trigger for GAME_STARTING → GAME_BATTLE

| Status   | Date       | Wingman Version |
|----------|------------|-----------------|
| Accepted | 2026-05-10 | 1.6.6           |

## Context

The current `GAME_STARTING` → `GAME_BATTLE` transition is gated entirely on OCR detection of a
"Good Luck" banner in the `good_luck` crop region (trigger: `good_luck_detected`). The
`_start_game_starting_loop` presses `MISSION_J20_KEY` every 5 seconds and scans for the banner;
once found it waits 13 seconds before firing the transition.

This presents a reliability problem. The "Good Luck" banner is a visual UI element that can be
missed — OCR noise, transient rendering artefacts, or the banner appearing off-screen during a
loading transition can all prevent detection. When that happens the loop runs for 180 seconds
before escalating to `GAME_STARTING_STALLED`, delaying mission start by up to three minutes.

`_game_battle_alive` is an existing flag on `Analyzer` (guarded by `_health_lock`) that is set to
`True` whenever the health OCR returns a value ≥ 1 during `GAME_BATTLE`. In practice this flag
becomes `True` the moment the player's aircraft spawns into the match — which is a reliable signal
that the battle is active and the mission can start immediately.

## Decision

### Step 1 — extend the OCR state guard to scan health in `GAME_STARTING`

The OCR background thread ([analyzer.py:841](../../wingman/analyzer.py#L841)) currently skips all
processing for states outside `GAME_BATTLE / GAME_BATTLE_MANUAL / GAME_END_B`. Because of this,
`_game_battle_alive` is never written while the FSM is in `GAME_STARTING` — the flag stays frozen
at its previous-round value and polling it from the game_starting loop would be inert.

The guard must be extended to also execute the `HEALTH` crop scan when the state is
`GAME_STARTING` **and** at least 10 seconds have elapsed since the loop started. A new
`threading.Event`, `_game_starting_health_scan_enabled`, is set by
`_start_game_starting_loop` after the 10-second gate; the OCR thread checks it before deciding
whether to run the health scan in `GAME_STARTING`. The event is cleared when the loop exits.

### Step 2 — poll the flag inside the existing wait loop

After the system has been in `GAME_STARTING` for **10 seconds**,
`_start_game_starting_loop` checks `analyzer.game_battle_alive` on **every 0.1 s tick** of the
existing `for _ in range(50)` wait loop — no separate polling interval or thread is needed.
If `game_battle_alive` becomes `True` while still in `GAME_STARTING`, the loop fires
`good_luck_detected`, calls `_set_last_mission("j20")`, and launches `mission_j20` immediately
(without the 13-second post-banner delay).

The 10-second gate prevents a race where health data from a previous round is still stale when the
loop starts. The existing `good_luck_detected` trigger is reused so no new FSM transition is
needed; the fallback is purely an additional activation path inside the existing loop.

The 13-second post-Good-Luck delay is **not** applied when the `_game_battle_alive` path fires —
by the time health is visible the player is already in the battle, so waiting further would delay
the mission unnecessarily.

### Summary of changes

| Component | Change |
|-----------|--------|
| `analyzer.py` → OCR thread state guard | Also run `HEALTH` crop scan when state is `GAME_STARTING` and `_game_starting_health_scan_enabled` is set. |
| `analyzer.py` → `Analyzer.__init__` | Add `self._game_starting_health_scan_enabled = threading.Event()`. |
| `controller.py` → `_start_game_starting_loop` | After 10 s gate: set `_game_starting_health_scan_enabled`; check `game_battle_alive` each 0.1 s tick; on `True`, fire `good_luck_detected`, call `_set_last_mission("j20")`, launch mission, clear event, return. Clear the event in all exit paths. |
| `analyzer.py` FSM | No change — `good_luck_detected` trigger is reused. |
| `analyzer.py` `_STATE_CROPS` | No change. |

## Sequence

```mermaid
sequenceDiagram
    participant Loop as game_starting loop
    participant OCR  as Good Luck OCR (async)
    participant HOCR as Analyzer OCR thread
    participant FSM  as Analyzer FSM

    Loop->>Loop: enter GAME_STARTING, loop_start = now
    loop Every 5 s (first 10 s: key press + Good Luck scan only)
        Loop->>Loop: press MISSION_J20_KEY
        Loop->>OCR: async scan good_luck region
        OCR-->>Loop: good_luck_event (if banner detected)
        Loop->>Loop: wait 5 s in 0.1 s ticks
    end
    note over Loop,HOCR: t=10 s — set _game_starting_health_scan_enabled
    HOCR->>HOCR: OCR guard now includes GAME_STARTING health scan
    loop Every 5 s (after 10 s: key + Good Luck + health check each 0.1 s tick)
        Loop->>Loop: press MISSION_J20_KEY
        Loop->>OCR: async scan good_luck region
        OCR-->>Loop: good_luck_event (if banner detected)
        loop Each 0.1 s tick
            Loop->>HOCR: read game_battle_alive
            alt game_battle_alive == True
                Loop->>FSM: _trigger("good_luck_detected")
                Loop->>Loop: _set_last_mission("j20"), launch mission_j20 (no extra delay)
                Loop->>Loop: clear _game_starting_health_scan_enabled, return
            end
            alt good_luck_event set
                Loop->>Loop: break inner tick loop
            end
        end
        alt good_luck_event set
            Loop->>Loop: wait 13 s post-banner
            Loop->>FSM: _trigger("good_luck_detected")
            Loop->>Loop: _set_last_mission("j20"), launch mission_j20
            Loop->>Loop: clear _game_starting_health_scan_enabled, return
        end
    end
    alt 180 s timeout
        Loop->>FSM: _trigger("starting_timeout")
        Loop->>Loop: clear _game_starting_health_scan_enabled, return
    end
```

## Consequences

**Positive**
- Mission starts as soon as the aircraft spawns when "Good Luck" is missed or delayed, eliminating
  the 180-second stall.
- No new FSM states or triggers — the fallback reuses `good_luck_detected`.
- No extra threads — the health scan runs in the existing OCR thread; the poll runs inside the
  existing `_loop` thread.

**Negative / Risks**
- The OCR thread now runs a lightweight health crop scan in `GAME_STARTING` (after the gate). This
  is a minor overhead and uses the same executor path already used in `GAME_BATTLE`.
- `_game_battle_alive` is a health-OCR artefact; if the health region misreads a loading screen as
  a valid health number the flag could fire prematurely. The 10-second gate mitigates most of this
  since the loading screen will have cleared by then.
- `_game_starting_health_scan_enabled` must be cleared in all exit paths of the loop (Good Luck,
  fallback, timeout, state change) to prevent the OCR thread from scanning health unnecessarily
  between rounds.

## Alternatives Considered

**Inline health scan in game_starting loop** — the loop could grab its own frame and run health
OCR directly, bypassing the flag. Rejected: duplicates OCR logic already in `Analyzer`; the
shared-event approach keeps health scanning in one place.

**Poll from day 0 (no 10 s gate)** — rejected because stale health data from the previous round
can leave `_game_battle_alive` transiently `True` at the start of `GAME_STARTING`.

**New FSM trigger `battle_alive_detected`** — considered but provides no benefit; the existing
`good_luck_detected` trigger already encodes "begin mission now" semantics regardless of which
signal fired it.

**Separate fallback thread** — unnecessary complexity; a poll inside the existing 0.1 s tick loop
is sufficient and avoids synchronisation overhead.
