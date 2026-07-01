# ADR 055 — Mission-Level Statistics Tracker

| Status   | Date       | Wingman Version |
|----------|------------|-----------------|
| Accepted | 2026-06-26 | 1.6.22          |

## Context

Wingman runs fully unattended for extended sessions but produces no mission-level telemetry.
The existing `PerformanceTracker` (`wingman/performance.py`) records per-crop OCR latency
and incoming-reaction timing, but nothing tracks gameplay outcomes at the mission level.

As a result, it is impossible to answer basic operational questions from a session log:

- How many missions completed this session?
- How often did the bot respawn vs. run out of missiles vs. finish normally?
- What was the average mission duration?
- How many flare bursts were deployed per mission?

This gap also blocks Phase 4 (Reinforcement Learning, ADR 024) from getting started: a reward
signal requires a record of outcomes. Establishing mission-level data collection now creates the
infrastructure RL will need without committing to the full RL architecture yet.

### Current event hooks in `main.py`

`main.py` already fires named events at key moments via `_emit_capture_event()`.
Two additional events need new hook points (not currently emitted):

| Event | Currently emitted? | Where |
|-------|--------------------|-------|
| `respawn_detected` | ✅ | `_emit_capture_event` in respawn block |
| `restart_last_mission` | ✅ | `_emit_capture_event` after `ctrl.restart_last_mission()` |
| `missiles_empty` | ✅ | `_emit_capture_event` in `_handle_no_missiles` |
| `click_to_detected` | ✅ | `_emit_capture_event` in click-to block |
| FSM state transitions | ✅ | `analyzer.set_on_fsm_transition` callback |
| `flare_burst_deployed` | ❌ | needs new call inside `_deploy_flares_on_new_incoming` |
| `flare_reload` | ❌ | needs new call inside `_handle_low_flares` |

### FSM callback constraint

`GameStateAnalyzer.set_on_fsm_transition` stores a single callback; calling it twice replaces
the first. When `replay_mode` or `capture_mode` is active, the replay/capture engine already
occupies this slot. The stats tracker must not evict it. See **Wiring** below.

## Decision

Add `MissionStatsTracker` (new file: `wingman/mission_stats.py`) that counts session-level
and per-mission events, prints a formatted summary on exit, and writes a JSON file to
`docs/performance/current/` alongside existing `run_*.json` OCR timing files.

### Data collected

**Per-mission** (reset on each `GAME_BATTLE` or `GAME_BATTLE_MANUAL` entry):

| Field | Source |
|-------|--------|
| `start_ts` | `state_enter:GAME_BATTLE` FSM transition |
| `duration_s` | delta to the next GAME_LOBBY, GAME_END_B, or GAME_WAITING entry |
| `respawn_count` | `respawn_detected` event |
| `flare_burst_count` | `flare_burst_deployed` event (new) |
| `flare_reload_count` | `flare_reload` event (new) |
| `no_missiles_abort` | `missiles_empty` event (bool) |
| `manual_takeover_count` | `state_enter:GAME_BATTLE_MANUAL` FSM transition |
| `outcome` | see outcome classification below |

**Outcome classification** (derived from the FSM transition that ends the mission):

| Outcome | Trigger |
|---------|---------|
| `"click_to"` | `click_to_detected` event — normal match completion |
| `"missiles_empty"` | `missiles_empty` event before a non-BATTLE state |
| `"lobby_exit"` | GAME_LOBBY entered directly without GAME_END_B (e.g. waiting_timeout, reclassify) |
| `"unknown"` | session ended while still in GAME_BATTLE (e.g. KeyboardInterrupt) |

**Session totals** (accumulated across all missions):

| Field | Description |
|-------|-------------|
| `session_start_ts` | wallclock time at tracker construction |
| `session_duration_s` | elapsed at `finalize()` |
| `missions_started` | count of `state_enter:GAME_BATTLE` transitions after startup |
| `missions_click_to` | count of `"click_to"` outcomes |
| `missions_missiles_empty` | count of `"missiles_empty"` outcomes |
| `missions_lobby_exit` | count of `"lobby_exit"` outcomes |
| `missions_unknown_outcome` | count of `"unknown"` outcomes |
| `total_respawns` | all `respawn_detected` events |
| `total_flare_bursts` | all `flare_burst_deployed` events |
| `total_flare_reloads` | all `flare_reload` events |
| `total_manual_takeovers` | all `state_enter:GAME_BATTLE_MANUAL` transitions |
| `avg_mission_duration_s` | mean of missions with a known end time |

### Interface

```python
class MissionStatsTracker:
    def on_event(self, event_name: str, ts: float) -> None: ...
    def on_fsm_transition(
        self, trigger_name: str, prev_state: str, next_state: str, ts: float
    ) -> None: ...
    def finalize(self, run_id: str | None = None) -> dict: ...
    def print_summary(self) -> None: ...
```

`on_fsm_transition` accepts the same four-argument signature as the existing
`_on_fsm_transition` callbacks — no analyzer change required.

### Wiring in `main.py`

**FSM callback chaining** — wrap the existing callback in a closure so both run:

```python
_existing_fsm_cb = None  # set earlier for replay/capture mode

def _chained_fsm_cb(trigger, prev, nxt, ts):
    if _existing_fsm_cb is not None:
        _existing_fsm_cb(trigger, prev, nxt, ts)
    stats_tracker.on_fsm_transition(trigger, prev, nxt, ts)

analyzer.set_on_fsm_transition(_chained_fsm_cb)
```

