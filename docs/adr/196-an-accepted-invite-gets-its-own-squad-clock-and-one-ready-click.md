# ADR 196 — An accepted invite gets its own squad clock and one READY click

| Status   | Date       | Wingman Version |
|----------|------------|-----------------|
| Draft    | 2026-11-19 | 1.9.5           |

Builds on [ADR 084](084-stall-screen-recovery-actions.md) (stall recovery and
the red X squad leave), which is Accepted and is not edited.

## Decision

- **D1. Accepting an invite restarts the squad clock.** `note_invite_accepted()`
  clears `_squad_lobby_since` and the UNREADY dwell when main accepts an invite.
  A new squad gets its own `stall_recovery.squad_no_battle_s` (900 s). It logs
  `INVITED accepted — squad clock restarted` when an old clock was running.
- **D2. The red X is found where it is drawn.** `locate_red_x()` searches the
  STALL_MULTI_PLAYER crop's column, between `stall_recovery.red_x_search_y`
  (0.12 to 0.75 of the height), for the X's saturated red. It centres a crop of
  the calibrated size on the tallest run of red rows. OCR still has to read `X`
  in that crop before anything is clicked, and the click goes to that crop
  (`red_x_crop()`).
- **D3. One READY click per accept.** Main's post-accept READY click goes
  through `claim_lobby_click()`. That returns False if PLAY/READY was clicked
  in the last `LOBBY_PLAY_RETRY_S` (10 s), and otherwise stamps the cooldown
  the lobby quick-scan already honours. Whichever path clicks first owns the
  click, and the other stands down. A click that missed is still retried after
  10 s. Entering GAME_LOBBY no longer clears a stamp under 10 s old. The
  first live run of D3 (18:33) showed why: GAME_UNKNOWN → GAME_LOBBY 0.8 s
  after the invite's click wiped the stamp.
- **D3a. The invite never clicks PLAY.** 1.5 s after ACCEPT the frame can
  still show the solo lobby's PLAY. Clicking it could start a queue alone, so
  the invite handler clicks only READY. The quick-scan clicks READY once the
  squad lobby is drawn.
- **D4. The UNREADY dwell leaves a squad only from an unclassified state.**
  The ADR 084 `unready_dwell_s` gate (160 s) now opens only in
  `STALL_ACTION_STATES` (GAME_UNKNOWN, GAME_STARTING_STALLED). That is the
  case ADR 084 built it for: UNREADY blocks classification. A squad lobby
  wingman has classified is left by the `squad_no_battle_s` gate (15 min,
  started by the first READY or UNREADY read). On 2026-08-19, before that gate
  existed, the dwell was the only way out of a classified lobby.
- **D5. "Matchmaking in progress. Unable to join." is dismissed with OK.** An
  invite accepted while the inviter's squad is queueing raises this dialog.
  It is the CREATION_FAILED dialog with a one-line message, so its OK sits
  30 px higher. A new popup crop, `UNABLE_TO_JOIN`, covers that OK. It is
  scanned in GAME_LOBBY next to CREATION_FAILED.
- **D6. A lobby blackout that shows the battle HUD becomes GAME_STARTING.**
  Once a GAME_LOBBY blackout is 5 s old (one popup pass has had its chance),
  the HEALTH and ALTITUDE_SPEED crops are read once a second. Two reads in a
  row with ADR 189 HUD evidence (`battle_hud_evidence`: health at the spawn
  floor plus speed or altitude, or speed and altitude together) fire
  `cancel_detected`, GAME_LOBBY → GAME_STARTING. GAME_STARTING's own probe
  confirms the HUD and starts the mission. Before this, nothing but the
  operator's `u`/`o` led from GAME_LOBBY into a battle. A dialog blackout has
  no speed or altitude, so it stays in GAME_LOBBY with its popup and ESC
  handling unchanged.
- **D7. The reward crate is opened from a lobby blackout, whatever it holds.**
  `STALL_PARTS_CRATE` (Anomaly 005) now also reads `SILVER` on the reward card,
  and it joins the GAME_LOBBY blackout gate next to STALL_PROFILE and
  STALL_INVITE_PLAYERS. Its action is unchanged: one click on the crate's `?`,
  which opens a reward already owned and commits nothing.
