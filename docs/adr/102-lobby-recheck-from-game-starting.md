# ADR 102 — Lobby Recheck from GAME_STARTING

| Status | Date       | Wingman Version |
|--------|------------|-----------------|
| Draft  | 2026-09-01 | 1.8.8           |

## Context

On 2026-09-01 a session spent 141.7 s convinced a match was starting while the
game sat at the lobby with the PLAY button on screen.

```
06:47:53,684  Lobby quick-scan: PLAY detected (text='PLAY') — clicking
06:47:53,957  Game state: GAME_LOBBY → GAME_WAITING
06:47:54,914  Lobby quick-scan: CANCEL detected in GAME_WAITING → GAME_STARTING
06:47:54,914  Controller: game_starting loop started - pressing 'u' key every 5s
...
06:50:26,669  GAME_STARTING health probe summary: 94 attempts over 141.7s
              — NO raw read at any point
06:50:26,669  FSM: GAME_STARTING → GAME_STARTING_STALLED
```

Ninety-four health probes, every one "no digits". Thirty 'u' presses into a
lobby. The session ended with `Missions started: 0` after 3m 26s.

The FSM had two exits from `GAME_STARTING`: `good_luck_detected` to
`GAME_BATTLE`, and `starting_timeout` to `GAME_STARTING_STALLED` after 150 s.
Neither looks at whether the premise still holds.

### Why nothing noticed

The evidence was on screen the entire time — PLAY, in a calibrated crop, in the
frame the quick-scan already holds. The scanner never looked, because
`GAME_STARTING` is not in `POPUP_DISMISS_STATES` and the loop skips any state
outside it before reading a single crop.

`GAME_STARTING` is the one state that asserts something about the screen without
ever rechecking it. `GAME_WAITING` re-reads CANCEL. `GAME_LOBBY` re-reads
PLAY/READY. `GAME_UNKNOWN` runs classification. `GAME_STARTING` presses a key on
a timer and waits for a banner that, if the match never began, is never coming.

## Decision

**D1. Re-read PLAY while the FSM believes a match is starting.** A new
`LOBBY_RECHECK_STATES = (GAME_STARTING,)` lets the quick-scan through its outer
gate and scans exactly one crop there.

**D2. Keep it separate from `POPUP_DISMISS_STATES`.** Reusing the popup set
would have been one line, and would also have granted popup *dismissal* during a
genuine match start — clicking dialogs on a screen this ADR has no business
touching. The recheck reads one crop and clicks nothing.

**D3. Walk the state back; do not click from `GAME_STARTING`.** On confirmation
the scanner fires `starting_play_visible` to `GAME_LOBBY` and stops. The
ordinary lobby path then clicks PLAY on its next pass. So a misfire costs a
state transition, never a click into a match that is genuinely starting.

**D4. A scoped transition, not another wildcard.** `starting_play_visible` has
`GAME_STARTING` as its only source. `manual_reset` already provides
anywhere-to-lobby; a second wildcard would let a stray PLAY read reset the FSM
from `GAME_BATTLE`, with an aircraft in flight.

**D5. Three consecutive agreeing reads.** The quick-scan runs at roughly a 1 s
cadence, so this is ~3 s of PLAY continuously visible — 50x faster than the
150 s timeout, while a single stray read cannot abort a match that really is
starting. The streak resets when PLAY disappears and whenever the scan observes
a state outside `LOBBY_RECHECK_STATES`, so it cannot span two separate stalls.

**D6. Clear the PLAY-click suppression on the way back.** `GAME_LOBBY` suppresses
a PLAY click for 60 s after the last one, so that a click which worked is not
repeated. Reaching this point is the proof that it did not work. Without the
reset the FSM would return to the lobby and then sit there for the remainder of
the window — trading a 150 s stall for a 60 s one.

## Consequences

A match that fails to start is recovered in about three seconds instead of a
hundred and fifty. The 150 s timeout stays as the backstop for stalls where PLAY
is *not* visible, which this ADR does not address.

