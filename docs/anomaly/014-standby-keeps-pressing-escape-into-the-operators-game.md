# Anomaly 014 — STANDBY Keeps Pressing ESC Into the Operator's Game

| Status | Date       | Wingman Version |
|--------|------------|-----------------|
| Draft  | 2026-10-25 | 1.9.3           |

## Summary

**Status: cause measured 2026-10-25; fixed the same day (uncommitted), live check pending.**
The same leak shows on 2026-10-21 (STANDBY 07:28:36, ESC 07:30:09,
`logs/wingman_20261021_073020.log`), so it predates that day's changes. STANDBY tells the operator
"wingman has stopped — MetalStorm is still running and is yours to fly". On 2026-10-25 it kept
pressing ESC into that game every 45 s for 25 minutes. The GAME_LOBBY escape loop (ADR 093) is a
thread that `main` stops only on an FSM state change. The shutdown path never stops it, and in
STANDBY no state change ever comes, because the process stays alive with the analyzer stopped.
Measured from `wingman.log` of the 13:08 `make r2` session
([Anomaly 013](013-a-test-xvfb-on-the-nested-display-blinds-make-r2.md)), rotated into `logs/`
by the next start.

## The sequence

```
13:12:58,104 Lobby quick-scan: PLAY still visible after 6 reads in GAME_STARTING — returning to GAME_LOBBY (ADR 102)
13:13:03,286 Controller: Backspace — ending wingman; MetalStorm stays up for manual control.
13:13:03,837 Exit requested, shutting down
13:13:08,059 STANDBY: wingman has stopped — MetalStorm is still running and is yours to fly.
13:13:44,368 GAME_LOBBY escape loop: ESC suppressed — lobby blackout in progress (43s)
13:15:14,369 GAME_LOBBY escape loop: blackout 133s past the ESC ceiling — pressing ESC anyway (ADR 093)
   ... every 45 s ...
13:39:59,401 Controller: escape_recovery - pressing 'escape' key for 0.05 seconds
13:40:25,003 STANDBY: interrupted — leaving MetalStorm running
```

| Measured | Value |
|---|---|
| ESC presses after STANDBY began | 33 (13:15:14 to 13:39:59), 45 s apart |
| Lobby blackout age at the first press | 133 s; every lobby crop had been gone since 13:13:01 |
| SIGTERM at 13:39:20 | ignored: the handler sets `exit_requested`, which the STANDBY wait does not read |
| SIGINT at 13:40:25 | ended wingman; MetalStorm and `:3` stayed up |

The blackout is what a match in progress looks like to the lobby crops (inferred: the walk-back
5 s earlier fits [Anomaly 012](012-lobby-walk-back-abandons-starting-matches.md), and no frame was
saved after 13:13). If a battle was running, each ESC opened the in-game menu over the operator's
flight.

## Cause

`main.py` starts the loop on entering `GAME_LOBBY` and stops it (`_stop_lobby_escape_loop()`)
on any other state change. Its own exit check is `analyzer.game_state != GameState.GAME_LOBBY`.
After `Exit requested`, neither can happen: the main loop no longer runs, so the state stays
`GAME_LOBBY`, and the `finally` block (`ctrl.cleanup(keep_hotkeys=standby_armed)`) does not call
`_stop_lobby_escape_loop()`. A normal exit hides this, because the daemon thread dies with the
process. STANDBY keeps the process alive.

## Fix

`main()`'s `finally` calls `_stop_lobby_escape_loop()` before `ctrl.cleanup(...)`.
`test_standby_stops_the_lobby_escape_loop_before_handing_over` (in
`tests/test_finish_round_then_exit.py`) fails with the call removed. Not yet done: an audit of the
other threads that press keys on their own timer.
