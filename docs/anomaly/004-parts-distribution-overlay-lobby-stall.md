# Anomaly 004 — "Select an Aircraft to Receive Parts" Overlay Stalls the Lobby

| Status | Date       | Wingman Version |
|--------|------------|-----------------|
| Draft  | 2026-09-13 | 1.8.9           |

## Summary

A full-screen **"SELECT AN AIRCRAFT TO RECEIVE PARTS"** overlay — a
post-match parts/upgrade distribution screen with a `CONFIRM` button —
opened over the lobby during a normal, successful round-end sequence (not
triggered by any wingman click, unlike Anomaly 001's PROFILE overlay).
None of the popup-dismissal crops recognize it, so the lobby quick-scan
reports "no lobby crops detected" indefinitely and the session makes no
progress until the ADR 093 liveness guard eventually ends it.

Found live, mid-session, while watching a `make r1 v` run for an unrelated
fix (Anomaly 003). **Detection is implemented and verified** (a calibrated
crop, real-OCR-tested against the archived frame); **the recovery click is
deliberately not wired in** — it would commit an in-game action rather than
just dismiss, which the existing auto-click gate's documented invariant
doesn't cover, so that decision is left to the operator rather than made
autonomously. See Disposition.

## The incident

Session `session_20260913_121658_acct1` (`wingman.log`, live at time of
writing).

| time | event |
|---|---|
| 15:29:29.882 | `GAME_END_B → GAME_LOBBY` — normal round-end flow, `Final continue click complete`. |
| 15:29:31–39 | `FINAL_CONTINUE` and `REVEAL_ALL` popups dismissed normally — the lobby was briefly healthy. |
| ~15:30:02 | `Lobby quick-scan: stall threshold reached` — no lobby crops detected from here on. The parts-distribution overlay most likely opened around here (not independently timestamped — inferred from the gap between the last successful dismissal and the first stall-threshold log). |
| 15:30:11 | First auto-capture: `blackout_20260913_153011_stuck31s.png`. |
| 15:32:13, 15:34:13 | Further auto-captures at the anomaly-capture interval, `stuck152s`/`stuck272s`. |
| **15:34:29.917** | `LIVENESS GUARD: no FSM state change and no OCR for 300s` (ADR 093 soft limit) — the first ERROR-level signal. |
| 15:35:28 (ongoing at time of writing) | Still stalled, `absent=384s` and climbing. Hard limit (900s default) would end the session cleanly at ~15:44:30 if nothing clears it — `GAME_LOBBY` with no mission running is a safe point, so that stop is expected to be clean. |

## The screen

`test_screenshots/unknown_anomalies/blackout_20260913_153413_stuck272s.png`
(not yet copied to a permanent reference path). Distinguishing features:

- Title `SELECT AN AIRCRAFT TO RECEIVE PARTS`, with a gear/upgrade icon.
- `PARTS TO DISTRIBUTE` counter (`100/100` in this capture).
- A horizontal row of owned aircraft (level + parts progress bar each),
  wider than one screen — implies scrolling/more aircraft than shown.
- A `CONFIRM` button, centered near the bottom.
- No lobby element visible anywhere — same blackout signature as Anomaly
  001, different screen.

## Why nothing recovered (as far as observed)

Same shape as Anomaly 001's table, abbreviated:

| path | evidence | why it failed |
|------|----------|----------------|
| Popup dismissal | `Lobby quick-scan: no lobby crops detected`, repeating | nothing calibrated for this overlay |
| Stall recovery (`STALL_PROFILE`/`STALL_EXIT_TO_DESKTOP`) | not confirmed in this log excerpt — worth checking directly | if scanned, neither crop matches this screen |
| Liveness guard | soft limit fired at 300s (observed) | correctly noticing; will end the session at the hard limit (900s) since `GAME_LOBBY` + no mission running is a safe point — this is the generic backstop working as designed, not a failure |

Unlike Anomaly 001, this session is expected to end **cleanly** via the
liveness guard's hard limit rather than needing an operator interrupt —
worth confirming once it actually fires.

## Log signature

```
Lobby quick-scan: no lobby crops detected (stalled <n>s)
Lobby quick-scan: stall threshold reached
```
repeating, with `BT[active]: selected=Idle ... absent=<n>s ... alt=None`
climbing and `BOUNDARY: no reading` alongside it, eventually followed by:
```
LIVENESS GUARD: no FSM state change and no OCR for 300s — wingman is not making progress (ADR 093)
```

## Impact

Not yet fully measured — the session was still stalled at the time this
was written. At minimum: one round's worth of session time lost to the
stall, ending at the liveness guard's hard limit rather than continuing to
play. Frequency unknown — this is the first observed occurrence in the
sessions run so far today.

## Disposition

**Detection implemented and verified; auto-click deliberately withheld
pending an explicit operator decision.**

1. **Done.** `STALL_PARTS_CONFIRM` crop added (`config.yaml`), coordinates
   measured directly from the archived reference frame by pixel-color
   thresholding around the button (not estimated) —
   `[0.4188, 0.8067]`–`[0.5807, 0.8625]`. Reference frame copied to
   `test_screenshots/STALL_PARTS_CONFIRM.png` (tracked, matching the
   `STALL_PROFILE.png`/`STALL_RETRY.png` convention). Real-OCR verification
   added to `tests/test_stall_crops_ocr.py` — both
   `test_stall_crop_detects_its_marker` and
   `test_stall_crop_does_not_fire_on_a_clean_lobby` pass against the real
   archived frame (`-m slow`, EasyOCR, not the default `make test` run).
2. **Deliberately not done: wiring it into the auto-click recovery gate.**
   `_stall_recovery_targets` (`analyzer.py`) has a documented invariant —
   see `test_lobby_blackout_past_dwell_opens_only_de_escalating_crops` —
   that every crop on this gate is a **strictly de-escalating** click
   (STALL_PROFILE's close-X, STALL_EXIT_TO_DESKTOP's Cancel: dismiss,
   nothing committed). Clicking `CONFIRM` here does not fit that bar.
   **Confirmed live 2026-09-13** (while investigating Anomaly 005, using
   the still-running game after wingman had exited): `CONFIRM` does
   **nothing** until an aircraft card is clicked first — no aircraft is
   pre-selected by default, contrary to this entry's original assumption.
   Clicking a card auto-assigns it all 100/100 parts and turns `CONFIRM`
   from grey (inert) to white (active). So the real auto-click would need
   to pick an aircraft *and* click CONFIRM — two decisions, not one, and
   the aircraft choice has no safe/neutral default. Reinforces rather than
   weakens the original call to leave this to the operator. Low-stakes
   (the operator's own aircraft, a minor upgrade-currency allocation, not
   destructive or irreversible in any real sense) but a genuine policy
   widening, not a same-shape addition — flagged in code (`analyzer.py`,
   right above the independent-UNREADY-gate comment) rather than crossed
   autonomously during an unattended loop. **Needs an explicit yes from
   the operator before wiring it in, including which aircraft (if any)
   should get the parts by default** — the two-line change is `main.py`'s
   `STALL_RETRY`-shaped dispatch branch (drafted and reverted, see git
   history/diff if wanted) plus one new `if` block in
   `_stall_recovery_targets` mirroring the
   `STALL_PROFILE`/`STALL_EXIT_TO_DESKTOP` blocks exactly.
3. Until (2) is decided, occurrences still resolve only via the liveness
   guard's 900s hard limit (confirmed clean in this incident — see
   timeline above) — a real but bounded cost, not a hang.