`GAME_STARTING` now costs one extra crop OCR per scan cycle. It is one crop on a
frame already captured, in a state that was otherwise doing nothing but pressing
'u' on a timer.

The failure mode moves rather than disappearing: a persistent PLAY misread
during a real match start would now bounce the FSM to `GAME_LOBBY` about three
seconds in. The debounce and the read-only scope bound the cost, and the ordinary
lobby path recovers from a wrong lobby entry.

## Alternatives considered

**Shorten the 150 s timeout.** Treats the symptom. A shorter timeout still waits
blind, and `GAME_STARTING_STALLED` then has to re-derive the state that PLAY
would have told it directly. The timeout is also load-bearing for genuinely slow
matchmaking.

**Add `GAME_STARTING` to `POPUP_DISMISS_STATES`.** One line, and it would have
worked. Rejected because it grants dismissal clicks as a side effect, which is a
much larger behaviour change than the problem needs.

**Let the health probe conclude it.** 94 probes returning "no digits" is itself
strong evidence the aircraft does not exist. But that is an inference from an
absence, and the absence has other causes — a loading screen, an OCR stall.
PLAY being visible is positive evidence about the actual screen.

## Validation

- **V1.** With PLAY visible in `GAME_STARTING` for three consecutive scans, the
  FSM returns to `GAME_LOBBY` and PLAY is clicked on the following pass.
- **V2.** One or two PLAY reads do not move the FSM.
- **V3.** A PLAY read in any battle state cannot fire the transition.
- **V4.** No popup is dismissed from `GAME_STARTING`.
- **V5.** The 150 s timeout still fires for a stall with no PLAY on screen.
- **V6.** The click suppression does not delay the recovered PLAY click.
- **V7 — live.** A session shows the walk-back followed by a started mission.
  Not yet observed; this ADR is Draft until it is.

## Revision 2 — 2026-09-25 (wingman 1.8.11)

The 09:09 to 10:59 run (one session, 18 rounds) and every September log show two defects on this path that the ADR did not cover, and one premise of the ADR that was tested and kept. Measured unless labelled.

### Evidence

**E1. A mission launched into the lobby (10:54:29).**

```
10:53:38  GAME_WAITING confirmed via QUEUE_FALLBACK (10.5s) — matchmaking confirmed → GAME_STARTING
10:54:00  GAME_STARTING health probe #9 (+12.5s since armed): raw=0
10:54:28  Lobby quick-scan: PLAY visible in GAME_STARTING (1/3 reads)
10:54:29  Lobby quick-scan: PLAY visible in GAME_STARTING (2/3 reads)
10:54:29  GAME_STARTING health probe #28 (+41.0s since armed): raw=7
10:54:29  Analyzer: health 7 confirmed in GAME_STARTING (+41.0s since armed) → game_battle_alive=True
10:54:29  Controller: mission 'su30' started → GAME_BATTLE
10:54:54  Controller: 'm' pressed twice during GAME_BATTLE — forcing GAME_LOBBY
```

The HUD frame written at 10:54 (`tests/test-output/live_hud.png`) shows the hangar with PLAY on screen under the overlay text `GAME_BATTLE` and `HP 7`. `_confirm_health_value` confirms when two of the last three reads agree within `value_confirm_tolerance` (15), and |0 − 7| = 7, so two different lobby digit fragments confirmed each other. The third PLAY read, about a second later, would have walked the FSM back (inferred: the streak needs three in a row and the reads were arriving 1.1 s apart); the health launch won the race. After the forced return the quick-scan logged `no lobby crops detected` for four minutes and the run was ended by hand.