- **D8. "Unable to join" is a refused READY, not matchmaking.** When the
  `UNABLE_TO_JOIN` popup is read, GAME_WAITING goes back to GAME_LOBBY for
  the next 15 s (`waiting_timeout`) instead of letting QUEUE_FALLBACK take the
  dimmed lobby behind the dialog for a queue. The lobby quick-scan then holds
  PLAY and READY for 30 s, and tries again after. A PLAY or READY click clears
  the refusal it follows, so only a dialog that answers this click sends
  GAME_WAITING back. The 15-min squad limit (D1, D4) still leaves a squad
  that never plays.
  - **Rev 1 (2026-11-20).** The first version held READY only, for 60 s, and
    kept the refusal for 15 s whatever was clicked after it. Its first live
    trigger was a **solo** PLAY (02:30): the dialog is not a squad-only one.
    PLAY was re-clicked every few seconds, and the PLAY at 02:30:16 was sent
    back 0.5 s after the click, on the 02:30:12 refusal of the click before.
- **D9. The daily event reward is collected.** A new popup crop,
  `EVENT_COLLECT`, reads `COLLECT` across the event window's whole button
  row, since the orange button sits under whichever day is due. The click
  goes to the orange button found in the row (`locate_collect_button`), not
  to the row's centre. COLLECT takes a reward already owned, like REVEAL_ALL,
  and the crate it opens is D7's.
- **D10. REVEAL_ALL's second click runs wherever its first could.** The
  second click, 3 s after the first (ADR 026), was gated on GAME_LOBBY, while
  the first runs in every popup-dismiss state (ADR 074). It now uses the same
  `POPUP_DISMISS_STATES`.

## Consequences

- A squad that never launches is still left after 15 min, measured from the
  latest accept instead of from the first squad of the session.
- The red X crop no longer has to be recalibrated when an event banner or a
  third member moves the PARTY panel.
- The accept no longer flips READY on and straight back off.

## Evidence

**D1 and D2: 2026-11-19, acct1 (`logs/wingman_20261119_103123.log`).**

```
09:11:50 popup 'INVITED' detected            (squad 1, never launched)
10:14:25 popup 'INVITED' detected            (squad 2)
10:16:19 READY visible but this squad has produced no battle for 64 min — not readying up, leaving it via the red X
10:16-10:31 Stall recovery: 'STALL_MULTI_PLAYER' not found   (every scan)
10:31:17 LIVENESS GUARD hard limit: no progress for 900s — ending the session
```

The 10:30 frame (`blackout_20261119_103030_stuck30s.png`) has the X at y 375.
The calibrated crop covers y 285–315: the 3RD YEAR ANNIVERSARY EVENT banner
pushed the PARTY panel down. Real EasyOCR on three frames:

| Frame | X centre (y) | Calibrated crop | Located crop |
|---|---|---|---|
| 2026-11-02 calibration | 300 | X | X |
| 2026-11-19 10:30 (banner) | 375 | not found | X |
| 2026-11-19 10:14 (third member) | 427 | not found | X |

**D3: 13 accepted invites in the November logs.** In 7 of them, main's READY
click and the quick-scan's came 1–3 s apart (on 11-11 the quick-scan clicked
first). In 6 of those 7, the player sat un-readied until the GAME_WAITING
missed-click re-click 13–16 s later. From the 18:12 acct2 session:

```
18:18:56 popup 'INVITED' detected (text='ACCEPT')
18:18:59 INVITED accepted — clicking READY
18:19:02 Lobby quick-scan: READY detected (text='READY') — clicking
18:19:08 Analyzer: 'READY' detected in READY crop          (un-readied)
18:19:13 Clicking READY                                     (missed-click re-click)
18:19:21 UNREADY detected (text='WAITINGCLICKTOUNREADY')
```

The later READY / WAITING cycles in that session (18:19:31, 18:20:03) came
with no wingman click in between. The party, not wingman, cleared the ready
state there, and wingman readied up again each time.

**D3 rev, D3a, D4: 2026-11-19 18:33 session (operator-run, with D1 to D3).**

