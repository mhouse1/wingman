# ADR 084 — Stall-Screen Recovery Actions

| Status | Date       | Wingman Version |
|--------|------------|-----------------|
| Draft  | 2026-08-20 | 1.8.4           |

## Context

ADR 074 added popup dismissal in `GAME_UNKNOWN` so a modal that hides every
classification marker could be cleared automatically. Two findings on
2026-08-19 showed that mechanism was both broken and incomplete.

**The scanner never ran while unclassified.** `analyze_frame` returned early
for the whole `GAME_UNKNOWN` branch, and the lobby quick-scan thread was
started *below* that return, on a line commented "once on first frame after
unknown-state classification". `GAME_UNKNOWN` was therefore in
`POPUP_DISMISS_STATES` while the thread that scans popups could only start once
the state was no longer `GAME_UNKNOWN`. A session that booted straight into a
modal could never recover. Evidence from `logs/wingman_20260819_053748.log`:

```
04:29:00,199 Game state: UNKNOWN -> GAME_UNKNOWN
04:29:30,813 ADR074 anomaly: GAME_UNKNOWN stuck for 31s - screenshot 1/5 saved
04:31:05,303 Controller: 'm' key pressed - forcing GAME_LOBBY (was GAME_UNKNOWN)
04:31:06,226 Lobby quick-scan background thread started
```

The scanner started 126 s after the stall began, and only because of a manual
key press. Across all retained logs, `NEW_FLIGHT_PASS` had been dismissed zero
times, against 48 for `REVEAL_ALL` and 18 for `INVITED` — its handler had never
executed.

**Four stall screens had no handler at all.** Screenshots archived by the ADR
074 anomaly recorder identified four distinct screens that strand the FSM, none
of which is an ordinary dismissable popup:

| Screen | Marker | Why it strands the FSM |
|--------|--------|------------------------|
| `STALL_RETRY` | RETRY | Update-loop error at the title screen; no markers exist yet |
| `STALL_EXIT_TO_DESKTOP` | Cancel | Modal blurs the lobby behind it |
| `STALL_AIRCRAFT` | AIRCRAFT | Stranded in the J-20 upgrades menu |
| `STALL_MULTI_PLAYER` | red X | Squad stuck at UNREADY, PLAY click suppressed |

`STALL_MULTI_PLAYER` is the structurally worst case.
`_classify_unknown_state` calls `scan_region_for_play_button`, which returns
`None` whenever UNREADY is detected. Classification therefore cannot succeed
while a squad sits unready, so the session stays in `GAME_UNKNOWN` indefinitely
and never looks like a popup at all.

## Decision

**1. Start the popup quick-scan thread before the `GAME_UNKNOWN` branch.**
Extracted as `_ensure_lobby_quick_scan_thread()` and called on every frame,
guarded against restart during shutdown. Gating the scanner on successful
classification defeated the exact recovery path ADR 074 existed to provide.

**2. Add recovery actions for the four stall screens, behind a tighter gate
than the popup crops use.**

| Crop | Action |
|------|--------|
| `STALL_RETRY` | Click the RETRY button |
| `STALL_EXIT_TO_DESKTOP` | Press ESC |
| `STALL_AIRCRAFT` | Press ESC |
| `STALL_MULTI_PLAYER` | Click the red X, then click PLAY after a delay |

ESC is used for `STALL_EXIT_TO_DESKTOP` rather than a Cancel click because the
Cancel button sits directly beside an Exit button that would close the game. A
mis-registered click there ends the session; ESC has no such neighbour.

**3. Gate the recovery actions on a persisted stall, not merely a state.**

- `STALL_ACTION_STATES` is `GAME_UNKNOWN` and `GAME_STARTING_STALLED` only. It
  deliberately excludes `GAME_LOBBY` and `GAME_WAITING`, which
  `POPUP_DISMISS_STATES` includes.
- A dwell of `action_after_s` (15 s) in such a state must elapse first, so a
  brief unclassified frame mid-transition triggers nothing.
- `STALL_MULTI_PLAYER` is gated instead on `unready_dwell_s` (30 s) of
  continuous UNREADY, measured inside `scan_region_for_play_button`. Because
  UNREADY suppresses classification outright, this stall must be timed from the
  UNREADY read rather than from any state.

These actions are more invasive than dismissing a promo banner: they leave
squads and dismiss modals adjacent to destructive buttons. The gate, not the
detection, is the safety-critical part.

**4. Report recovery attempts to the ADR 074 anomaly recorder.** A recovery
action is recorded via `note_dismiss_attempt()`, so an anomaly capture states
whether recovery was tried and failed rather than implying nothing was
attempted.

## Anomaly Recorder Correction

Live testing on 2026-08-20 exposed a defect in the first version of the
attempt-reporting change. The recorder inferred "handling failed" from the
state still being `GAME_UNKNOWN`, and logged:

```
00:33:37,319 ADR074 anomaly: GAME_UNKNOWN stuck for 31s
             (dismissal of NEW_FLIGHT_PASS attempted 1x and did NOT clear it)
```

The dismissal had in fact worked — the popup was absent from 00:33:15 onward
across five consecutive scans. The state remained `GAME_UNKNOWN` because
classification was slow while the game finished loading, which is a different
condition entirely.