**E2. Which health values launch.** Across the September logs the probe confirmed 1,724 values (archive duplicates included): 1,718 at 160 to 312 (203: 819, 250: 816, 160: 68, 201: 11, 208: 2, 312: 1, 163: 1) and 6 at 7. Four of the six coincided with a loading screen and were harmless: the mission restarted when the real health arrived about 45 s later (2026-09-25 06:42:35, 07:28:00, 08:15:14; 2026-09-14 06:53:56). Two coincided with the lobby: 08:54:06 (PLAY seen 3.9 s earlier) and 10:54:29.

**E3. The READY crop misses the squad lobby's button.** The 09:16, 09:23 and 09:28 rounds dwelt in `GAME_LOBBY` for 142, 194 and 127 s (a normal round takes 4 to 13 s), logging `no lobby crops detected` and `GAME_LOBBY blackout — the forced state may be wrong` while the operator pressed `m` by hand (the other two dwells show the same signature; only the first has a screenshot: inferred that all three were the squad layout). The operator's screenshot from 09:16:41 shows a two-player party and a 257 × 78 px READY button with `0 / 2 READY` above it. Through the real OCR path with the configured crops, READY, PLAY, UNREADY and CANCEL were all `detected=False`; a crop over the button read `'READY'`. The configured READY crop (1534, 1038, 1741, 1093 px) starts 72 px left of the button and covers 53% of its width and 42% of its height. `tests/calibration_map.yaml` has no READY or UNREADY entry and the crop dates from 2026-04-11, so ADR 072's August recalibration never reached it.

**E4. D5 (three consecutive reads) was tested and kept.** 10:24:45 to 10:25:54 logged nine PLAY sightings in 36 s and never three in a row (pairs, then a miss) until the operator forced the lobby. I replayed all 2,838 `GAME_STARTING` phases in the September logs against "three sightings within 10 s": 13 phases had sightings and were left stuck by the streak rule; the window would have fired in 2 of them (saving 56 s on 2026-09-24 06:59 and 28 s on 2026-09-25 10:24) and would have aborted 2 phases that went on to launch a real mission (2026-09-13 20:30:56, launched 39 s later; 2026-09-25 08:54:02, launched 6 s later). A wash, so D5 stands.

### Decisions

**D7. A confirmed health below `STARTING_MIN_CONFIRMED_HEALTH` (20) cannot launch a mission from `GAME_STARTING`.** Applied after ADR 063's recurrence check, and logged when it fires (`… confirmed but below the 20 floor`). No spawn starts below its aircraft's full health, and the two populations in E2 are 20 times apart. The four harmless loading-screen 7s no longer start the mission early, which cost nothing to lose: the mission restarted on the real read anyway. A constant, like `STARTING_PLAY_CONFIRM_READS`.

**D8. The READY crop is the button, and READY-crop text that reads UNREADY is the already-ready state.** The crop is now (1609, 1063, 1860, 1135) px, the measured button inset 3 px, so the click at the crop centre lands mid-button. The button toggles READY and UNREADY and 'UNREADY' contains 'READY', so `_lobby_crop_verdict` maps READY-crop text containing UNREADY onto the existing UNREADY branch (to `GAME_WAITING`, no click). Without it the wider crop would click a second time after 60 s and un-ready the player.

### Alternatives considered

- **A lobby-visible veto on the health launch** (no launch within 5 s of a PLAY sighting). It would have stopped both lobby launches, but it needs new state shared between the quick-scan thread and the OCR pool, and D7 alone stops every observed case (all read 7). Kept in reserve for a lobby launch that reads 20 or more.
- **Windowed streak:** E4.
- **A relative floor** (a fraction of the last spawn health). Aircraft health differs by up to 2× (160 to 312), and an aircraft change would deadlock the fast path; the fixed floor has 20× margin on both sides.

### Not addressed

