# Anomaly 005 — "+100 Universal Parts" Reward Crate Blocks Startup Classification

| Status | Date       | Wingman Version |
|--------|------------|-----------------|
| Draft  | 2026-09-13 | 1.8.9           |

## Summary

**Status: fixed.** A reward/loot-crate screen (**"+100 UNIVERSAL PARTS"**,
a `?`-marked crate in a hangar scene) appeared at session start, before
wingman ever reached a classifiable lobby state, and blocked all progress
for 600s until a dedicated startup-stall watchdog ended the session. Root
cause traced live (clicking the crate's `?` mark opens it and leads
directly into Anomaly 004's overlay — same parts-currency flow) and a
calibrated auto-dismiss shipped the same day. See Disposition.

Two corrections along the way, kept rather than silently rewritten, per
this project's own convention: **(1)** the original text claimed no safety
net existed at all — incomplete; a separate 600s startup-stall watchdog
(`main.py:1471`, independent of ADR 093's liveness guard) already bounded
it. **(2)** the original text assumed the dismiss action was unknowable
without interactive access — it was findable by grabbing a live frame from
the still-running game after wingman exited and testing directly, no
interactive session needed.

**Recurred on the very next fresh session launch — 2 for 2 so far**,
suggesting this is not a rare edge case but a normal, perhaps-every-launch
event (plausibly tied to parts/currency having accumulated from the
previous session, which never got to the Anomaly-004 screen that would
have spent them). Likely related to Anomaly 004 (`SELECT AN AIRCRAFT TO
RECEIVE PARTS`) — probably the same parts-currency UI flow: this may be
what grants the 100 parts Anomaly 004 later shows waiting to be spent.

Found live, immediately after launching a fresh `make r1 v` session to
continue Anomaly 003 validation.

## The incident

**First occurrence** — session `session_20260913_155600_acct1`
(archived; live as `wingman.log` at the time):

| time | event |
|---|---|
| 15:56:00 | Session starts (`Design 012: recording session video...`). |
| 15:56:00.342 | `XKey listener thread died` (`Xlib.error.BadRequest`) — unrelated transient startup glitch, self-healed via the existing reconnect logic 3s later (`XKey: display reconnected after 1 attempt(s)`). Noted, not the subject of this record. |
| ~15:56:34 | First auto-capture: `unknown_20260913_155634_stuck30s.png` — the reward-crate screen. |
| 15:57:34.847 | `GAME_UNKNOWN startup classification timeout after 90.4s` — first firing. Does **not** abort the session (that only happens when `live_capture is not None`, the ADR045 test lane — not a normal run); it just logs and continues. |
| 15:58:15 (131.1s) | Manually stopped (`z`) before the 600s stall-exit watchdog would have fired on its own — see Disposition/correction above. |

**Second occurrence, next session, session `session_20260913_160023_acct1`**
(archived `wingman.log`) — this time left to run to its natural conclusion:

| time | event |
|---|---|
| 16:00:28 | Session starts, `UNKNOWN → GAME_UNKNOWN` immediately. |
| 16:02:00 – 16:10:24 | `GAME_UNKNOWN startup classification timeout` repeats continuously, climbing from 92.1s to 596.5s. Never classified anything else the entire session — no `Game state: X → Y` transition after the initial entry. |
| **16:10:24.854** | `STALL: GAME_BATTLE not reached within 600s of startup (last state GAME_UNKNOWN) — exiting wingman. The computer is left running; check the game window and relaunch.` — the dedicated watchdog fires, `exit_requested.set()`. |
| 16:10:25.028 | Full clean shutdown: keys released, hooks deregistered, executor shut down, performance/stats artifacts written, session summary printed (0 missions, exit code 0). |

## The screen

`test_screenshots/unknown_anomalies/unknown_20260913_155634_stuck30s.png`.
A hangar scene: a "+100 UNIVERSAL PARTS" banner (upgrade-gear icon) top
left, and a black-and-camo crate with a glowing `?` mark, center-right —
reads as a loot-crate reward animation/screen, not a menu or dialog. No
lobby element, no known popup text, nothing STALL_PROFILE/STALL_RETRY/
STALL_EXIT_TO_DESKTOP/STALL_AIRCRAFT match (all confirmed scanned and
`not found` in the log — the `STALL_ACTION_STATES` batch is correctly
reaching this state and correctly finding nothing calibrated).

## Why nothing recovered (quickly) — but something does, at 600s

| path | evidence | why it failed |
|------|----------|----------------|
| Stall-recovery crop scan | `Stall recovery: '<crop>' not found` for all four `STALL_RECOVERY_CROPS`, repeating | nothing calibrated for this screen |
| `GAME_UNKNOWN` startup timeout (90s, logged) | fires every tick past 90s | logs only; does not act or abort outside the ADR045 test lane — a warning, not a guard |
| Liveness guard (300s soft / 900s hard) | never observed to fire across either occurrence | OCR is actively *running* every tick (health/fuel/lobby-crop reads, all logged), which counts as progress regardless of whether anything useful was read — the 300s-of-silence clock this guard watches for never accumulates here |
| **`STALL: GAME_BATTLE not reached within 600s`** (`main.py:1471`) | **fired correctly on the second occurrence, at 600s, ending the session cleanly** | this is the real safety net for this shape of failure — independent of the liveness guard, gated on wall-clock-since-startup and whether `GAME_BATTLE` was ever reached, not on any OCR/FSM signal at all |

So: bounded at 600s by a mechanism that isn't the liveness guard and isn't
the `GAME_UNKNOWN` timeout warning — a real, working, already-existing
guard, just a slower one than Anomaly 004's (900s hard limit, but that one
at least had OCR-silence to key off; this one is a flat wall-clock cap
regardless of activity). Genuinely different from Anomaly 001's original
livelock (that one had **no** bound of any kind before ADR 093).

## Log signature

```
GAME_UNKNOWN startup classification timeout after <n>s
```
repeating every tick, alongside `Stall recovery: '<crop>' not found` for
each calibrated crop, with no `Game state: X → Y` transition anywhere in
between.

## Impact

**Measured, not estimated: exactly 600s (10 minutes) lost per occurrence**,
confirmed by letting the second one run to its natural conclusion instead
of stopping it manually. Bounded and clean (process exits, no crash, no
lingering game process left in a bad state per the watchdog's own log
message), but a full 10-minute session produced **zero missions** — this is
a real, recurring productivity cost, not a safety issue. At 2-for-2 fresh
launches so far, this could be costing 10 minutes of *every* session start.

## Disposition

**Fixed and implemented, 2026-09-13, same day (third occurrence of this
investigation) — using the still-live game from the second occurrence
rather than racing a fresh launch.**

1. **Done.** After wingman exited via the 600s watchdog, the game itself
   was still running (per the watchdog's own log message) and still
   showing this exact screen. Grabbed a live frame directly from the
   nested display (`mss`) and tried a manual click on the crate's glowing
   `?` mark — **it worked**: the screen advanced directly into Anomaly
   004's `SELECT AN AIRCRAFT TO RECEIVE PARTS` overlay. This **confirms
   item 3 below directly**: same parts-currency flow, this crate screen is
   the entry point, Anomaly 004's overlay is the next step. Also
   established live: `CONFIRM` on that next screen does nothing until an
   aircraft is clicked first (auto-assigns it all 100/100 parts; `CONFIRM`
   turns from grey to active) — recorded in Anomaly 004's Disposition,
   confirming that screen's auto-click was correctly left un-wired.
2. **Done.** Unlike `STALL_PARTS_CONFIRM`, clicking the crate commits
   nothing — it only opens/collects a reward already owned, the same
   risk profile as `STALL_PROFILE`'s close-X. Added `STALL_PARTS_CRATE`
   (detection, text `PARTS`/`UNIVERSAL`) and `STALL_PARTS_CRATE_DISMISS`
   (the `?` mark, click target) to `config.yaml`, both coordinates
   measured by pixel-color thresholding against the archived reference
   frame (not estimated). Added to `STALL_RECOVERY_CROPS` (the
   `STALL_ACTION_STATES`-gated batch — this screen occurs in
   `GAME_UNKNOWN`, not a `GAME_LOBBY` blackout, so it uses the existing
   general gate, not a new dedicated one like Anomaly 004 needed). New
   `main.py` dispatch branch, same detect-one/click-another shape as
   `STALL_PROFILE`. Real-OCR test added
   (`tests/test_stall_crops_ocr.py::test_stall_crop_detects_its_marker[STALL_PARTS_CRATE...]`)
   — passes against the archived frame. `make lint` clean, gate-logic
   tests (`test_stall_recovery.py`, 29 tests) unaffected and passing.
   **Not yet live-validated** — next `make r1 v` run should confirm this
   screen now clears automatically instead of costing 600s.
3. **Confirmed** (see item 1) — same parts-currency flow as Anomaly 004,
   this is the entry point.

## What to watch

- Whether this keeps recurring on every fresh session start (2-for-2 so
  far) — if it's genuinely universal, item 1 above becomes much higher
  value (10 minutes lost on every single launch, not an occasional cost).
- **Confirmed twice**: the incident is bounded — first by a manual `z`
  stop (131s), then by letting it run naturally into the 600s watchdog,
  which fired exactly as designed and exited cleanly. No longer an open
  question.
- Whether the reward-crate screen ever changes on its own if left running
  past 600s normally (the watchdog cuts it off before this could be
  observed) — would need `startup_stall_exit_after_s` temporarily raised
  in config to find out, not done here.

## References

- Anomaly 004 — the parts-distribution overlay; likely the same
  parts-currency UI flow, different entry point (post-round vs.
  session-start).
- `wingman/main.py:1471` — the `STALL: GAME_BATTLE not reached within <N>s`
  watchdog that actually bounds this incident (`startup_stall_exit_after_s`,
  default 600s) — not ADR 093's liveness guard, a separate mechanism.
- ADR 093 — the liveness guard; checked and confirmed not the relevant
  mechanism here (see the correction in Summary).
- `test_screenshots/unknown_anomalies/unknown_20260913_155634_stuck30s.png`
  — the auto-captured reference frame.
