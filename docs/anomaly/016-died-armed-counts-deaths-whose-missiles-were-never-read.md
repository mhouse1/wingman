# Anomaly 016 — "Died Armed" Counts Deaths Whose Missiles Were Never Read

| Status | Date       | Wingman Version |
|--------|------------|-----------------|
| Draft  | 2026-10-29 | 1.9.3           |

## Summary

The session summary's "Died armed … (missiles unused at death; ADR 143)" no longer measures deaths
with missiles aboard. Since at least 2026-10-25 it has counted nearly every death outside an eject,
because the missile count at death is almost never read and an unread count is booked as armed.
The ADR 137 D8 pre-crash buffer, which exists to supply that count, has been empty at every armed
death in the logs checked: wingman flies the pursuit outside a mission thread, and the buffer only
samples while a mission runs. Found 2026-10-29 during the `/iterate` loop on account 3; not fixed,
the operator decides.

## Evidence (measured)

`DIED ARMED` lines whose numbers came from the pre-crash buffer (`pre-crash frame, Ns old`)
against those that had none (`no pre-crash frame buffered`), and the summaries:

| Session | Respawns | Died armed | Missiles read at death | From the buffer |
|---|---|---|---|---|
| 2026-10-28 17:06, account 2 | 287 | 241 | 3 of 241 | 0 |
| 2026-10-29 04:39, account 3 | 34 | 30 | 1 of 30 | 0 |
| 2026-10-29 07:13, account 3 (to 08:47) | | 41 lines | 1 of 41 (`6 missile(s)`) | 0 |

Every log from 2026-10-25 05:32 to 2026-10-29 has 0 buffered `DIED ARMED` lines, over 1,600 in
all. The one count read in the 07:13 session came from the live HUD, not the buffer.

In the 07:13 session's behavior-tree lines, `mission=True` on 136 of 3,735 ticks (4%) and
`mission=False` on 3,599.

## Cause

`RespawnHandler._sample_pre_crash_buffer` (`wingman/tick_handlers.py`) returns early unless
`current_game_state == GAME_BATTLE and self._ctrl.is_mission_running()`. `is_mission_running()` is
true only while a mission thread holds the mission lock, which was most of a battle when ADR 137
D8 was written (2026-09-12). The pursuit now flies most of every battle outside one (4% of ticks
above), so the buffer is empty when a death is detected. The emit site then keeps `missiles=None`
from the death screen, and `if missiles is None or missiles > 0` books it as died armed (inferred
from the code and the 4% share; the gate's history was not traced).

## Consequences

- The "Died armed" total and its ADR 143 split (enemy fire, terrain, contested, unclassified)
  count deaths whose missiles were unknown. They are close to "deaths not in an eject", not
  deaths with missiles aboard.
- Each `DIED ARMED` line's `alt` and `rate` are None, and the saved crash frame is the death
  screen, not the moment before it: the D8 instrument is blind in pursuit flight.
- The ADR 143 cause itself does not depend on the buffer (it uses alert and emergency recency),
  and the kill card reader (Code Review 019 cycle 19) names enemy kills independently.

## Options (operator's decision)

1. **Sample the buffer in any battle tick**, dropping the mission gate. The `is_respawning`
   check still keeps dying frames out. Cost: one frame copy a tick for the 8 s window; at the
   1.5 s loop interval that is about 6 frames, about 40 MB (inferred; store a JPEG or a crop if it
   matters, as the HUD black box does).
2. **Count an unread missile count apart from armed**: "died armed" only when the count read is
   above 0, and a "missiles unknown" line beside it. This keeps the metric honest whenever the
   buffer misses, whatever its cause.

Recommended: both. Option 1 restores the data ADR 137 D8 meant to collect; option 2 stops a
silent gap from inflating the metric again.

## References

- [ADR 137](../adr/137-emergency-climb-airbrake-and-crash-instrument.md) D4 and D8: the
  per-occurrence line and the pre-crash buffer.
- [ADR 143](../adr/143-classify-died-armed-deaths-enemy-fire-vs-terrain.md): the cause split.
- `wingman/tick_handlers.py`: `_sample_pre_crash_buffer`, `_lookback_pre_crash_sample`, and the
  `DIED ARMED` emit site in `tick_detect`.