- The `UNREADY` crop (1490, 1041, 1785, 1088 px) is stale for the same reason. D8 handles the state through the READY crop, but recalibrating the crop itself needs a frame of the new-layout UNREADY button.
- Both stuck phases of the 09:09 run (10:24:45 and 10:53:38) began with `GAME_WAITING → GAME_STARTING` on `QUEUE_FALLBACK` at 19.5 s and 10.5 s with neither CANCEL nor PLAY visible, and the lobby was back 30 to 50 s later. Why matchmaking ended without a match is not known.
- V7 was met before this revision: the walk-back has fired 100 times across the September logs, for example 2026-09-25 06:19:12 `GAME_STARTING → GAME_LOBBY`, PLAY clicked at 06:19:58, `mission 'su30' started` at 06:20:32. The ADR stays Draft until the operator promotes it.

### Verification

- `tests/test_starting_health_floor.py` (13 tests) and `tests/test_lobby_ready_squad_layout.py` (13 tests, two of them real OCR under `-m slow`): 26 passed with the change. Mutation checks: floor disabled fails 4, UNREADY never recognised fails 2, ADR 063 recurrence skipped fails 9, old crop restored fails 4.
- The geometry tests use `tests/fixtures/lobby_ready_squad_br.png`, the bottom-right corner of the 09:16:41 screenshot (the full frame is under the gitignored `tests/test-output/`).
- Gates: `make lint` clean; `make test` 2173 passed, 35 skipped (the first full run had one failure, `test_calibrate_config_writer::test_coord_edit_changes_only_the_coord_lines`: the writer normalizes floats, so the hand-written `0.8380` was a third changed line; written as `0.838`); `make reqs-gate` passed.

### Live check 1 (in progress)

Run: `make rd` at 2026-09-25 11:34, wingman pid 3706157 started 11:35:04, log `wingman.log` (the 09:09 to 10:59 run is `logs/wingman_20260925_105914.log`). Code state: HEAD `8e339a8` plus this revision (`wingman/analyzer.py`, `wingman/config.yaml`; diff hash `5f52318337fda599`). Game UI: no build identifier is logged; the lobby at start was the solo `PLAY` layout, and the squad `READY` layout (a two-player party) is the case D8 is for.

What the run should show, and what would refute it:

- D7: every `confirmed in GAME_STARTING` line reads 160 or above; a `confirmed but below the 20 floor` line means a lobby or loading-screen fragment was caught (expected a few times per session, harmless); no mission started while `PLAY` or `READY` is on screen (`launching mission immediately` followed by `'m' pressed`, or by `GAME_BATTLE → GAME_LOBBY`, refutes it).
- D8: in a squad lobby, `Lobby quick-scan: READY detected` and a click within seconds of entering `GAME_LOBBY`, and no `GAME_LOBBY blackout` stall while the party panel shows `0 / 2 READY`. `READY crop reads UNREADY` after the click means the guard did its job. Two clicks on READY within 60 s, or a dwell of more than 30 s with a READY button on screen, refutes it.
- Not expected to change: the `the match never started` walk-back (D5).

Observations as they land (measured from `wingman.log`):

