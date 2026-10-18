# Anomaly 012 — The Lobby Walk-Back Abandons Matches That Are Starting

| Status | Date       | Wingman Version |
|--------|------------|-----------------|
| Draft  | 2026-10-18 | 1.9.2           |

## Summary

**Status: cause measured 2026-10-18; fix in [ADR 161](../adr/161-the-lobby-walk-back-waits-six-reads.md),
live validation pending.** [ADR 102](../adr/102-lobby-recheck-from-game-starting.md) walks
`GAME_STARTING` back to `GAME_LOBBY` when PLAY or READY is read three times in a row, on the
premise that a match that is starting never shows the lobby button. It does: a found match can
show the button for one to three reads before the loading screen. When the third read lands in
that flash, wingman returns to the lobby, the loading screen hides every lobby crop, and the match
plays with nobody flying it until the round ends. It happened three times on 2026-10-18, between
08:09 and 12:55, on three accounts, each costing a round of about six minutes. All numbers below are
measured from the logs named unless labelled.

## The lost matches

| When (2026-10-18) | Account | Button, reads | Lobby crops gone | What followed |
|---|---|---|---|---|
| 08:09:13 | account 1 | PLAY, 3 (08:09:11.5 to 08:09:13.8) | 08:09:16.6 | battle at 5:22 of 6:00 in the 08:09:59 frame; ESC at 08:11:29 opened the scoreboard; `LIVENESS GUARD` 08:14:14; back at 08:15:47 (`logs/wingman_20261018_082111.log`) |
| 09:42:39 | main | READY, 3 (4 s into `GAME_STARTING`) | +2.8 s | 6 min of lobby blackout to `GAME_WAITING` at 09:48:54; `LIVENESS GUARD` 09:47:39 (`logs/wingman_20261018_124949.log`) |
| 12:55:09 | account 2 | READY, 3 (12:55:06.5 to 12:55:09.3), after a PLAY walk-back at 12:54:58 | 12:55:10 | the 12:56:30 frame shows an air-superiority battle at 34 to 48 with the aircraft at 550 KPH, nobody flying; the operator's `u` at 12:55:56 started the mission (`wingman.log` of the 12:54 session, rotated into `logs/` by the next start) |

Two more walk-backs had the button gone on the next read and ended with the game closed
(2026-10-15 18:25:21; 2026-10-18 12:49:24, inferred to be the operator), and one where the log
ended (2026-10-16 16:35:07). The account names come from the pilot name in each session's frames:
MOBIUS0 for account 1, MOBIUS1 for the main account, MOBIUS2 for account 2.

The 08:09 sequence, one line a read:

```
08:09:11,553 Lobby quick-scan: PLAY visible in GAME_STARTING (1/3 reads)
08:09:12,684 Lobby quick-scan: PLAY visible in GAME_STARTING (2/3 reads)
08:09:13,803 Lobby quick-scan: PLAY still visible after 3 reads in GAME_STARTING — the match never
             started, returning to GAME_LOBBY (ADR 102)
08:09:16,577 Lobby quick-scan: no lobby crops detected (stalled 0.0s)
08:09:59,955 ADR074 anomaly: GAME_LOBBY blackout stuck for 31s (no calibrated popup crop matched)
```

## How long a starting match shows the button

Every `GAME_STARTING` phase in the 52 logs of 2026-10-15 to 2026-10-18 (1,056 phases), by the
longest run of consecutive PLAY or READY sightings in it:

| Phase ended in | No sighting | 1 read | 2 reads | 3 reads (walked back) |
|---|---|---|---|---|
| `GAME_BATTLE` | 734 | 13 | 8 | — |
| `GAME_LOBBY` | — | — | — | 301 |

A run of three is never seen to reach a battle because three is where the walk-back fires. What
the lobby showed after each of the 301 walk-backs:

