# Anomaly 001 — Lobby PROFILE Overlay Livelock

| Status | Date       | Wingman Version |
|--------|------------|-----------------|
| Draft  | 2026-08-24 | 1.8.5           |

## Summary

A full-screen **PROFILE overlay**, opened over the lobby by wingman's own click,
held a session inert for **110 minutes**. Wingman kept running and kept logging,
but performed zero OCR, zero control actions and zero FSM transitions. No error
was raised, nothing crashed, and after the first nine minutes nothing warned.

This is the observation record. The decision and the fix are ADR 093; the
generalised lesson is Research 009.

## The incident

Session `run_20260824_001148_acct1`, 2026-08-24 00:11 to 03:39 (3h 27m).

| min | log lines | control actions | state changes |
|-----|-----------|-----------------|---------------|
| 60 | 6,988 | 1,573 | 30 |
| 75 | 6,726 | 1,671 | 16 |
| **90** | 3,978 | **0** | **0** |
| 195 | 3,415 | 6 | 0 |

Work stopped at minute 90 and never resumed. The process stayed healthy by every
conventional measure: 0 errors, ~4,000 log lines per 15 minutes, memory flat,
threads and file descriptors steady.

The session reported 14 missions in 3h 27m against 33 in a comparable healthy
run, and its memory measurement was corrupted — 1.9h of idle folded into a
nominal 3.25h window, reading +41 MB/h where the active portion read +2 MB/h.

## The screen

ADR 087's evidence capture worked exactly as designed and produced the answer:
five frames at 31s, 151s, 271s, 391s and 511s. The first and last are
**identical** — a full-screen profile page for another player, over the lobby,
with the mouse cursor resting on the player row that opened it.

Reference frame: `test_screenshots/STALL_PROFILE.png` (tracked).

Distinguishing features:

- Title `PROFILE` at top left, beside a back chevron.
- Close control `X` at top right.
- No lobby element visible anywhere — hence the blackout.
- Opened by a lobby click landing on a player row. The cursor position in the
  capture is the evidence; the specific click was not identified.

## Why nothing recovered

The overlay is not the lobby, not a calibrated popup, and not the exit dialog,
so each of the three recovery paths found nothing to act on:

| path | log evidence | why it failed |
|------|--------------|---------------|
| Popup dismissal | `no calibrated popup crop matched — nothing to dismiss` | nothing calibrated for this overlay |
| `STALL_EXIT_TO_DESKTOP` cancel | `'STALL_EXIT_TO_DESKTOP' not found` x1,235 | no exit dialog on screen |
| ESC escape loop | `ESC suppressed — lobby blackout in progress` x162 | gated off by the blackout itself |

The third is the trap, and it is worth stating precisely: **the recovery was
disabled by the condition it was meant to recover from.** ADR 087 suppressed ESC
during a blackout for a sound reason — ESC opens the Exit-to-Desktop modal, and
firing into a blackout re-creates it seconds after recovery cancels it. But the
suppression had no ceiling, so a blackout caused by anything the popup scan
cannot recognise disables the only mechanism that could clear it, permanently.

ESC would very likely have closed this overlay on the first press.

## The alarm went quiet

`unknown_anomaly.max_per_episode: 5` caps captures. The **warning shared that
cap**, so the last complaint was logged at 511 s while the fault ran a further
100 minutes in silence. For an unattended session this is the worst property of
the whole incident: absence of complaint read as health.

## Log signature

How to recognise a recurrence, in order of specificity:

```
ADR074 anomaly: <state> blackout stuck for <n>s (no calibrated popup crop
    matched — nothing to dismiss)
GAME_LOBBY escape loop: ESC suppressed — lobby blackout in progress
Stall recovery: 'STALL_EXIT_TO_DESKTOP' not found          (repeating)
Lobby quick-scan: no lobby crops detected (stalled <n>s)    (repeating)
```

The decisive tell is **`n_ocr=0` on consecutive `RESOURCE` lines** while the
process still logs. OCR count is the cheapest liveness proxy in the log, and it
distinguishes "stuck" from "quiet" better than state or memory does.

## Occurrence history

Six sessions in the corpus contain lobby-blackout stall episodes:

| log (named by end time) | warnings | max stuck | ESC suppressed (blackout gate) |
|-------------------------|----------|-----------|-------------------------------|
| `wingman_20260821_102133` | 5 | 510s | 0 |
| `wingman_20260821_103701` | 4 | 393s | 0 |
| `wingman_20260821_105108` | 4 | 391s | 0 |
| `wingman_20260821_110404` | 3 | 270s | 7 |
| `wingman_20260822_092108` | 5 | 511s | 56 |
| `wingman_20260824_033917` | 5 | 511s | **162** |

**Only the last is confirmed as a PROFILE overlay** — it is the only one whose
captures were inspected. The earlier five are lobby blackouts of unidentified
cause and must not be assumed to be the same screen.

Two observations, neither of them proof:

- Every one of these sessions is short. That now reads as sessions abandoned
  because they were stuck, rather than sessions intended to be brief.
- Blackout-gate ESC suppression appears from 2026-08-21 11:04 onward and its
  count escalates sharply into the incident. The correlation is suggestive but
  cannot be dated precisely from version control, because this repository
  routinely runs uncommitted code — the commit that contains the suppression is
  an upper bound on when it was written, not on when it first ran. The
  deadlock mechanism is established by reading the code, not by this timeline.

## Impact

- 110 minutes of a 207-minute session lost; 19 missions not flown.
- One ADR 091 replication data point corrupted, requiring the session to be
  discounted and rerun.
- Five prior sessions likely truncated for the same reason.
- No alert after the first nine minutes.

## Disposition

Addressed by **ADR 093**, four changes ordered general to specific:

1. A ceiling on the ESC suppression — it becomes a delay, not a veto.
2. A liveness guard: no FSM state change **and** no OCR for a limit ends the
   session at a safe point. This is the part that catches the *next* unrecognised
   screen rather than this one.
3. The warning continues on a doubling interval past the capture cap.
4. A `STALL_PROFILE` crop, with `STALL_PROFILE_DISMISS` as its click target.

### Status of the fix

| | evidence |
|---|---|
| Liveness guard false positives | none in ~12.4h across two sessions |
| `STALL_PROFILE` detection | verified by OCR against the reference frame |
| `STALL_PROFILE` closing a live overlay | **never observed** |
| ESC ceiling firing | **never observed** |

On 2026-08-24 15:20 the machinery was exercised without firing: a brief lobby
blackout opened the gate, `STALL_PROFILE` was scanned and logged `not found`,
and ordinary `REVEAL_ALL` popup dismissal cleared the blackout within seconds.
Correct behaviour, and evidence the scan is wired — but not evidence the
recovery works.

## What to watch

- Any recurrence of the log signature above, especially `n_ocr=0` runs.
- The first live `Stall recovery: 'STALL_PROFILE' — closing the profile overlay`
  line, which would be the first proof the recovery works.
- Any `LIVENESS GUARD` line during healthy play — that would be a false positive
  and means the thresholds need raising.
- Other full-screen overlays reachable from the lobby (store, hangar, settings,
  match history) are presumably capable of the same thing and are not
  calibrated. The liveness guard, not per-screen calibration, is the intended
  answer to those.

## References

- ADR 093 — the decision and the fix
- ADR 087 — blackout evidence capture and the ESC suppression this amends
- ADR 074 — `GAME_UNKNOWN` popup dismissal and the anomaly threshold
- Research 009 — guard the symptom generically, not only the known cause
- `test_screenshots/STALL_PROFILE.png` — reference frame