4. Not yet checked: whether this screen only appears after specific
   conditions (parts threshold, level-up) — if predictable, it might be
   avoidable rather than only dismissible. Not investigated.

## What to watch

- **Confirmed**: the hard limit fired at exactly 900s
  (`15:34:29.917` soft → `15:44:30.020` hard) and the process exited
  cleanly (exit code 0, full session summary written) — the "ends safely"
  assumption held.
- **Confirmed**: `STALL_PROFILE`/`STALL_EXIT_TO_DESKTOP` were scanned
  repeatedly against this screen (every ~6s from `15:30:03` onward) and
  correctly found neither matched — the existing recovery paths were
  properly exercised and correctly failed, not skipped.
- Any recurrence, and whether it correlates with parts/level milestones.
- Whether the operator wants `STALL_PARTS_CONFIRM` wired into the auto-click
  gate (Disposition item 2) — the open decision this record exists to
  surface.

## References

- Anomaly 001 — the PROFILE overlay livelock; same blackout shape,
  different screen, and the ADR 093 liveness guard this incident is
  currently relying on to end cleanly.
- ADR 093 — the liveness guard and blackout ESC ceiling.
- ADR 087 — blackout evidence capture (the auto-captured frames this
  record cites).
- `test_screenshots/unknown_anomalies/blackout_20260913_15*` — the three
  auto-captured frames from this incident.