```
18:33:40     popup 'INVITED' detected
18:33:41.953 INVITED accepted — clicking PLAY              (solo-lobby frame; claim stamped)
18:33:42.743 GAME_UNKNOWN → GAME_LOBBY                     (on_enter_GAME_LOBBY cleared the stamp)
18:33:43.523 Lobby quick-scan: READY detected — clicking   (second click, 1.6 s later)
18:33:58     UNREADY detected … (0s)                        (dwell starts)
             GAME_WAITING → GAME_STARTING → GAME_LOBBY every ~25 s (the leader queueing)
18:36:38     UNREADY detected … (160s)
18:36:39     Stall recovery: 'STALL_MULTI_PLAYER' detected (text='X', state=GAME_WAITING)
18:36:41     clicking LEAVE                                 (squad left 3 min after the accept)
```

D2 worked at the calibrated position: the located crop read the X and the
click landed at (1731, 300).

**D5: 2026-11-19 19:10 session (operator-run, operator's `v` screenshot
`screenshot_20261119_191726.png`).**

```
19:16:35 popup 'INVITED' detected → clicking INVITED
19:16:36 … 19:18:42 Lobby quick-scan: no lobby crops detected  (every pass; CREATION_FAILED not found)
19:17:10 ADR074 anomaly: GAME_LOBBY blackout stuck for 31s (no calibrated popup crop matched)
19:18:42 GAME_LOBBY escape loop: blackout 135s past the ESC ceiling — pressing ESC anyway (ADR 093)
19:18:53 Lobby quick-scan: PLAY detected — clicking
```

| Dialog | OK button rows | CREATION_FAILED crop (real OCR) | UNABLE_TO_JOIN crop (real OCR) |
|---|---|---|---|
| Match Creation Failed (2 lines) | 595–660 | OK | not found |
| Unable to join, 19:17:26 (1 line) | 565–631 | not found | OK |
| Unable to join, 19:17:10 anomaly frame | 565–631 | not found | OK |
| PLAY lobby; red X lobby (10:30) | none | not found | not found |

**D6: 2026-11-19 20:02 session (operator-run; `v` screenshot
`screenshot_20261119_201120.png`, the in-match scoreboard at 2m 0s, KF-21
0/2/0).**

```
20:08:40-45  READY visible in GAME_STARTING (1/6 … 5/6 reads)
20:08:47     READY still visible after 6 reads — the match never started, returning to GAME_LOBBY (ADR 102)
20:08:50 …   Click-to OCR re-enabled: GAME_LOBBY blackout           (match intro "TEAM ALPHA" on screen)
20:09:33     ADR074 anomaly: GAME_LOBBY blackout stuck for 31s      (blackout_20261119_200933_stuck30s.png)
20:10:36     BOUNDARY: dist=0.292                                    (the battle minimap, read in GAME_LOBBY)
20:11:02     blackout 132s past the ESC ceiling — pressing ESC anyway (opened the scoreboard)
20:11:24     'u' key pressed - starting the configured mission (acs, state=GAME_LOBBY)
```

The round ran 2 min 37 s unflown, with 2 deaths. Why the ADR 102 recheck read
READY six times while the squad's match was loading is not known: no frame was
saved during those reads. D6 is the backstop for that recheck firing too
early. The recheck itself is unchanged.

**D7: 2026-11-19 04:50 session (`logs/wingman_20261119_052926.log`).**

```
05:14:21     Lobby quick-scan: popup 'FINAL_CONTINUE' detected → clicked; GAME_LOBBY
05:15:00     ADR074 anomaly: GAME_LOBBY blackout stuck for 31s   (blackout_20261119_051459_stuck31s.png: "+300 SILVER", crate '?')
05:16:29 …   blackout 133s past the ESC ceiling — pressing ESC anyway   (every 45 s, 18 times, no effect)
05:19:15     LIVENESS GUARD: no FSM state change and no OCR for 300s
05:29:15     LIVENESS GUARD hard limit: no progress for 901s — ending the session
```

| Frame | STALL_PARTS_CRATE label, real OCR, text [PARTS, UNIVERSAL, SILVER] | `?` crop white fraction |
|---|---|---|
| 05:14:59 and 05:23:00 crate frames | `+300SILVER` | 0.41 |
| STALL_PARTS_CRATE.png (Anomaly 005) | `+100UNIVERSALPARTS` | 0.41 |
| SILVER popup, PLAY lobby, red-X lobby, match intro | not found | 0.00 |

**D8: 2026-11-19 `make r` session started 20:41 (with D1 to D7).**