| First thing after the walk-back | Walk-backs |
|---|---|
| a PLAY or READY click, an UNREADY read, a READY held by the squad-leave rule, or `GAME_WAITING`: the lobby was real | 269 |
| `no lobby crops detected` first, then the lobby came back and `GAME_WAITING` followed | 26 |
| `no lobby crops detected` on the next read, then a lobby blackout or a jump to battle: the match was lost | 3 |
| `no lobby crops detected` first, then the game was closed or the log ended | 3 |

So a real start shows the button for up to three reads (about 2.2 to 2.8 s, the scan running every
1.1 s), and in every lost match it was gone on the fourth. A failed start, the case ADR 102 was
written for, keeps the button up until it is clicked: in the 268 the lobby path found it and
clicked on the next read.

## Not covered

- What the screen shows during the flash: no frame is saved at a `GAME_STARTING` sighting.
  Saving one at the first sighting would show whether the flash is the lobby itself or a
  transition screen that reuses it.
- The operator's `n` presses at 12:54:56 to 12:55:00 (four, all on `:3`: the 12:55:28
  `XKey[:3]` line counts 9 key presses, 5 of them wingman's) clicked PLAY twice in the lobby
  during the 12:54:58 walk-back. A click on PLAY while a found match loads may itself cancel it
  (not known).
- The squad loop of 2026-10-18 00:57 to 01:05 (a READY walk-back every 20 s for 8 minutes). Its
  last walk-back, 01:05:43, was correct: READY was on screen at 01:05:46 and held by the
  squad-leave rule (`squad has produced no battle for 28 min`). The lobby went dark after the
  escape loop's ESC at 01:06:28, which on the lobby opens Exit to Desktop. A separate question.

## Live check of ADR 161 (account 2, from 2026-10-18 13:39)

Run: `make r2`, wingman pid 3027116 started 13:39:18, log `wingman.log`; code: `012a0e4` plus ADR 161
(`STARTING_PLAY_CONFIRM_READS` 6). Observations as they land, measured from the log:

- 13:39:38 to 13:41:19, a failed start: PLAY in bursts of 2 (to 13:40:19), 2 (13:40:55 to 13:40:56) and
  3 (13:41:03 to 13:41:05), each followed by about 6 s with no sighting, then 6 reads from 13:41:13
  and the walk-back at 13:41:19. It was right: PLAY was clicked at 13:41:22 and `GAME_WAITING`
  followed at 13:41:23. The three-read rule would have walked back at 13:41:05, 14 s sooner, so a real
  failure does not always keep its button up, and the longer streak can cost more than the 3.3 s
  ADR 161 first estimated.
- 13:41:35.9 to 13:41:47.6, the button changing with no click from wingman: `CANCEL detected in
  GAME_WAITING` at 13:41:35.9, PLAY on 6 reads from 13:41:38.3 (walk-back at 13:41:43.9), then
  `CANCEL detected` in `GAME_LOBBY` at 13:41:46.5 and back to `GAME_STARTING`. The 13:42:25 frame shows
  the search running: CANCEL, `0M 38S (ESTIMATED 1M 13S)`, `1 / 1 READY`, and the party panel with
  account 2 alone (`PARTY (1/5)`). On that frame the real OCR path reads nothing in the PLAY crop and
  CANCEL in the CANCEL crop. So the search ended and began again by itself within 10 s, or something
  outside wingman clicked (inferred, not separated). In this flow the walk-back costs about 3 s in
  the lobby before CANCEL sends it back.
- 14:23:49.9 to 14:25:24, a start the three-read rule would have walked back, reaching a battle: the
  `INVITED` popup was clicked at 14:23:30 (`accept_invite`), READY at 14:23:35.6, `GAME_STARTING` from
  14:23:48.8; READY on 5 reads (14:23:49.9 to 14:23:55.4), gone at 14:23:56.6; `Good Luck` at
  14:25:10.6, health 100 confirmed at 14:25:23.5, `GAME_BATTLE` at 14:25:24.8. The third read, where
  the old rule fired, was 14:23:52.7. So a starting match can show its button for five reads, two
  more than the longest flash in the 2026-10-15 to 10-18 logs, which the three-read rule truncated.

