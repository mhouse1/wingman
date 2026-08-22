# ADR 087 — Capture evidence when a classified state goes blind

| Status | Date       | Wingman Version |
|--------|------------|-----------------|
| Draft  | 2026-08-21 | 1.8.5           |

## Problem and outcome

**Symptom.** Wingman sat in `GAME_LOBBY` for 17 minutes doing nothing useful,
with every lobby crop reading blank. Across six sessions it never reached a
battle.

**Root cause.** `GAME_END_B` timed out and *forced* the FSM to `GAME_LOBBY`
while the game was still on the post-match "Click to Continue" screen. The
click-to poller self-suppresses in `GAME_LOBBY`, so the forced state disabled
the one detector that could clear the screen holding the FSM there. The
recovery asserted a state instead of verifying one, and the assertion silenced
its own cure.

**Common defect.** Three separate gates each trusted a state label over what
the screen showed:

| Gate | Trusted | Fixed by |
|------|---------|----------|
| ADR 074 anomaly capture | state is `GAME_UNKNOWN` | d1 — capture on a lobby blackout too |
| ADR 084 stall recovery | state in `STALL_ACTION_STATES` | addendum 1 — blackout opens the exit-dialog crop |
| click-to self-suppression | state is `GAME_LOBBY` | addendum 4 — yields during a blackout |

**Changes, in the order they were made.** Each was necessary; the first three
addressed a self-inflicted amplifier, the fourth the cause.

| # | Change | Effect |
|---|--------|--------|
| d1 | Capture a screenshot on a lobby blackout, not only `GAME_UNKNOWN` | Produced the evidence everything below depended on |
| a1 | A sustained blackout may scan `STALL_EXIT_TO_DESKTOP` | Dialog detected for the first time |
| a2 | Cancel is clicked, not ESC'd (supersedes ADR 084 for this crop) | Dialog actually closes |
| a3 | No ESC into a lobby blackout, from any source | Stops wingman re-opening it every 23s |
| a4 | Click-to OCR resumes during a blackout | **Root-cause fix** |

**Outcome.** The session after a4: 5h43m, **55 missions, 100% click-to finish**,
zero lobby exits, zero unknown outcomes. Blackout log lines fell from 130 to 7
in the first five minutes.

**Status.** Draft. The amplifier chain (a1–a3) and the root cause (a4) are all
implemented and live-validated. Remaining open question: whether ESC belongs in
lobby recovery at all — the 45s escape loop presses it blindly and has no
evidence behind its interval.

## Context

ADR 074 archives a screenshot whenever `GAME_UNKNOWN` outlives a threshold, on
the reasoning that a screen nothing recognises is exactly the screen worth
keeping. The capture is gated on the **state name**.

That gate misses the more misleading failure. On 2026-08-21 the session sat in
`GAME_LOBBY` for roughly eight minutes:

```
09:52:13  last live frame — GAME_BATTLE, 10709m, OUT OF AMMO
09:53:02  portal: restore token found; PipeWireBackend: pipeline running
09:53:02  PipeWireBackend: game window found via xwininfo — "Metalstorm"
...
09:59:42  Lobby quick-scan: no lobby crops detected (stalled 11.2s)
09:59:42  Lobby quick-scan: stall threshold reached — pressing ESC
09:59:45  Lobby quick-scan: no lobby crops detected (stalled 2.9s)
09:59:46  Click-to OCR skipped: GAME_LOBBY state active
```

The FSM was confidently in `GAME_LOBBY` while **every** lobby crop read empty,
so the ESC recovery ran on a loop against a screen it could not see. Because
the state was not `GAME_UNKNOWN`, ADR 074 never fired and the only artefacts
left were the ESC presses themselves.

The diagnosis then failed on every available route:

- `mss` `XGetImage()` fails — this host captures via the PipeWire portal (ADR 050).
- `import` / `xwd` against the game's XWayland window: both fail for the same reason.
- `org.gnome.Shell.Screenshot`: `AccessDenied`.
- The `v` screenshot hotkey needs a real keypress; the `keyboard` package
  demands root and wingman's own XTest shim is in-process.
- `live_hud.png` only renders in `GAME_BATTLE`, so it was stale from before the
  episode began — and being stale, it was briefly and wrongly read as evidence
  that capture itself had died.

The running wingman held the only working capture path in the system and was
not being asked to use it. **A state that is classified but blind produces less
evidence than one that is honestly unknown**, which is the wrong way round.

## Decision

Capture is gated on the **evidence**, not on the state name. When every
defining crop for the current state reads empty for longer than the ADR 074
threshold, the recorder captures, exactly as it does for `GAME_UNKNOWN`.

The analyzer already emits `LOBBY_STALL` on that condition, re-firing every 10s
while the blackout lasts, so no new detector is introduced — the existing beat
gains a second subscriber alongside the ESC press.

Episode bookkeeping:

- The first beat opens an episode; the recapture interval and per-episode cap
  are shared with the `GAME_UNKNOWN` path.