```
22:00:23     INVITED accepted — clicking READY
22:00:30     popup 'UNABLE_TO_JOIN' detected → OK          (D5 working)
22:00:34     READY detected — clicking; GAME_LOBBY → GAME_WAITING
22:00:41/47  popup 'UNABLE_TO_JOIN' detected → OK          (each READY raised it again)
22:00:47     GAME_WAITING confirmed via QUEUE_FALLBACK (12.0s) → GAME_STARTING
22:03:18     game_starting timed out after 150s without the HUD → STALLED
22:03:19     popup 'UNABLE_TO_JOIN' detected → OK; → GAME_UNKNOWN → GAME_LOBBY
22:03:26     READY detected — clicking                        (the loop again)
22:03:40     QUEUE_FALLBACK → GAME_STARTING
```

About 2.7 min a lap, and no lap could reach a battle.

**D8 rev 1: 2026-11-20 02:30, solo lobby (`make r` session started 22:35, D8
first version).**

```
02:30:05     PLAY detected — clicking; GAME_LOBBY → GAME_WAITING
02:30:12     popup 'UNABLE_TO_JOIN' detected → OK
02:30:14     GAME_WAITING: the READY was refused — back to GAME_LOBBY   (D8 working)
02:30:16     PLAY detected — clicking                                    (2.5 s later: no hold for PLAY)
02:30:17     GAME_WAITING: the READY was refused — back to GAME_LOBBY   (0.5 s after the click: stale refusal)
02:30:23-44  popup 'UNABLE_TO_JOIN' detected                             (every 5 s)
02:30:49     PLAY detected — clicking
02:30:53     Lobby quick-scan: CANCEL detected → GAME_STARTING           (accepted, a real queue)
```

Refused for at least 33 s, accepted by 44 s. No 150 s GAME_STARTING timeout,
unlike 22:00: the first version already broke the loop D8 is for.

**D9: 2026-11-20 02:44 (`make r` session started 22:35).**

```
02:43:57     GAME_END_B → GAME_LOBBY
02:44:45     ADR074 anomaly: GAME_LOBBY blackout stuck for 31s   (blackout_20261120_024445_stuck31s.png:
             3RD YEAR ANNIVERSARY EVENT, Day 6 HUGE CRATE, COLLECT)
02:46:12     blackout 133s past the ESC ceiling — pressing ESC anyway   (closed the window, crate not collected)
02:46:18     PLAY detected — clicking
03:00:23     ADR074 anomaly: GAME_LOBBY blackout stuck for 31s   (the same window, Day 6 COLLECT, after the next round)
```

ESC closes the window without collecting, so it comes back after every round:
about 2 min 15 s a round until the reward is taken.

| Frame | EVENT_COLLECT row, real OCR | Button found by colour |
|---|---|---|
| 02:44:45, Day 6 | `COLLECT` | x 1203–1332, y 836–864 |
| the same, button moved one tile (Day 7) | `COLLECT` | 154 px right |
| PLAY lobby, red-X lobby, silver crate, REVEAL_ALL | not found | — |

**D9 and D10: 2026-11-20 03:20, the first minute of a `make r` session with D9.**

```
03:20:07     UNKNOWN → GAME_UNKNOWN                      (startup)
03:20:23     popup 'EVENT_COLLECT' detected → Clicking EVENT_COLLECT at (1268, 850)   (D9 working)
03:20:28     popup 'REVEAL_ALL' detected → clicked        (the HUGE CRATE)
03:20:31     REVEAL_ALL second click suppressed — state is GameState.GAME_UNKNOWN
03:20:36     STALL_CHOOSE_REWARDS → middle reward, SELECT ONE
03:20:40 …   ADR074 anomaly: GAME_UNKNOWN stuck            (unknown_20261120_032040_stuck32s.png:
             the crate's nine-card reward summary, no button)
03:21:38 …   GAME_UNKNOWN startup classification timeout   (every second)
```

November logs: the second REVEAL_ALL click ran 114 times, all in GAME_LOBBY,
and was suppressed in 8 sessions. Six of the 8 reached the next state in
10–31 s, one took 2 min 17 s, and one logged none.

## Validation

- [ ] D1: a session that accepts an invite after an earlier squad logs
      `squad clock restarted`, and does not leave the new squad before 15 min.
