# ADR 146 — Generic Close-Button Recovery for a Stuck GAME_UNKNOWN

| Status | Date       | Wingman Version |
|--------|------------|-----------------|
| Draft  | 2026-09-24 | 1.8.11          |

## Context

On 2026-09-24 at 12:05 a session started with a full-screen promotional window
("A-10 Thunderbolt", "limited time offer", 13 d left) covering the lobby. It matches none of the popups
wingman knows: each of those is an OCR-detected, hand-calibrated crop (ADR 074), and this one has a
different title, layout and buttons. The classifier could not find PLAY or READY and the state sat in
`GAME_UNKNOWN` from 12:05:27 until it became `GAME_LOBBY` at 12:08:09, a few seconds after a hand click on the
window's close cross (about 2 min 42 s in all).
The log shows the whole stall: `Stall recovery: 'STALL_*' not found` cycling every 5 s, the ADR 093 line
`GAME_UNKNOWN STILL stuck after 31s ... Recovery has not worked` (a warning only), and 47
`GAME_UNKNOWN startup classification timeout` errors from 12:06:58 onward. An unattended run that hit this
would have sat idle for its whole session. It happened on 1 of the 6 sessions started that day.

The same review shows the fix cannot be "add another popup template": the game runs time-limited offers and
will show a new window next week. It also cannot be a blind key.

## Evidence (measured 2026-09-24; inferred parts are marked)

**ESC is unsafe.** With the game on a plain lobby, one synthetic ESC opened an "Exit to Desktop" dialog with
**Exit** as the highlighted (white) default and Cancel beside it. ADR 074 uses ESC for the flight-pass promo,
but a stuck classifier cannot tell that a modal is present, and a blind key on a lobby leads
toward quitting the game. (The dialog was closed by clicking Cancel; nothing else changed.)

**The close cross is one shared sprite.** The A-10 window (29 px), another player's profile screen
(`STALL_PROFILE.png`, 38 px) and the flight-pass window (`NEW_FLIGHT_PASS.png`, 45 px) draw the same bold
white cross at different sizes on a 1920 x 1200 frame. Full-screen pages (the operator's own profile, the Flight
Pass page, the Shop) use a back chevron instead and have no cross.

**A white-mask template match separates it from everything else measured.** Matching on white pixels rather
than colour (the window behind the cross is a photograph), at six scales (five ship), over all 3,258 full-size
frames in `test_screenshots/`:

| Frame | Best score |
|-------|------------|
| Flight-pass window (real cross) | 0.951 |
| Another player's profile screen (real cross), two frames | 0.859 |
| A-10 window (the source of the sprite), at the nearest scale in the shipped grid | 0.937 |
| Best on any other non-gameplay frame: "Exit to Desktop" dialog | 0.747 |
| Live pages on 2026-09-24: Flight Pass 0.704, Shop 0.667, own profile 0.640 (the replay's live lobby search also stayed below 0.82) | all below 0.82 |
| Archived plain lobby (`P1_000_LOBBY_PLAY.png`) | 0.613 |

The scale 0.75 is left out of the shipped grid because it produced most of the highest false matches. Three
genuine examples is a **thin** sample; the threshold, 0.82, sits between 0.859 and 0.747 and is a named guess.

## Decision

Add `wingman/close_button.py` and wire it into the main loop (`main.py`, beside `unknown_anomaly.tick`):

- `find_close_button(frame)` returns the best white-cross candidate at or above `min_score` (default 0.82),
  matching a 29 x 29 sprite mask (embedded as readable rows, no image asset) at scales 0.9 to 1.65 on a
  half-resolution white mask. About 60 ms per search.
- `GenericCloseRecovery.tick(frame, unknown)` holds the policy: nothing until the state has been
  `GAME_UNKNOWN` for `min_stuck_s` (25 s, far beyond the 1 to 2 s a normal classification takes and past the
  5 s known-popup scan), one search per `retry_interval_s` (15 s), at most `max_clicks` (3) clicks per stuck
  episode, and the episode resets the moment the state leaves `GAME_UNKNOWN`. It returns a hit; the caller clicks
  through the existing `Controller.click_crop` on a small box centred on it (`click_region`).
- Config block `game_unknown_close` (`enabled: true` by default; the recovery only acts in a state that has
  already failed for 25 s). It also calls `unknown_anomaly.note_dismiss_attempt("generic_close")` so the ADR 074
  recorder treats it like any other dismissal attempt.

## Alternatives considered

- **ESC after a stuck timeout.** Rejected on the evidence above.
- **More per-popup templates.** Kept for known popups; it cannot anticipate a new promotion.
- **Do nothing, rely on the ADR 093 warning.** That is what failed here: silence had been fixed, recovery had not.
- **Ship gated off.** Not chosen: the action is confined to a state that has already been failing for 25 s, and it
  was verified end to end below. It is one config line to turn off.

## Consequences

- A new automated click exists, but only after 25 s of `GAME_UNKNOWN`, at most 3 per episode.
- A false positive would click somewhere on an unclassifiable frame. The measured false maximum is 0.747 against
  a 0.82 threshold, but that margin rests on three genuine crosses; a different modal whose cross is drawn
  differently would simply not match (recovery does nothing), and a gameplay frame stuck in `GAME_UNKNOWN` can
  in principle contain a white cross-like shape. The click budget bounds the damage.
- It will not help on windows without the cross (the "Exit to Desktop" dialog, page-style screens). Those stay
  as before.
- `docs/adr/074-...` is Accepted and unchanged; this ADR extends its area rather than editing it.

## Verification

- `tests/test_close_button.py`, 24 tests: the finder at sizes 1.0, 1.3 and 1.55, over a noisy photographic
  background, rejecting an upright plus, plain white shapes, no white, bad input and honouring the threshold; the
  real A-10 case from a committed 200 x 180 fixture (`tests/fixtures/a10_promo_close_region.png`); four archived
  frames when present (`NEW_FLIGHT_PASS`, `STALL_PROFILE`, and the two negatives `STALL_EXIT_TO_DESKTOP` and a
  plain lobby; skipped where the untracked corpus is absent); the click-region maths and edge clamping; and the
  recovery policy with an injected clock (disabled by default, nothing before `min_stuck_s`, retry interval, click
  budget, misses not spending clicks, episode reset, threshold passed through).
- **Incident replay, end to end (13:44 to 13:45).** The real captured A-10 frame was shown full-screen over the
  nested display with a click logger, and wingman was started against it. `GAME_UNKNOWN` at 13:44:36; at 13:45:02
  `GenericCloseRecovery: GAME_UNKNOWN for 26s - clicking a close button at (1701,255), score 0.937, scale 0.90
  (click 1 of 3)`; the logger recorded a click at (1701, 255) the same second, one pixel from the cross centre. At
  13:45:19, with the overlay removed, the retry search on the visible lobby logged `no close button found` and did
  not click, then `GAME_UNKNOWN -> GAME_LOBBY` at 13:45:19.8. Exactly one click in total.
- Full suite and lint: see the action item log for the run recorded with this change.

## Not verified

- A **live positive on a real promotion**: the A-10 window did not reappear in the two later game launches, so the positive path was verified on the replayed real frame, not a fresh occurrence. The click
  landing on a real window's close button is inferred from the hand click at (1700, 254) that did close it.
- Cross-shaped shapes in gameplay frames that are misclassified as `GAME_UNKNOWN`: not measured.
- The recovery is silent about page-style screens; not a defect, but not covered.

## References

ADR 074 (popup dismissal in GAME_UNKNOWN), ADR 093 (stuck-state warning), ADR 094 (finish-round stop),
`docs/action-item/001-target-tracking-lock-stability-and-false-positives.md` (start-up incident entry).
