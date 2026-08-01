# ADR 059 — Health-Gated Immediate Mission Restart

| Status | Date       | Wingman Version |
|--------|------------|-----------------|
| Draft  | 2026-07-31 | 1.6.29          |

Supersedes the scheduled-restart flow documented in
[ADR 011](011-respawn-mission-restart-flowchart.md) (the flowchart, delay
timers, and retry loop described there no longer exist). Also updates the
respawn-during-manual behavior relative to the stay-manual design briefly
shipped on 2026-07-30, and notes two mission-accounting changes that refine
[ADR 055](055-mission-level-statistics-tracker.md).

## Context

The restart flow after a death had grown three overlapping mechanisms:

1. A **scheduled restart**: respawn detection armed a 4 s delay
   (`restart_delay_after_unlock`), a 2 s retry loop
   (`restart_retry_interval`), a 20 s stuck-OCR fallback
   (`respawn_fallback_timeout`), and a 12 s health guard — the machinery
   ADR 011 documents.
2. An **immediate health-alive restart**: the analyzer's dead→alive health
   transition sets a one-shot `alive_event`; the handler restarts the mission
   after a short respawn-clear stability window.
3. A **stay-manual rule** (2026-07-30): a respawn during `GAME_BATTLE_MANUAL`
   kept the FSM manual and required `u` to resume.

Production 2026-07-31 07:42 exposed the interaction bug: the player died in
manual mode, health returned, the log printed "scheduling restart in 4.0s" —
and nothing ever restarted, because the scheduled path is `GAME_BATTLE`-gated
while the stay-manual rule held the FSM in `GAME_BATTLE_MANUAL`. The aircraft
flew uncommanded until the operator quit. The scheduled path also raced the
immediate path in ordinary auto-mode deaths (the immediate path always won
when healthy, making the scheduler mostly-dead code with live failure modes).

## Decision

**One restart path: as soon as health returns, the mission restarts.**

1. **Death ends manual takeover.** Respawn detection during
   `GAME_BATTLE_MANUAL` fires `respawn_reset` (after the P2_040 live-capture
   hook, which needs the pre-transition frame) and re-enables auto-restart.
   Manual mode remains sticky only while alive: `i/j/k/l` takeover still
   requires `u` to resume — until the aircraft dies.
2. **The scheduled-restart machinery is removed**: the `PENDING_RESTART`
   state, delay/retry/fallback timers, and their four config keys are gone.
   `RespawnState` is reduced to `IDLE`/`RESPAWNING` (a detection-dedup latch,
   cleared when the respawn screen clears or on `GAME_END_B`).
3. **The alive-event handler is hardened to carry the whole load:**
   - Deferrals **re-arm** the one-shot event instead of consuming it: while
     respawn OCR still flaps, during the `respawn_clear_stability_s` window,
     and while a *cancelled* mission thread still holds the mission lock
     (teardown — distinguished from a genuinely running mission via the
     mission-cancel flag, `Controller.is_mission_teardown_in_progress()`).
     Health frequently returns 100–300 ms before the respawn screen clears;
     consuming the event there lost the only restart signal.
   - A new death **clears** any pending re-armed event, so stale pre-death
     health cannot restart the mission after the next respawn.
   - `reset_health_for_respawn()` clears the cached health value, so
     `on_enter_GAME_BATTLE` (respawn_reset path) cannot re-arm the event from
     the previous life's health when OCR missed the terminal 0.
4. **Eject is auto-mode only**: the missiles-empty handler is additionally
   gated on `GAME_BATTLE`, closing the teardown window where an eject could
   inject inputs into a manual flight.

## Consequences

- Restart latency after health returns is one stability window
  (`respawn_clear_stability_s`, 1.5 s) instead of 4 s minimum.
- There is no restart without confirmed health — the health-guard-timeout
  behavior ("restart despite health unknown") is gone. If health OCR fails
  entirely, no restart occurs; that is deliberate: restarting a dead aircraft
  pressed keys into the respawn screen.
- The ADR044 runtime gate now asserts the flow end-to-end
  (`respawn_detected` and `restart_last_mission` checkpoints on
  P1_050/P1_060), so it cannot silently regress while gates stay green.

## Mission-accounting refinements (ADR 055)

Two behavior changes to `MissionStatsTracker` refine ADR 055 without changing
its architecture; recorded here per the superseding-decisions rule:

- `GAME_BATTLE_EJECT` is a member of the battle-state set: an eject is a
  mid-mission excursion, not a mission boundary (previously each eject split
  one round into two "missions" and dropped takeover counts).
- At mission end, the **terminal trigger wins** over a stale mid-mission
  `_pending_outcome`: a round that ran out of missiles mid-way but ended on
  the click-to-continue screen is a `click_to` finish (the missiles-empty
  fact stays on the mission record as `no_missiles_abort`).

## Validation

- `make tp` green with the strengthened ADR044 checkpoints; the replay run
  demonstrates the deferral-retry sequence (8 deferrals while the respawn
  screen persisted, restart 1.5 s after clear).
- `tests/test_mission_stats.py` pins both accounting refinements
  (mutation-verified).
- Live validation: watch a manual-mode death — expect
  "Respawn screen active — mission restarts when health returns" followed by
  "💚 HEALTH ALIVE — restarting mission immediately".
