# ADR 074 — Extend Popup Dismissal to GAME_UNKNOWN

| Status | Date       | Wingman Version |
|--------|------------|-----------------|
| Draft  | 2026-08-15 | 1.8.2           |

## Context

On 2026-08-15 (~10:02) a session stalled unrecoverably: after a
GAME_STARTING_STALLED → GAME_UNKNOWN reclassification, the FSM sat in
GAME_UNKNOWN for 15 minutes (617 identical scan ticks) until externally
terminated. A screen capture taken afterwards showed the cause: the game was
displaying its modal *"Event refresh in progress, try again soon."* popup
with an OK button, dimming the lobby behind it. The GAME_UNKNOWN classifier
looks only for lobby markers (PLAY / READY / UNREADY) and health digits — all
obscured by the modal — so no classification could ever succeed. The same
recovery path had completed in 18 seconds the previous night when no popup
was present.

The codebase already knows this popup. `event_refresh` (detection crop, text
`AGAIN`/`AGAINSO`) and `event_refresh_dismiss` (the OK button crop) are
calibrated in `config.yaml`, and the lobby quick-scan detects and dismisses
it — but every stage is gated to GAME_LOBBY / GAME_WAITING:

1. `_run_game_lobby_quick_scan` skips its cycle outside those two states.
2. The popup-batch gate (`do_popup_scan`) requires those states.
3. The post-futures re-check cancels queued popup futures outside them.
4. `_handle_lobby_popup` in `main.py` suppresses the dismissal click
   outside them.

A modal popup can appear while the FSM is in GAME_UNKNOWN (observed) — and
when it does, the popup that the system knows how to dismiss becomes the
thing that permanently prevents recovery.

## Decision

Extend the existing popup scan-and-dismiss pipeline to GAME_UNKNOWN. No new
watchdog subsystem: the quick-scan thread, popup crop definitions, OCR
batch, cooldown guard (`popup_click_allowed`), and click path are reused
unchanged.

- The quick-scan loop also runs in GAME_UNKNOWN, scanning **popup crops
  only** — no lobby-crop batch there. Lobby-marker detection in
  GAME_UNKNOWN stays where it is (the main-loop classifier), and quick-scan
  clicking of PLAY/CANCEL from an unclassified state would be wrong.
- The `do_popup_scan` gate, the post-futures state re-check, and the
  `_handle_lobby_popup` click handler each accept GAME_UNKNOWN.
- Cadence is unchanged: popup OCR every 5 s, dismissal clicks rate-limited
  by the existing per-popup cooldown.

Expected recovery flow: popup appears → GAME_UNKNOWN classification fails →
popup batch detects `event_refresh` within ~5 s → dismissal click on the OK
crop → lobby markers become visible → normal GAME_UNKNOWN → GAME_LOBBY
classification resumes.

### Anomaly evidence capture

Dismissal only works for popups with calibrated crops. The next stranding
variant will, by definition, be a screen nothing recognises — and the
2026-08-15 incident was only diagnosable because a screenshot happened to be
taken while the game was still stranded. `UnknownAnomalyRecorder`
(`tick_handlers.py`, ADR 060 handler contract) makes that evidence automatic:
when GAME_UNKNOWN persists past `unknown_anomaly.screenshot_after_s` (30 s —
normal startup classifies in ~4 s and never triggers), the current frame is
saved as `test_screenshots/unknown_anomalies/unknown_<timestamp>_stuck<N>s.png`
with a WARNING log line, recapturing every `recapture_interval_s` up to
`max_per_episode` per episode. Archived frames feed `make add-crops`
calibration so each new stranding variant becomes a dismissable popup (or
other stall handling) in a follow-up change.

## Consequences

- All popups in the quick-scan list (INVITED, CREATION_FAILED, REVEAL_ALL,
  SILVER, UNLOCK_CLOSE, INSPECT, event_refresh) become dismissable from
  GAME_UNKNOWN, closing the class of stranding, not just the observed
  instance.
- A dismissal click now can fire while the screen is genuinely unknown. The
  risk is bounded: clicks only fire on a positive OCR match of a calibrated
  popup crop with expected text, never blindly, and the per-popup cooldown
  prevents click storms on a persistent match.
- GAME_UNKNOWN spends a small amount of OCR budget every 5 s on popup crops.
  At ~0.2 s per crop on the thread pool this is negligible against the
  1.5 s tick.
- The 2026-08-15 stranding would have resolved in roughly one popup-scan
  period (≤ 5 s) instead of never.

## Verification

- Unit: popup emission path exercises the GAME_UNKNOWN gate (state-gating
  test).
- Live: **pending.** The plan was to attach wingman to the still-stranded
  game process (which was still displaying the popup) and watch it recover
  to GAME_LOBBY without a relaunch. At validation time the desktop was in
  active interactive use with the game window occluded — dismissal clicks
  are screen-coordinate mouse events and would have landed in foreground
  applications, so the attempt was aborted. Validate on the next
  unattended `make rd` session (popup recurrence) or against a
  deliberately provoked stranding.

## References

- ADR 065 — starting-health-probe reachability (the STALLED → UNKNOWN
  reclassification this failure followed).
- `wingman/analyzer.py` `_run_game_lobby_quick_scan` — the reused pipeline.
- `wingman/main.py` `_handle_lobby_popup` — the reused click handler.