This means `set_on_fsm_transition` is called exactly once (after replay/capture callbacks are
registered), so the single-slot constraint is satisfied without modifying the analyzer.

**New events** — two lines added in `main.py`:

```python
# in _deploy_flares_on_new_incoming, before the burst thread is started:
stats_tracker.on_event("flare_burst_deployed", time.time())

# in _handle_low_flares, after ctrl.reload_flares():
stats_tracker.on_event("flare_reload", time.time())
```

**Startup exclusion** — `MissionStatsTracker.on_fsm_transition` ignores
`state_enter:GAME_BATTLE` until the first non-`GAME_UNKNOWN` state has been seen.
This mirrors the `startup_classification_complete` flag in `main.py` and prevents counting
a partial mission that might be in progress when wingman starts mid-game.

**Replay/capture exclusion** — stats tracker is not constructed when `replay_mode` or
`capture_mode` is active; stats are meaningless for test replay runs. All wiring is
guarded by `if stats_tracker is not None`.

**`finally` block** — after `analyzer.cleanup()`:

```python
if stats_tracker is not None:
    stats_tracker.finalize(run_id=tracker.run_id)  # correlate with PerformanceTracker
    stats_tracker.print_summary()
```

`PerformanceTracker` exposes `run_id` (already computed at construction) so both JSON
files share the same filename timestamp.

### Output JSON (example)

```json
{
  "wingman_version": "1.6.22",
  "run_id": "20260626_153000",
  "session_start_ts": 1751000000.0,
  "session_duration_s": 3612.4,
  "missions_started": 18,
  "missions_click_to": 14,
  "missions_missiles_empty": 2,
  "missions_lobby_exit": 1,
  "missions_unknown_outcome": 1,
  "total_respawns": 6,
  "total_flare_bursts": 47,
  "total_flare_reloads": 12,
  "total_manual_takeovers": 3,
  "avg_mission_duration_s": 183.7,
  "missions": [
    {
      "index": 0,
      "start_ts": 1751000042.1,
      "duration_s": 201.3,
      "respawn_count": 0,
      "flare_burst_count": 4,
      "flare_reload_count": 1,
      "no_missiles_abort": false,
      "manual_takeover_count": 0,
      "outcome": "click_to"
    }
  ]
}
```

### Console summary (example)

```
━━━ Wingman Session Summary ━━━━━━━━━━━━━━━━━━━━━━━━━━
Session duration  : 1h 00m 12s
Missions started  : 18
  Click-to finish : 14   (78%)
  Missiles empty  : 2    (11%)
  Lobby exit      : 1    ( 6%)
  Unknown outcome : 1    ( 6%)
Avg mission time  : 3m 03s
Total respawns    : 6
Total flare bursts: 47   (2.6 per mission)
Flare reloads     : 12
Manual takeovers  : 3
Stats saved to    : docs/performance/current/run_20260626_153000_stats.json
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
```

## Consequences

**Positive:**

- Provides operational visibility into unattended sessions without changing any detection logic.
- Two new events (`flare_burst_deployed`, `flare_reload`) are minimal additions to `main.py` — each is one line in an existing handler.
- JSON output lands in `docs/performance/current/` and shares the `run_id` with the existing OCR timing file, so both files can be correlated by filename.
- Sets up the data collection pattern required by Phase 4 RL: mission outcome + duration is the raw reward signal.
- Orthogonal to `PerformanceTracker`: OCR latency vs. gameplay outcomes are distinct concerns in separate files.
- Zero impact on replay/capture test modes.

**Negative / risks:**

- The `run_id` sharing requires `PerformanceTracker` to expose `run_id` as a public attribute. It currently computes it internally inside `_write_run_file`. A one-line change moves it to `__init__`.
- Flare burst events are emitted from the main thread just before the burst daemon thread starts. If the main loop is under heavy load and the burst thread fires late, the `flare_burst_deployed` timestamp is slightly optimistic. This is acceptable for counting purposes.
- Mission boundary detection relies on FSM state transitions, which are noisy during startup classification. The `startup_classification_complete` guard must be enforced inside the tracker, not assumed from the caller.

## Alternatives considered

**Extend `PerformanceTracker`:** Rejected. `PerformanceTracker` owns OCR/perception timing; mixing gameplay-outcome counters would blur its responsibility and complicate the existing regression-check logic.

**Log scraping:** Rejected. Parsing structured data from free-text log lines is fragile. Event callbacks are the correct hook.

**SQLite (ADR 043):** ADR 043 proposes a SQLite store for performance data. That is a richer but higher-effort path. This ADR is intentionally simpler — JSON per session, no schema, no query layer. If ADR 043 is implemented, mission stats records are a natural addition to its schema.

## Implementation checklist

- [ ] `wingman/mission_stats.py` — `MissionStatsTracker` class
- [ ] `wingman/performance.py` — expose `run_id` as a public attribute
- [ ] `wingman/main.py` — add two new event calls, chain FSM callback, wire tracker in `finally`
- [ ] `tests/test_mission_stats.py` — unit tests: mission boundary detection, outcome classification, session aggregation, JSON serialisation
