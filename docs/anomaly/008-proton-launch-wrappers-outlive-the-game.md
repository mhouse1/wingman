# Anomaly 008 — Proton Launch Wrappers Outlive the Game After a `z` Exit

| Status | Date       | Wingman Version |
|--------|------------|-----------------|
| Draft  | 2026-09-27 | 1.8.12          |

## Summary

**Status: open, seen once.** After the 2026-09-27 15:54-18:35 session ended with `z`
(ADR 094), wingman closed MetalStorm and the nested display `:3` normally, but the
game's launch wrappers and its `wineserver` kept running. They were still up 3 min
10 s later, when the operator had them stopped with SIGTERM. Three other session ends
the same day left nothing behind. The cause is not known.

Nothing failed inside wingman: its shutdown found exactly one game process, closed it
within 0.25 s of SIGTERM, and closed `:3` 0.25 s later, as in the exits that left no
wrappers.

## What was observed

All times 2026-09-27, measured from `logs/wingman_20260927_*` and `ps` at the time.

| Time | Event |
|------|-------|
| 15:51-15:52 | `make rd` starts MetalStorm through umu and Proton. The wrapper processes below date from here. |
| 18:34:18 | `z` acknowledged (`FINISH ROUND: requested`). |
| 18:35:21.887 | `FINISH ROUND: stopping in GAME_LOBBY`. |
| 18:35:22.159 | `Game shutdown: closing Metalstorm.exe (1 process(es): 2843809)`. |
| 18:35:22.409 | `Game shutdown: Metalstorm.exe closed`: no SIGKILL was needed. |
| 18:35:22.665 | `Nested display: :3 closed`. |
| 18:35:41 to 18:37:10 | Six processes whose command line names `Metalstorm.exe`, plus the Proton `wineserver`, still running (below). A 60 s wait for them to exit timed out. |
| 18:38:32 | Operator asked for them to be stopped. SIGTERM to all seven; all exited within about 3 s. |

The processes left running, all started with the session:

| PID | Process |
|-----|---------|
| 2843599 | `umu-run …/Metalstorm.exe` |
| 2843616 | pressure-vessel `srt-bwrap` |
| 2843671 | pressure-vessel `pv-adverb` |
| 2843695 | GE-Proton `proton waitforexitandrun` |
| 2843711 | `srt-launcher-service` |
| 2843717 | `c:\windows\system32\umu.exe …/Metalstorm.exe` (the Windows-side launcher inside Wine) |
| 2843723 | GE-Proton `wineserver` |

## Other session ends the same day

| Session end | How it ended | Game shutdown lines | Wrappers afterwards |
|-------------|--------------|---------------------|---------------------|
| 08:48 | operator force-closed the game | none (wingman died with `:3`) | gone by 09:13 (pre-flight showed none) |
| 10:47 | the game exited on its own during standby | `MetalStorm exited on its own` | gone by about 10:48:40 (pre-flight) |
| 10:54:58 | `z` after a 6-minute, 1-mission run | closed 0.25 s after SIGTERM, `:3` 0.25 s later | **gone within 11 s** (checked 10:55:09) |
| 15:17:58 | `z` after the operator's 3 h 36 m, 36-mission run | same as above | gone by 15:54 (pre-flight); exact time not recorded |
| 18:35:22 | `z` after a 2 h 41 m, 29-mission run | same as above | **still running at 18:38:32**, stopped by hand |
| 19:45:29 | `z` in `GAME_WAITING` after a 6-minute, 1-round run | same as above | gone by about 20:10 (pre-flight: 0 processes); exact time not recorded |
| 20:32:39 | `z` in `GAME_LOBBY` after a 22-minute, 4-mission run | same as above | **gone within 9 s** (checked 20:32:48) |

The two long `z` exits went through the same shutdown path; one left the wrappers and
one, as far as the 15:54 check can say, did not.

## What it affects

- **The next launch is not affected.** `make launch-game` runs `pkill -f
  "Metalstorm.exe"` before starting the game, which matches every wrapper above by its
  command line.
- **A process count reads as a running game.** `pgrep -fc "[M]etalstorm.exe"` returned
  6 with no game running. The iterate skill's pre-flight does not rely on that count,
  but a person or script reading it would conclude the game is still up.
- **Resource cost** is small (idle processes), but a `wineserver` and a Proton wrapper
  left per session would accumulate if the next launch did not clear them.
- **Unknown:** whether starting the game from Heroic while these are alive for the same
  prefix causes trouble.

## Not caused by code review 018

The game-shutdown code (`wingman/game_shutdown.py`) changed on 2026-09-27 only in how a
failing presence scan is logged (CR-018-17). `find_game_pids()` matches the kernel `comm`
name, so it targets only the game binary, never the wrappers, before and after that
change. It found exactly one process at 18:35:22, as at 10:54:58 and 15:17:58.

## Hypotheses, none tested

- **Inferred:** the wrappers wait on the `wineserver`, and the `wineserver` waits for
  its last Wine client to exit. Something inside the prefix, most likely `umu.exe`, the
  Wine-side parent of `Metalstorm.exe`, did not exit when its child was killed. The
  18:35 process list is consistent with this and does not show it.
- **Assumed:** session length matters (a leak or a stuck child accumulating over hours).
  The 15:17 exit, after 3.5 hours, argues against a simple length effect, but its timing
  was not recorded.

## What to capture next time

If wrappers are still running a minute after `Game shutdown: … closed`, before stopping
them:

```bash
ps -eo pid,ppid,etime,stat,args --forest | grep -B2 -A12 "[u]mu-run"   # the whole launch tree
pgrep -af "\.exe"                                                      # every Wine process still alive
ls -l /proc/$(pgrep -f "[w]ineserver" | head -1)/fd | wc -l             # wineserver client connections
```

Then wait 10 minutes and check again, to learn whether they ever exit by themselves.

## Possible fixes, not chosen

- **Close the whole launch tree.** `close_game()` would also SIGTERM the `umu-run` process
  tree that started the game, if it is still alive a few seconds after the game closes.
  It owns that launch, so this stays within ADR 094's "close MetalStorm" intent.
- **Stop the prefix's `wineserver`** (`wineserver -k` with the matching Proton build and
  `WINEPREFIX`) after the game closes.

Either needs the capture above first, so the fix addresses the process that actually
holds the prefix open.

## Disposition

Open. Watch each session end for `Game shutdown: … closed` followed by wrappers still
running a minute later, and add a row to the table above.

## References

- ADR 094 — finish the round, then exit (`z`)
- ADR 105 — close the nested display when the game exits
- `wingman/game_shutdown.py` — `close_game`, `find_game_pids`
- `Makefile` — `launch-game`, which clears leftover wrappers before each launch
