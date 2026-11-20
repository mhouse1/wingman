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
      then GAME_WAITING. One accept; more needed before calling the rate.
- [ ] D4: a squad whose leader is slow stays past 160 s in GAME_WAITING, and
      is left only after `no battle for 15 min`.
- [ ] D5: the dialog logs `popup 'UNABLE_TO_JOIN' detected` within one popup
      pass, with no `blackout stuck` anomaly after it.

Tests: `tests/test_stall_recovery.py` (`TestInviteRestartsTheSquadClock`,
`TestRedXLocator`, `TestOneReadyClickAfterAnInvite`,
`TestUnreadyDwellOnlyWhenUnclassified`, `TestLobbyEntryKeepsARecentClick`).
