# ADR 202 — Restart the Game Every 30 Rounds

| Status   | Date       | Wingman Version |
|----------|------------|-----------------|
| Draft    | 2026-11-23 | 1.9.5           |

## Decision

A live session ends after `session_cycle.rounds` finished rounds (30). At the end
of that round wingman asks for the [ADR 094](094-finish-round-then-exit-hotkey.md)
finish-round stop. That stop waits out the end screen, exits at the lobby, and
closes MetalStorm and the nested display. Wingman then waits
`session_cycle.wait_s` (120 s) and exits with code 75. `make r`, and so
`make r1` to `make r3`, loops on that code: it runs the game launch steps
(`nested-setup launch-game wait-game nested-focus`) for the same prefix again and
starts wingman again. Any other exit code ends the loop.

- A round counts once the FSM has reached a battle state and then entered
  `GAME_END_B` or `GAME_LOBBY`. A `GAME_UNKNOWN` mid-round
  ([ADR 195](195-a-battle-with-no-hud-finds-its-way-back.md)) does not end it.
- Live runs only. Replay and capture runs read the shipped config and never cycle.
- The operator keeps control. Pressing `z` while the cycle's stop is pending
  (that is, on the end screen) cancels it, as it cancels any finish-round stop, and turns the
  cycle off for the rest of the session. A `z` of the operator's own during a
  round exits 0 and ends the loop. Ctrl-C or SIGTERM during the 120 s wait also
  ends it.
- The 90 s shutdown watchdog ([ADR 121](121-a-hung-shutdown-must-leave-evidence.md)) is stood
  down for the wait and armed again for the exit that follows it.

## Why

The memory panel ("Memory per sample", [Design 018](../hldd/018-score-per-round-in-grafana-hldd.md))
shows the game's resident memory climbing for as long as it runs. Measured from the
`RESOURCE` lines of the 2026-11-22 21:10 session (7.1 h, 72 missions,
`logs/wingman_20261123_041909.log`):

```
game    rss 513->3870MB (+3357)  rate=+304 MB/h
wingman rss 680->3494MB (+2814)  rate=+89 MB/h  peak=3865MB
VERDICT: BOTH processes growing above 50 MB/h (game faster) — see Performance 008
```

Wingman cannot release the game's memory, and its own leak ([Performance 008](../performance/008-long-session-memory-leak-and-ocr-degradation.md))
is unfixed. Restarting both processes resets both. A cycle of 30 rounds is about
3.5 h at the 7.1 h session's pace of 72 missions. That session's game RSS was
2,810 MB at 3.5 h, against 3,870 MB at its end, and wingman's own RSS was 3,355 MB,
well under the memory guard's 6,000 MB soft limit. The cycle was first tried at
2 rounds to test the restart quickly (the live check below). The operator then
set 30 so that runs stay long and unattended.

The [ADR 090](090-memory-guard-bounded-session.md) memory guard stays as it is.
It watches wingman's RSS only, and it ends the session without closing the game,
so it cannot bound the game's growth.

## Consequences

- Each cycle pays the wait (120 s) plus a relaunch (6–12 s measured), about
  1% of a 3.5 h cycle. At 2 rounds, with cycles of 10–17 min, the same cost was
  about a sixth of the wall clock.
- Each cycle rotates a session log into `logs/` and writes a run file, so
  per-session figures cover up to 30 rounds. Per-round figures (Design 018) are
  unaffected.
- `make r`'s recipe now calls a sub-make through `CYCLE_MAKE := $(MAKE)`. A recipe
  line that names `$(MAKE)` runs even under `make -n`. During development a dry run of the first
  version started wingman (it found no `:3` and exited at once).
- On Windows the loop restarts wingman only, because `GAME_LAUNCH_DEPS` is empty there.

## Validation

- `tests/test_session_cycle.py`: round counting, the operator's cancel, the wait and its
  watchdog, the exit code, the shipped config, and the make loop's dry run.
- Live, with `rounds: 2`: in a `make r3` run, the log shows `SESSION CYCLE: round 2 of 2 finished`,
  then `exiting 75`, then a fresh session with the game relaunched. The game's
  RSS in the next session starts again near 500 MB.

### Live check, 2026-11-23 (`make r3`, acct3, uncommitted on 883064b)

The first cycle went through end to end (`logs/wingman_20261123_055046.log`):

```
05:48:32 GAME_BATTLE → GAME_END_B
05:48:32 SESSION CYCLE: round 2 of 2 finished — stopping at the lobby, closing MetalStorm, relaunching in 120 s
05:48:46 FINISH ROUND: stopping in GAME_LOBBY — no round in progress (ADR 094)
         game    rss 514->1476MB (+962)
05:48:46 Game shutdown: Metalstorm.exe closed / Nested display: :3 closed
05:50:46 SESSION CYCLE: exiting 75 for make r to relaunch
05:50:58 Configuration loaded (new session); RESOURCE elapsed=0s rss_mb=681 game_rss_mb=513
```

Round 2's matchmaking stalled for 150 s and then joined a round in progress
(GAME_UNKNOWN → GAME_BATTLE). It still counted as round 2, as intended. One cycle took 15.8 min: 13.4 min
of session, 2 min of wait, and 12 s of relaunch.

Three of the first five starts after the cycle went live stalled in matchmaking
(05:45, 05:59, 06:09). The stalls are about the hour, not the cycle. Across
November's logs, written before this ADR, 06:00 is the worst hour for stalls:
16 of 75 starts (21%), against 19 of 604 (3%) from 09:00 to 14:59. The second
relaunch took 7 s from exit 75 to the new session's first line (06:06:12 to 06:06:19).

A restart every two rounds makes startup about six times as frequent, so a rare
startup fault now comes up more often. At the fifth and sixth starts (06:35, 06:53) the
relaunched game opened on the previous session's PERFORMANCE panel again (a
screenshot at 06:53 shows score 250, K/D/A 2/3/1, which session 5 read and
clicked through at 06:50:32). The FSM classified it correctly
(`unknown_to_end_detected`), but the click-through does not run for an end screen
entered from GAME_UNKNOWN. The 30 s GAME_END_B timeout forced the lobby each time,
for a cost of 30 s per occurrence. The panel was not read again (no `ROUND
PERFORMANCE` line in the new session), and the cycle did not count it as a round.
Open: click through an end screen met at startup.

The operator's stop ends the loop. At 06:54:58 the operator pressed `z` on `:3`
(`injected=False`) during session 6's matchmaking. Wingman stopped at once,
closed the game, and exited 0. The `make r3` loop ended without relaunching.
Totals for the trial: five cycles completed from 05:35 to 06:53 with no failed
relaunch, and each relaunch took 6–12 s from exit 75 to the new session's first
line.