- Three missed beats (30s) lapse the episode — a recovered scan stops emitting,
  so a blackout that clears never produces a late capture.
- Any FSM state change ends the episode: a new answer means classification is
  working again.
- Captures are named `blackout_*` rather than `unknown_*`, and the log line
  names the state, so triage can tell the two apart.

The clock starts at the **first beat**, not at the true start of the blackout,
so `stuck_for` understates the stall by the analyzer's own 10s threshold. That
is deliberate — it keeps the handler independent of that constant, and erring
late can only suppress a capture, never invent one.

## What the first capture showed

The capture fired within minutes of shipping and answered the question
immediately: `blackout_20260821_101718_stuck270s.png` shows an **"Exit to
Desktop" confirmation dialog**, with **Exit** as the highlighted default button
and Cancel beside it.

The blackout is self-inflicted:

1. Lobby crops read blank; after 10s `LOBBY_STALL` fires.
2. The subscriber presses ESC. **In the lobby, ESC opens "Exit to Desktop".**
3. That dialog matches no lobby crop, so the scan stays blank.
4. 10s later the next beat presses ESC again. Goto 2.

A crop for this screen already exists — `STALL_EXIT_TO_DESKTOP`, whose action is
a Cancel click — but ADR 084 gates the stall-recovery batch on
`STALL_ACTION_STATES`, which deliberately excludes `GAME_LOBBY`. So the one
dialog wingman creates itself was the one dialog it would not scan for. Its
recovery and its detection were gated on contradictory conditions.

The safety margin here was thinner than the deadlock suggests: the modal sat for
eight minutes with **Exit highlighted as the default**. Any stray Enter or Space
reaching that window quits the game.

## Decision (addendum) — a sustained lobby blackout may cancel the exit dialog

`_stall_recovery_targets` gains an independent gate, in the same shape as the
existing UNREADY one: once a `GAME_LOBBY` blackout outlives `action_after_s`,
`STALL_EXIT_TO_DESKTOP` becomes eligible.

This is deliberately narrower than opening the full batch. Only the dialog
wingman can create itself is unlocked, and its action is a Cancel click —
strictly de-escalating. `STALL_RETRY` and `STALL_AIRCRAFT` remain gated on a
genuinely unclassifiable state, preserving ADR 084's reasoning that invasive
actions must not fire while the FSM still knows where it is.

Tracking the blackout needs its own anchor: `lobby_stall_since` restarts on
every ESC as a press cooldown, so it measures the gap between presses, not the
length of the stall.

## Decision (addendum 2) — Cancel is clicked, and ESC stands down

Shipping the gate above proved the recovery *action* was also wrong. The dialog
was detected on every scan for 25 minutes and never cleared:

```
10:34:04  Stall recovery: 'STALL_EXIT_TO_DESKTOP' detected (text='CANCEL')
10:34:14  Lobby quick-scan: stall threshold reached — pressing ESC
10:34:15  Stall recovery: 'STALL_EXIT_TO_DESKTOP' detected (text='CANCEL')
10:34:15  GAME_LOBBY escape loop: pressing ESC
10:34:21  Stall recovery: 'STALL_EXIT_TO_DESKTOP' — pressing ESC
10:34:27  Stall recovery: 'STALL_EXIT_TO_DESKTOP' detected (text='CANCEL')
```

Two findings:

**ESC cannot dismiss this modal.** ADR 084 chose "escape, never a click" for
this crop because the Cancel button sits beside an Exit button that would close
the game. Sound reasoning, but it assumed ESC was a working alternative. It is
not — ESC only ever *opens* the dialog. The chosen-safe action was a no-op, and
the safe-looking policy produced a total deadlock: zero battles across three
sessions.

**Three uncoordinated ESC sources were re-opening it**: the 45s `GAME_LOBBY`
escape loop, the 10s `LOBBY_STALL` beat, and the 20s stall-recovery action
itself — all pressing the key that creates the modal.

So, superseding ADR 084 for `STALL_EXIT_TO_DESKTOP` only:

- The action is a **Cancel click** on the calibrated crop. That crop is the
  Cancel button; its centre sits roughly 130px clear of Exit, and the click goes
  through the same `click_crop` path every other recovery crop uses.
- `exit_dialog_visible()` gates **every** ESC source. While the modal is up, the
  escape loop and the stall beat both stand down, so nothing re-opens what the
  click closes. The flag lapses after ~2 scan intervals so a cleared dialog
  never suppresses ESC permanently.

`STALL_AIRCRAFT` keeps ESC unchanged — it has no adjacent destructive button and
ESC works there.

## Decision (addendum 3) — no ESC into a lobby blackout, from any source

The Cancel click works: the dialog clears within ~6s of the click. It was then
re-opened every time, on a 23s cycle:

