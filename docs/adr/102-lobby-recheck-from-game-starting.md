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

## References

- ADR 074 — `POPUP_DISMISS_STATES` and why `GAME_UNKNOWN` joined it; the set
  this decision deliberately does not reuse
- ADR 087 — click-to OCR re-enable during a lobby blackout, visible in the same
  log extract
- ADR 094 — the round-start suppressor the lobby click path already honours
- `wingman/analyzer.py` — `_lobby_quick_scan`, the scanner extended here
