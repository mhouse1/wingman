# Anomaly 013 — A Test Xvfb on the Nested Display Blinds `make r2`

| Status | Date       | Wingman Version |
|--------|------------|-----------------|
| Draft  | 2026-10-25 | 1.9.3           |

## Summary

**Status: cause measured 2026-10-25; two of three causes fixed, live check in progress.**
Twice on 2026-10-25, at 12:04 and 12:49, `make r2` started wingman with no game. Wingman
attached to `:3`, read an all-black frame, and sat in `GAME_UNKNOWN` until the operator stopped
it. The server answering on `:3` was not the nested Xwayland ([ADR 099](../adr/099-nested-display-lane-for-unattended-operation.md)).
It was a private Xvfb that a pytest run had started for its own use ([ADR 153](../adr/153-gate-lanes-on-a-private-xvfb.md)).
That pytest had died without stopping it. `nested-display.py start` saw a server answering with
no game attached, reused it, and the game was launched onto a GPU-less Xvfb. Numbers are measured
from the logs and processes named, unless labelled otherwise.

## What was on `:3`

| Measured | Value |
|---|---|
| Listener on `/tmp/.X11-unix/X3` (`ss -xlp`) | `Xvfb` pid 2142480, started 11:51:48 |
| Its command line | `Xvfb -displayfd 11 -nolisten tcp -screen 0 1920x1200x24` (the conftest form) |
| Nested Xwayland on `:3` | none |
| `Metalstorm.exe` at 12:08 | none |
| `unknown_20261025_125033_stuck30s.png` | 1920x1200, max pixel 0 (all black) |

The 12:49 session, from `wingman.log` before it was rotated:

```
12:49:58,787 ADR 099: nested lane ACTIVE - capture and injection on :3, hotkeys observed on :0, :3
12:50:03,288 Game state: UNKNOWN → GAME_UNKNOWN
12:50:33,698 ADR074 anomaly: GAME_UNKNOWN stuck for 30s (no calibrated popup crop matched — nothing to dismiss)
12:50:58,925 Exiting
```

The 12:04 session logged `GAME_UNKNOWN startup classification timeout after 236.3s` at 12:08:33.

## Three causes

1. **The Xvfb leaked.** `tests/test_replay_integration_path1_path2.py` runs `wingman.main.main()`
   inside pytest. `main()`'s `finally` arms the 90 s shutdown watchdog
   ([ADR 121](../adr/121-hung-shutdown-watchdog.md)), which nothing cancels in-process, so 90 s later
   it calls `os._exit(2)` in whatever test is running. The 11:51 `make ocr` died that way inside
   `test_telemetry_corpus`: make reported `Error 2` and pytest printed no summary. `os._exit`
   skips conftest's teardown, so the Xvfb was never stopped (inferred from the exit code and the
   missing summary; the watchdog's banner was not found in any log).
2. **The test display can be `:3`.** conftest and `scripts/gate-display.sh` start Xvfb with
   `-displayfd`, which takes the lowest free display number. With `:0` and `:1` held by the
   desktop, one test run gets `:2` and a second concurrent one gets `:3`. Both were seen on
   2026-10-25 (measured from the `display: private Xvfb on :N` header lines).
3. **`nested-display.py start` reused any idle server.** Its "up and no game attached" branch
   printed `already running (idle) — reusing` without checking that the server was its own
   Xwayland.

## Fixes

| Cause | Fix | State |
|---|---|---|
| 1 | `_run_replay` stubs `_arm_shutdown_watchdog` for the in-process run | done 2026-10-25 |
| 3 | `start()` reuses an idle server only if `find_nested_display_pids` finds the nested Xwayland; otherwise it exits 1 and names the socket owner | done 2026-10-25 |
| 2 | start the test Xvfb on an explicit high display number, never `:3` | proposed |

Against the real leftover, the guard printed:

```
ERROR: an X server answers on :3 but it is not the nested Xwayland (owner: Xvfb pid 2142480). Stop it before starting a session.
```

Tests: `test_start_refuses_to_reuse_a_server_that_is_not_the_nested_xwayland` fails with the
guard disabled (measured: 1 failed, 37 passed), and `test_the_socket_owner_is_found_by_its_listening_inode`
covers the owner lookup. `make lint` and `make test` passed: 3325 passed.

## Live check

| Run | Code state | Game UI | Result |
|---|---|---|---|
| `make r2`, launched 13:07:58 | HEAD `6107007` plus the working tree of 2026-10-25 (uncommitted) | October lobby layout | `:3` served by Xwayland pid 2327726, `Metalstorm.exe` up, wingman pid 2328766 from 13:08:00; `GAME_LOBBY` 13:08:21, PLAY read and clicked 13:08:24, `GAME_STARTING` 13:08:28, `mission 'acs' started` 13:08:58; one round flown (2 deaths, click-to finish 1); Backspace on `:3` at 13:13:03 put wingman in STANDBY with the game up. Fix confirmed: the game and wingman both ran on a real nested display |

A second finding from the same run, not caused by this anomaly: at 13:12:58 the lobby walk-back
fired after PLAY was read 6 times in `GAME_STARTING` (13:12:52 to 13:12:58), and every lobby crop
was gone from 13:13:01. That is the [Anomaly 012](012-lobby-walk-back-abandons-starting-matches.md)
shape (a match loading after the walk-back), inferred rather than measured: the Backspace came
5 s later, so no frame shows what the game did next.