```
10:48:27  'STALL_EXIT_TO_DESKTOP' — clicking CANCEL
10:48:33  'STALL_EXIT_TO_DESKTOP' not found          <- cleared, flag zeroed
10:48:34  Lobby quick-scan: stall threshold reached — pressing ESC   <- re-opened
10:48:50  'STALL_EXIT_TO_DESKTOP' — clicking CANCEL
10:48:56  'STALL_EXIT_TO_DESKTOP' not found
10:48:57  Lobby quick-scan: stall threshold reached — pressing ESC
```

The addendum-2 suppression was defeated by its own success signal: one "not
found" scan zeroes the dialog flag, and the ESC beat fires 1.4s later into the
gap. Widening the staleness window only lengthens the cycle — after any lapse
the press returns and re-creates the modal.

So the gate is the **blackout**, which outlasts those gaps, and the beat's ESC
press is removed outright:

- `LOBBY_STALL` no longer presses ESC. The beat's remaining job is to arm the
  capture and the blackout clock. Recovery for a lobby blackout is the stall-crop
  scan, which cancels the dialog.
- The 45s `GAME_LOBBY` escape loop is gated on `lobby_blackout_active()`.

The reasoning generalises past this one dialog: **during a blackout, ESC has no
demonstrated benefit and one demonstrated harm.** A key whose lobby effect is
"open a modal that hides every lobby crop" cannot be a recovery from lobby crops
being hidden.

Note this session inherited the dialog from the previous one — it was already up
16s after entering `GAME_LOBBY`, before any ESC. So the beat is proven to
*sustain* the blackout, not yet to start it. The 45s escape loop pressing ESC
into a *healthy* lobby remains the prime suspect for the original cause, and is
untested.

## Root cause — a forced state that disables its own cure

With ESC removed the exit dialog stopped reappearing, and the blackout
**continued** — proving the dialog was an amplifier, not the cause. The next
capture showed the real screen: the post-match **PERFORMANCE** panel, with
**"Click to Continue..."** at the bottom.

Wingman has a calibrated `click_to` crop for precisely that prompt. It was never
scanned. The chain:

```
10:44:23  GAME_END_B timeout — click-to OCR may be stuck; forcing recovery to GAME_LOBBY
10:44:28  Click-to OCR skipped: GAME_LOBBY state active
   ...    (repeats for 17 minutes)
```

1. `GAME_END_B` timed out and **forced** the FSM to `GAME_LOBBY`.
2. The game was still on the end-of-match screen, so the forced state was a lie.
3. The click-to poller self-suppresses in `GAME_LOBBY` — a correct rule added
   after the 2026-07-30 double click-through.
4. So the forced state disabled the one detector that could clear the screen
   that was holding the FSM there.
5. Lobby crops matched nothing → blackout → ESC → exit dialog → everything above.

The recovery asserted a state instead of verifying one, and the assertion
silenced its own cure. A timeout that cannot fix the problem renamed it.

## Decision (addendum 4) — suppression yields to contradicting evidence

The click-to scan resumes in `GAME_LOBBY` while a lobby blackout is active.

A blackout is precisely the evidence that the suppression's premise — *the FSM
is right about being in the lobby* — has failed. In a healthy lobby the crops
match, no blackout is active, and the 2026-07-30 suppression is untouched.

This is the same shape as the rest of this ADR: **gate on evidence, not on a
state name.** Three separate defects in this incident share that root — the
capture gate (`GAME_UNKNOWN` only), the recovery gate (`STALL_ACTION_STATES`
only), and this one. Each trusted a label over what the screen showed.

## Consequences

A lobby blackout now leaves a frame to look at, and the specific blackout
wingman inflicts on itself now clears. The class of bug this addresses is the
*confidently wrong* state, which is harder to diagnose than an unknown one
precisely because every subsystem reports itself healthy.

ESC in the lobby still opens the dialog the first time a blackout is
misdiagnosed; what changes is that it is now closed rather than re-opened.
Whether ESC is the right lobby recovery at all remains open — it is the source
of this failure, and the 45s escape loop in particular has no evidence behind
its interval.

This ADR carries a real risk the previous policy avoided: wingman now clicks
inside a dialog whose other button quits the game. The mitigation is that the
click target is a calibrated crop covering Cancel, not a computed offset, and
`tests/test_stall_crops_ocr.py` already asserts that crop reads "CANCEL". If
that crop is ever recalibrated onto the wrong button, the failure is a closed
game rather than a stalled one.

Nothing about the `GAME_UNKNOWN` path changes; ADR 074's behaviour, thresholds,
and dismissal-grace logic are untouched.

## Alternatives considered

**Reclassify a blind `GAME_LOBBY` to `GAME_UNKNOWN`.** Existing stall recovery
would then apply for free. Rejected for now: it discards the classifier's
evidence rather than recording that it disagrees with the crops, and it would
route a lobby blackout into recovery paths built for a different failure. Worth
reconsidering once the captured frames show what the screen actually is.

**Lower the ADR 074 threshold.** Does nothing here — the state never became
`GAME_UNKNOWN` at all.

**Have the diagnosing operator take the screenshot.** Not available: as above,
every capture route on this host is blocked except the portal session wingman
already owns.