- [ ] D2: a squad leave on a shifted PARTY panel logs
      `Stall recovery: 'STALL_MULTI_PLAYER' detected` and reaches PLAY.
- [x] D3: accepts are followed by exactly one READY click, or by
      `already clicked by the lobby scan` or `PLAY still on screen`, with no
      missed-click re-click. Seen once, 2026-11-19 19:24 (session started
      19:10): quick-scan READY at 19:24:25.548, then `READY already clicked by
      the lobby scan in the last 10 s, not clicking it again` at 19:24:26.043,
      then GAME_WAITING. 20:01 (session started 19:52), the other order: main's
      `INVITED accepted — clicking READY` at 20:01:11.916, no quick-scan
      click, GAME_WAITING at 20:01:17. Two accepts, one in each order; more
      needed before calling the rate.
- [ ] D4: a squad whose leader is slow stays past 160 s in GAME_WAITING, and
      is left only after `no battle for 15 min`.
- [x] D6: a GAME_LOBBY blackout over a live round logs `Lobby blackout … is
      a battle`, then `HUD confirmed in GAME_STARTING` and the mission start,
      with no ESC-ceiling press. 2026-11-20 03:15 (`make r` session started
      22:35): the D9 event window sat over the lobby from 03:14:19, and a queue
      started under it with no wingman click (the 03:15:04 frame shows the red
      CANCEL behind the window where PLAY had been). HUD read at 03:15:41
      (health 175, 893 kph, 1149 m) and 03:15:44 (962 kph), handoff at
      03:15:44, `HUD confirmed in GAME_STARTING` and `mission 'acs' started`
      at 03:15:45, about 4 s after the HUD first showed. (20:08, before D6: the
      round ran 2 min 37 s unflown, until the operator's `u`.)
- [ ] D8: after `UNABLE_TO_JOIN`, `the READY was refused … back to
      GAME_LOBBY` and `holding READY`, with no QUEUE_FALLBACK confirm and no
      150 s GAME_STARTING timeout in between.
- [x] D9: the event window logs `popup 'EVENT_COLLECT' detected` and a click
      on the button, with no ESC-ceiling press after it. 2026-11-20 03:20:23,
      16 s into a session: detected and clicked at (1268, 850), the Day 6
      button; the HUGE CRATE opened (REVEAL_ALL at 03:20:28).
      **Open:** 06:33:33 (Day 7, operator session started 06:33:13): `COLLECT`
      read but `its button not found`; the window was gone by 06:33:35 (PLAY
      clicked, no blackout). No frame was kept, so why the orange search
      missed is not known (inferred: the Day 7 button is another colour, or
      the window was fading). A miss now saves its frame
      (`collect_miss_*.png` in test_screenshots/unknown_anomalies).
- [x] D10: the second REVEAL_ALL click runs outside GAME_LOBBY. 2026-11-20
      03:44:38 (session started 03:44:14, with D10): `REVEAL_ALL second click
      after 3s delay` in GAME_UNKNOWN, then GAME_UNKNOWN → GAME_LOBBY at
      03:44:43 and PLAY. The session before it, without D10, sat on the
      reward summary from 03:20:40 until the 600 s startup stall ended it at
      03:30:02.
- [ ] D7: a crate over the lobby logs `Stall recovery: 'STALL_PARTS_CRATE'
      detected` about 15 s into the blackout, and the lobby comes back.
- [x] D5: the dialog logs `popup 'UNABLE_TO_JOIN' detected` within one popup
      pass, with no `blackout stuck` anomaly after it. 2026-11-19 22:00 (`make r`
      session started 20:41): invite accepted 22:00:21, READY clicked 22:00:23,
      `UNABLE_TO_JOIN` detected and OK clicked at (959, 598) at 22:00:30, again
      at 22:00:41 and 22:00:47 after the next READY click; GAME_STARTING at
      22:00:47, no blackout anomaly. (19:16, before D5: 2 min 6 s to the ESC
      ceiling.) Each READY click into a squad that is already queueing raises
      the dialog again; three in 17 s here, then the squad's queue took over.

Tests: `tests/test_stall_recovery.py` (`TestInviteRestartsTheSquadClock`,
`TestRedXLocator`, `TestOneReadyClickAfterAnInvite`,
`TestUnreadyDwellOnlyWhenUnclassified`, `TestLobbyEntryKeepsARecentClick`).