- 11:43:50: `health 100 confirmed in GAME_STARTING (+11.0s since armed)`. A legitimate launch, lower than any September value (160 to 312), so the aircraft on this account differs from the earlier runs: `Good Luck` at 11:43:40, probe #7 `raw=100` held as unconfirmed, probe #8 `raw=100` confirmed, `GAME_STARTING → GAME_BATTLE` 1.3 s later, in-battle `Health: 100`, and a normal climb (1,024 m at 11:43:56). The floor of 20 passed it, which is the case for a fixed floor over a relative one: no history is needed and aircraft health varies at least from 100 to 312.
- 12:04:09 to 12:05:56: a `GAME_LOBBY` blackout of 1 min 45 s (54 `no lobby crops detected` reads, `ADR093: GAME_LOBBY blackout STILL stuck` at 12:04:51) that is **not** the READY or health defects: the lobby was the solo `PLAY` layout, and a modal, "REDLINE Flight Pass Complete — You have uncollected rewards! [COLLECT]", dimmed it so no lobby crop could read. The round ended at 12:04:05, `FINAL_CONTINUE` was dismissed at 12:04:11, and nothing knows this popup (it is in none of `popup_crop_names`, `_STATE_CROPS` or the config crops). A snapshot of `:3` at 12:05:09 shows it. I clicked COLLECT at 12:05:56 (960, 627) to unblock the run (a reward button on the operator's account; the popup says the rewards were uncollected, so it should not recur until the next pass completes) and wingman read `PLAY` and clicked it at 12:05:59. Not fixed here: a separate defect. The COLLECT button measures 804,594 to 1116,661 px on that frame, saved as `test_screenshots/to_be_added/FLIGHT_PASS_COMPLETE.png` for `make add-crops`; the wiring is a config crop plus two name lists in `analyzer.py`, as for `TAP_HERE_TO_CONTINUE`. Without it, an unattended session idles until the ADR 093 liveness guard, and on 2026-09-25 03:54 that guard logged "ending the session" at 04:09 and the process kept running until 05:33.
- 12:33:38 to 12:34:06: a legitimate start that showed the E4 pattern. `QUEUE_FALLBACK (12.0s)` moved `GAME_WAITING → GAME_STARTING` and `PLAY` was read at 12:33:38.5 (1/3) and 12:33:39.7 (2/3), then never again; the display at 12:34:03 was the black loading screen, `Good Luck` was detected at 12:33:54, and `health 100 confirmed (+18.5s)` launched the mission at 12:34:06. Two sightings 1.2 s apart at the start of `GAME_STARTING` occur in real starts, so a "three sightings within 10 s" rule would have been one frame from aborting it: live support for keeping D5 as it is.
- Interim tally, 11:35 to 13:06 (1 h 32 min, run still going): 14 rounds ended, 15 `GAME_STARTING → GAME_BATTLE`, 6 health confirmations (all 100), 0 `below the 20 floor`, 0 `READY` events (the lobby stayed solo), 0 walk-backs, 0 `m` presses, 0 direct lobby/battle jumps, 0 errors, 0 liveness firings, 1 blackout (the Flight Pass popup, cleared by hand at 12:05:56). This cannot confirm D7: the earlier run had 2 lobby launches in about 35 rounds (a 6% rate), so 15 clean rounds is what a 6% rate produces 40% of the time. It confirms that the floor and the crop change did not disturb 15 healthy starts.
- **13:34:10 to 13:34:21: D7 fired live.** After `QUEUE_FALLBACK (10.5s)` at 13:33:58 the probe read a steady `raw=7` on probes #1 to #9 (+0.5 s to +12.5 s): #1 held as unconfirmed, #2 to #9 (eight lines) `confirmed but below the 20 floor`, then `no digits` (#10 to #25), `Good Luck` at 13:34:34, `raw=100` at 13:34:46 (held as unconfirmed) and the mission launched at 13:34:47.7 on the `Good Luck` timer, with `Health: 100` in battle 0.5 s later. Before this revision probe #2 at 13:34:10.5 confirms 7 and launches the mission about 37 s early, on the loading screen. That is the harmless kind (E2's four loading-screen cases; this is the fifth), so no lobby launch has been caught yet, but it shows the floor holding a launch back until the real start, and that the loading-screen `7` is common. No lobby veto was needed. The floor logs one line per probe while the `7` persists (eight here); that is the price of not dropping reads silently.

## References

- ADR 074 — `POPUP_DISMISS_STATES` and why `GAME_UNKNOWN` joined it; the set
  this decision deliberately does not reuse
- ADR 087 — click-to OCR re-enable during a lobby blackout, visible in the same
  log extract
- ADR 063 — the recurrence filter whose tolerance let two lobby digits confirm
  each other (Revision 2, D7)
- ADR 072 — the August recalibration that left the READY and UNREADY crops
  behind (Revision 2, D8)
- ADR 094 — the round-start suppressor the lobby click path already honours
- `wingman/analyzer.py` — `_lobby_quick_scan`, the scanner extended here