Failure is now inferred from the popup still being detected, not from the
state. A `LOBBY_POPUP_ABSENT` event fires whenever a popup batch completes with
nothing found, and the recorder distinguishes three outcomes:

| Condition | Reported as |
|-----------|-------------|
| Popup still detected past the grace window | dismissal attempted and did NOT clear it |
| Popup cleared, state still unclassified | cleared the popup but GAME_UNKNOWN persisted |
| Nothing ever matched | no calibrated popup crop matched |

The grace window itself is measured from the *first* attempt of an episode
rather than the latest, so a popup re-clicked every cycle without clearing
still produces evidence instead of deferring forever.

## Consequences

- A session booting into a modal recovers on its own; previously it required a
  manual `m` press.
- Recovery actions cannot fire while the FSM knows where it is, so healthy
  operation is unaffected. A 25-minute live session across three mission cycles
  produced zero recovery scans.
- `STALL_MULTI_PLAYER` matches on the single character `X`, and
  `_process_text_region` does substring matching with no confidence threshold.
  Any OCR output containing `x` inside that 48x41 region matches. The crop is
  tightly bound to the button, but this is the loosest matcher in the set and
  the first thing to suspect if a spurious squad-leave is observed.
- Anomaly screenshots now carry a verdict on recovery, making the archive
  usable for deciding whether a new crop is needed or an existing action is
  failing.
- Four more OCR crops run per scan cycle, but only while stalled — the gate
  short-circuits before any OCR is submitted during normal operation.

## Validation

Live, 2026-08-20 00:33 — `NEW_FLIGHT_PASS` detected and dismissed from
`GAME_UNKNOWN` for the first time in the project's history:

```
00:33:01,124 Lobby quick-scan background thread started
00:33:06,182 Game state: UNKNOWN -> GAME_UNKNOWN
00:33:06,616 popup 'NEW_FLIGHT_PASS' detected (text='NEWFLIGHTPASS')
00:33:06,616 Lobby quick-scan: dismissing popup 'NEW_FLIGHT_PASS' (state=GAME_UNKNOWN)
00:33:06,616 Controller: escape_recovery - pressing 'escape' key
00:33:15,570 popup 'NEW_FLIGHT_PASS' not found
```

The thread started 5 s *before* the state went unclassified, versus 126 s
*after* it in the 2026-08-19 failure.

OCR verified against the archived screenshots for all four stall crops:
`AIRCRAFT`, `CANCEL`, `X`, and `RETRY` each read cleanly.

Gate verified negatively over a 25-minute live session (three mission cycles,
2026-08-20 01:07 to 01:32): zero recovery scans, one `GAME_UNKNOWN` episode
lasting 1.07 s against the 15 s dwell.

Live, 2026-08-20 02:31 — first autonomous stall recovery, `STALL_EXIT_TO_DESKTOP`:

```
02:31:05,192 Game state: GAME_STARTING -> GAME_STARTING_STALLED
02:31:10,973 Game state: GAME_STARTING_STALLED -> GAME_UNKNOWN
02:31:20,733 Stall recovery: 'STALL_RETRY' not found
02:31:20,857 Stall recovery: 'STALL_EXIT_TO_DESKTOP' detected (text='CANCEL', state=GAME_UNKNOWN)
02:31:20,857 Stall recovery: 'STALL_EXIT_TO_DESKTOP' - pressing ESC (state=GAME_UNKNOWN)
02:31:24,017 Game state: GAME_UNKNOWN -> GAME_LOBBY
02:31:24,968 Game state: GAME_LOBBY -> GAME_WAITING
```

Each gate behaved as designed:

- Action fired 15.66 s after entering `GAME_STARTING_STALLED`, against the
  15.0 s `action_after_s` threshold.
- The dwell clock ran from `GAME_STARTING_STALLED`, not from the later
  `GAME_UNKNOWN` entry 9.88 s before the action — both states belong to
  `STALL_ACTION_STATES`, so the dwell correctly spans them.
- Scan order held: `STALL_RETRY` was checked first and missed,
  `STALL_EXIT_TO_DESKTOP` matched and broke the loop, `STALL_AIRCRAFT` was
  never scanned.
- Recovery took 3.16 s from the ESC press to `GAME_LOBBY`. Total stall was
  18.82 s, below the 30 s anomaly threshold, so no screenshot was archived —
  the recorder and the recovery path compose correctly.

For comparison, the 2026-08-19 04:29 baseline stalled for 126 s and recovered
only after a manual `m` press.

**Not yet validated live:** the `STALL_RETRY`, `STALL_AIRCRAFT`, and
`STALL_MULTI_PLAYER` actions, and the corrected `LOBBY_POPUP_ABSENT`
reporting. The three unfired actions share the gate and dispatch path proven
above and differ only in their click target; `STALL_MULTI_PLAYER` carries the
most residual risk, being the only action that clicks twice and reads the PLAY
button back. All rest on unit coverage
(`tests/test_stall_recovery.py`, `tests/test_stall_crops_ocr.py`,
`tests/test_tick_handlers.py`).

## Related

- ADR 074 — extend popup dismissal to `GAME_UNKNOWN`. This ADR corrects its
  implementation; 074 is still `Draft` and its stated premise never held for
  the startup case.
