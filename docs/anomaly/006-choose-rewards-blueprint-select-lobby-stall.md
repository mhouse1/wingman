# Anomaly 006 — "Choose Rewards" Blueprint-Selection Overlay Stalls the Lobby

| Status | Date       | Wingman Version |
|--------|------------|-----------------|
| Draft  | 2026-09-13 | 1.8.9           |

## Summary

A full-screen **"CHOOSE REWARDS — SELECT ONE 0/1"** overlay opened over the
lobby after a round ended, offering a choice between three different
blueprint rewards (seen: `M.III ROCKET BOOSTER BLUEPRINT`, `F-5 ANTI-RADAR
FLARES BLUEPRINT`, `SU-57 GUN COOLING BLUEPRINT`, each showing a "YOU HAVE
X/Y" progress count). None of the popup-dismissal or stall-recovery crops
recognize it, so the lobby quick-scan reports "no lobby crops detected"
indefinitely — the same blackout shape as Anomalies 001 and 004, a
different screen again.

Found live, mid-session, while watching a `make rd v` run for continued
Anomaly 003/005 validation. **Not yet auto-detected or auto-clicked** —
this record exists to capture it before the log rotates (per this
project's own convention) and to raise the operator decision it requires;
see Disposition.

## The incident

Session live at time of writing (`wingman.log`, `make rd v`, no explicit
`session_*` account suffix — launched via `make rd` per operator
instruction to attach rather than force an account relaunch).

| time | event |
|---|---|
| 21:09:01.215 | `Game state: GAME_END_B → GAME_LOBBY` — normal round-end flow. |
| ~21:09:13 (inferred) | The "Choose Rewards" overlay most likely opened here — not independently timestamped, inferred from the gap between entering `GAME_LOBBY` and the first stall-threshold log. |
| 21:09:44.711 | First auto-capture: `blackout_20260913_210944_stuck31s.png` (ADR 087). |
| 21:09:45 onward | `Stall recovery: 'STALL_PROFILE' not found` / `'STALL_EXIT_TO_DESKTOP' not found`, repeating every ~6s — both existing stall-recovery crops correctly scanned and correctly found no match. |
| 21:09:50–21:10:21+ (ongoing at time of writing) | `Lobby quick-scan: no lobby crops detected` / `stall threshold reached`, repeating every ~12s. |

## The screen

`test_screenshots/unknown_anomalies/blackout_20260913_210944_stuck31s.png`
(not yet copied to a permanent reference path). Distinguishing features:

- Title `CHOOSE REWARDS`, subtitle `SELECT ONE 0/1`.
- Three blueprint option cards, each with an icon, a name, and a
  `YOU HAVE X/Y` progress count.
- A greyed-out `SELECT ONE` button, bottom right — inert until one card is
  chosen (same shape as Anomaly 004's `CONFIRM`, which only activates
  after a card is clicked).
- No lobby element visible anywhere — same blackout signature as
  Anomalies 001 and 004.

## Why nothing recovered

Same shape as Anomaly 004's table:

| path | evidence | why it failed |
|------|----------|----------------|
| Popup dismissal | `Lobby quick-scan: no lobby crops detected`, repeating | nothing calibrated for this overlay |
| Stall recovery (`STALL_PROFILE`/`STALL_EXIT_TO_DESKTOP`) | confirmed scanned, both `not found` | neither crop matches this screen |
| Liveness guard | not yet fired at time of writing (session young) | will end the session cleanly at the 900s hard limit, `GAME_LOBBY` + no mission running being a safe point — same backstop that resolved Anomaly 004 |

## Disposition

**Operator decision (2026-10-02): "it should always choose the reward in the
middle of the screen then accept."** This replaces the decision of 2026-09-13
below, which is kept for the record.

- **Recurrence (measured).** 2026-10-02: `GAME_END_B` to `GAME_LOBBY`, then
  the overlay with three token rewards (Ideal Maneuvering, Efficient
  Afterburners, Streak Armor). The state stayed
  `GAME_LOBBY`, so only the lobby-blackout crops were scanned (`STALL_PROFILE`
  and `STALL_EXIT_TO_DESKTOP`, both `not found`), and the liveness guard
  logged its 300 s warning five minutes after the lobby was entered. The
  operator chose by hand about a minute later and the session carried on. No
  frame of the overlay was saved by wingman; the only capture is a half-resolution grab of the nested display
  taken just before that choice.
- **What wingman does now.** `STALL_CHOOSE_REWARDS` (the title, text
  `CHOOSEREWARDS`) is a stall-recovery crop, eligible both in an
  unclassifiable state and on a sustained `GAME_LOBBY` blackout. On
  detection `_choose_middle_reward` (`wingman/main.py`) clicks
  `STALL_CHOOSE_REWARDS_PICK`, the middle of the three cards, and 1.5 s later
  `STALL_CHOOSE_REWARDS_ACCEPT`, the SELECT ONE button; the second click is
  dropped if the game has left the lobby. The usual stall dwell (15 s) and
  cooldown (20 s) apply.
- **What is measured and what is not.** The three crops were measured on the
  half-resolution capture, so to about 2 px at full size. The real OCR reads
  the title from that capture scaled back up
  (`tests/test_stall_crops_ocr.py`, reference `test_screenshots/
  STALL_CHOOSE_REWARDS.png`, which is local and not tracked) and does not
  fire on an ordinary lobby frame. Not yet seen live: the click sequence
  itself, the 1.5 s delay (a named guess), and whether the blueprint variant
  of 2026-09-13 has the same layout. The first live occurrence should be
  checked against the log line `Stall recovery: 'STALL_CHOOSE_REWARDS'`.
- This is the only stall-recovery action that commits something instead of
  dismissing it; `STALL_PARTS_CONFIRM` (Anomaly 004) remains unautomated.

**Operator decision (2026-09-13), superseded: leave unautomated.** Same treatment as
Anomaly 004 — detect and document, never auto-select. The liveness
guard's 900s hard limit remains the sole recovery path; this is an
accepted, bounded cost, not something to fix by guessing a default.

1. **Not yet done.** No calibrated crop exists for this screen. A
   detection-only crop (title text `CHOOSE REWARDS`) could be added
   following the same pattern as `STALL_PARTS_CONFIRM`, but coordinates
   below are a **visual estimate from the captured frame, not yet
   pixel-measured or OCR-verified** — unlike this project's usual
   convention (see Anomaly 004/005), deliberately deferred until the
   operator's answer below determines whether this is worth calibrating
   at all, and if so, whether a click target needs measuring too.
   Approximate location: title text spans roughly x:0.34-0.66, y:0.04-0.09
   of the frame.
2. **Explicitly not auto-clicked, pending an operator decision.**
   Selecting a blueprint is a real choice between three different
   permanent upgrade paths (each with its own `YOU HAVE X/Y` progress) —
   the same shape as Anomaly 004's "which aircraft gets the parts"
   question, and by the same reasoning: every existing auto-click in this
   codebase is a strictly de-escalating dismiss (nothing committed); this
   would be the first that commits progress toward a specific choice with
   no safe/neutral default. Left to the operator rather than guessed.
3. Until (1)/(2) are decided, occurrences resolve only via the liveness
   guard's 900s hard limit (same backstop that resolved Anomaly 004
   cleanly) — a real but bounded cost (up to 15 minutes of session time),
   not a hang.

## What to watch

- **Confirmed**: the 900s hard limit fired cleanly (soft at 21:14:01.249,
  hard at 21:24:01.347) and the session ended with a full summary (45m06s,
  5 missions, 100% click-to-finish, 0 unknown outcomes) — the "ends safely"
  assumption held, same as Anomaly 004.
- Whether this recurs, and how often relative to Anomaly 004 (both are
  post-round reward/currency screens; unclear yet whether they're
  mutually exclusive per round-end or can both appear across different
  rounds).
- Whether the operator wants a default blueprint auto-selected, and if
  so, which one — or whether to leave this to manual/operator judgement
  entirely, same open question shape as Anomaly 004.
- Whether the `SELECT ONE` button's greyed-out state confirms the same
  "must pick a card first" pattern already found for Anomaly 004's
  `CONFIRM` button.

## References

- Anomaly 001 — the original PROFILE overlay livelock; same blackout
  shape, different screen, and the ADR 093 liveness guard this incident
  is currently relying on to end cleanly if untouched.
- Anomaly 004 — the parts-distribution overlay; same "detection is safe,
  auto-click is a real decision" shape, direct precedent for this
  record's Disposition.
- ADR 093 — the liveness guard.
- ADR 087 — blackout evidence capture (the auto-captured frame this
  record cites).
