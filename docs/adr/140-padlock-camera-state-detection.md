# ADR 140 — Padlock Camera State Detection via Multi-Signal Fusion

| Status | Date       | Wingman Version |
|--------|------------|-----------------|
| Draft  | 2026-09-17 | 1.8.9           |

## Implementation status (2026-09-17)

D1-D5 are implemented: `Controller.padlock_state()`, the `_padlock_engaged`/
`_padlock_dot_streak_since` state, the `stop_eject_sequence`/`padlock_camera`/
manual-press-hotkey hooks, `Controller.note_padlock_center_dot`, and
`TargetTracker.detect_padlock_center_dot` with its own `padlock_center_
indicator` config block (`config.yaml`/`config_schema.py`/
`controller_config.py`). Wired into `BehaviorTreeHandler.tick()`
(`tick_handlers.py`), battle-state-gated, the same call-site shape as HLDD
001's terrain reading.

**Still purely observational, per Non-Goals 2-3**: nothing reads
`padlock_state()` for gating or actuation yet — this cycle only builds and
wires the signal itself. Unit tests cover the detector against all 8 real
`test_screenshots/padlock_off/*.png` frames (copied there from the
operator's own capture, `tests/test-output/padlock_off/`, gitignored per
ADR 100 D7) plus synthetic edge cases, and the full D2-D5 state machine
(`tests/test_controller_no_keyboard.py`, `tests/test_target_tracking.py`).
Full gate green (`make lint && make test`). Open Questions 1-2 (padlock-ON
comparison, and whether the fusion condition is reachable long enough in
practice) are unresolved — next step is a live session logging
`padlock_state()` transitions with no consumer wired, before either HLDD
001 re-graduation or `ensure_padlock_off` reuse are considered.

**Update (2026-09-18)**: Open Question 2 is resolved (reachable, not rare —
see "First live trial"). Open Question 1 is quantified but not closed — two
full sessions (5,066 presses) show pressing roughly doubles the dot's flip
rate over baseline, including in the specific context that looked
concerning after the first trial (see "Second live trial"). Still purely
observational; still no consumer wired. The controlled ground-truth test
remains the recommended next step before any consumer is added.

## Context

The padlock camera re-points the view at whatever it has locked, instead of
looking forward along the aircraft's own nose. During ordinary `mission_j20`
operation this isn't occasional — `_start_search_and_destroy_locked`'s
`_padlock_loop` (`controller.py:2207-2217`) presses `padlock_camera()` on an
unconditional ~6s cadence (gated only by `self._padlock_cooldown_until`) for
as long as the search-and-destroy loops are alive, i.e. almost the entire
battle, not just a window around respawn.

This matters now because HLDD 001 Phase 1 (forward sky-occlusion
terrain-ahead detector) reads whatever the capture currently shows and
assumes it is looking forward. The operator flagged this directly tonight —
*"whenever the padlock camera activates it changes the view to look at the
enemy, this can interfere with terrain detection"* — and then escalated it
from a narrow respawn-window workaround to a general requirement: *"we need
to reliably detect the padlock camera mode, this will be important in the
future as well for other jets and modes."* `docs/hldd/001-terrain-avoidance-
hldd.md` was un-graduated back to `shadow: true` the same day specifically
because of this gap (see its Open Question 6), and its own Config section
now states re-graduation needs "a padlock-engaged detector [that] exists and
is itself validated" first. This ADR is that detector.

**No reliable signal exists today.** Three things were checked, in order,
before writing anything new:

1. `Controller._padlock_engaged` (`controller.py:272`, typed `bool | None`)
   is written exactly once in the whole codebase — `False`, inside
   `ensure_padlock_off` (`controller.py:1893`) — and read nowhere except one
   test assertion (`tests/test_eject_heatdive.py:336`). It is a tri-state
   slot that was clearly intended for this purpose and never wired up.
2. `TargetTracker.detect_padlock_off` (`tracker.py:186`, ADR 136 D4) is
   **confirmed broken**: it was built around a dashed green ring, recalibrated
   once against a live capture, then shown by a second live capture to not be
   fixed at screen center at all — it tracks a flight-path/velocity-vector
   marker that moves with aircraft attitude, not padlock state. Two
   before/after frames that looked like confirmation were "very likely
   coincidences — the marker happened to drift through the fixed detection
   region at those particular moments." `ensure_padlock_off`'s auto-toggle
   call site has been disabled ever since
   (`eject_closed_loop.heatdive_padlock_verify: false`,
   `config.yaml:766`) because blindly toggling the real key against a signal
   that measures something else was "actively worse than doing nothing."
3. **This session independently re-derived the same lesson before writing
   this ADR.** Earlier tonight, while investigating the operator's padlock
   report, a fix was drafted reusing `detect_padlock_off` — caught, before it
   was tested or shipped, against ADR 136 D4's own finding, and fully
   reverted. This ADR does not reuse that detector or that region.

**New evidence.** The operator ran a live session and captured 8 real
screenshots directly (not wingman's own evidence-capture mechanism — manual
screen captures), all confirmed padlock-off throughout:
`tests/test-output/padlock_off/screenshot_20260917_{062804,063103,063139,
063144,063149,063210,063216,063218}.png`, spanning level cruise, hard banked
turns, an enemy near boresight with a name/distance/type label, gun/lock
warnings, and a dive toward the water — real combat variety, not one
convenient frame.

Directly measured from all 8 (script-driven, OpenCV `BGR2HSV`, not eyeballed):
a small, **solid** (not dashed, not translucent) green dot sits at
**exact screen center** — bounding box `(959,603)`-`(961,605)` at 1920x1200,
i.e. fractional `(0.4995, 0.5025)`-`(0.5005, 0.5042)`, about 3x3px. Its color
is **byte-for-byte identical across all 8 frames**: OpenCV HSV `(55, 76,
224)` (48 sampled pixels, zero variance), regardless of roll, pitch, whether
an enemy is near boresight, or where the ADR 136 D4 ring itself has drifted
to in that same frame (confirmed separately off-center in the banked shots,
at completely different screen positions each time). This is strong evidence
it is a **different, fixed HUD element** from the moving ring ADR 136 D4
already ruled out — not a recalibration of the same mistake. A cropped,
4x-zoomed sample (`screenshot_20260917_063218.png`, a hard-banked frame where
the ring itself was nowhere near center) confirms it visually, not just by
pixel count.

**What this evidence does NOT show.** Only padlock-off frames were reviewed
here — no padlock-ON reference frames were available for direct comparison
in this pass. That this same dot disappears, moves, or changes when padlock
is genuinely engaged is the operator's own assertion from having watched
this live; it is not independently re-confirmed by this analysis. Flagged
below as the first thing to verify, not assumed.

## Decision

**D1. A tri-state signal, not a per-frame boolean.** Padlock state is
`False` (confirmed off), `True` (confirmed on), or `Unknown` (no confirmed
information) — never inferred from a single frame read in isolation, and
never flipped to `True` by anything in this ADR (see Non-Goals). Reuse
`Controller._padlock_engaged` as the backing field rather than adding a
parallel one — it is already typed `bool | None`, already exists at the
right place in `__init__`, and today is simply never driven. Add a public
accessor, `Controller.padlock_state() -> "bool | None"`, so callers (HLDD
001's terrain trigger, and any future forward-view-dependent feature) read
it the same way `is_mission_running()`/`is_climbing()` expose other
Controller-owned state.

**D2. Signal 1 — respawn sets `False`.** The game restores a forward/chase
view on every respawn; this is already the implicit assumption behind
`stop_eject_sequence`'s own docstring ("a respawn or a match ending" both
restore the primary loadout) and matches the operator's first framing of
this issue (a padlock lock during the opening seconds of a fresh life).
Hook `stop_eject_sequence` (`controller.py:4648`, called from
`tick_handlers.py:366` and `:555` on respawn detection) to also set
`self._padlock_engaged = False`. This is the highest-confidence signal in
this design — it needs no visual confirmation at all.

**D3. Signal 2 — any `PADLOCK_CAMERA` press sets `Unknown`.**
`PADLOCK_CAMERA` (`'p'`, `keybindings.py:34`) is a pure toggle with no on/off
argument (ADR 136's own finding) — a press changes state to *something*, and
without a working visual detector there is no way to know which way. Rather
than guess or count presses (the exact mistake ADR 136 D4's post-mortem
already named as "actively worse than doing nothing"), any press clears
confidence entirely. Two hook points, both already-existing choke points,
not new ones:

- `Controller.padlock_camera()` (`controller.py:1415`) — the single
  method every programmatic press already funnels through
  (`_padlock_loop`'s 6s auto-cycle, `ensure_padlock_off`'s retry presses,
  `padlock_target_switch`'s double-press). Set `self._padlock_engaged = None`
  at the top of this method, before the key press.
- The existing manual-press hotkey handler, `padlock_key_pressed`
  (`controller.py:909-921`, registered via `keyboard_module.on_press_key`),
  which already distinguishes a genuine manual press from a programmatic
  echo via the same `_programmatic_key_counts`/`_prog_release_grace_until`
  guard every other watched maneuver key uses (SAF-001 convention). Set
  `self._padlock_engaged = None` there too, inside the existing `if`
  branch that already fires only for real manual presses — this covers the
  case where the operator presses `p` directly against the game, entirely
  outside `padlock_camera()`'s call graph.

**D4. Signal 3 — a fused, corroborated confirmation sets `False`.** The new
center-dot detector alone is not trusted as a sole source of truth (its
padlock-ON behavior is unverified — see Context) — it only sets state when
corroborated by operational context that narrows the false-positive risk:

> `is_mission_running()` **and** `is_secondary_weapon_active()` **and** the
> center-dot detector reads positive on every sampled frame for a
> continuous 2 seconds (wall-clock, not tick-count — reset by any negative
> or missing read, the same "consecutive agreeing reads" shape
> `terrain_avoidance.confirm_reads` and `boundary.recover_below_time_s`
> already use elsewhere in this file) → `self._padlock_engaged = False`.

New detector: `TargetTracker.detect_padlock_center_dot(frame)` — a **new**
method, not a modification of the broken `detect_padlock_off` (ADR 136 D4
stays exactly as-is; this is a different element with its own config
block, `padlock_center_indicator`, mirroring `padlock_indicator`'s existing
shape in `config.yaml`/`config_schema.py`):

```yaml
padlock_center_indicator:
  region_pct: [0.485, 0.47, 0.515, 0.53]   # tight, fixed at true center —
                                            # deliberately NOT tracking the
                                            # moving ADR 136 D4 ring
  green_lower: [50, 60, 200]
  green_upper: [60, 90, 255]
  min_pixels: 3
```

The measured value is `(55, 76, 224)` with zero observed variance across 8
frames; the bounds above pad it modestly for compression/scaling headroom in
live capture, the same margin-over-measurement approach `terrain_avoidance`'s
`sky_hsv` already used. A fixed, narrow crop is a deliberate choice, not an
oversight: it will simply fail to detect (stays `Unknown`, never wrong)
during the maneuvers where the ADR 136 D4 ring drifts elsewhere on screen —
that is the safe failure mode for a fusion signal that only ever needs to
catch *some* 2-second window, not every frame.

**Why this needs `is_mission_running()` and `is_secondary_weapon_active()`
at all, rather than the dot alone**: the dot's presence has only been
measured during confirmed-off frames; gating it behind a narrower
operational context (armed, in-mission, secondary loadout selected) reduces
how much surface area a false read (some other green pixel drifting into a
3x3 crop) can affect, versus trusting it unconditionally on every tick of
every game state including menus, loading screens, and cutscenes where
nothing about this analysis has been checked at all.

**D5. Everything else holds the last known state.** A tick where none of
D2/D3/D4 fires does not change `padlock_state()` — it is not re-derived
every frame, only updated on these three specific transitions. Any caller
that needs "definitely safe to trust the forward view" must check
`padlock_state() is False` explicitly, not `!= True` — `Unknown` is
untrusted, the same conservative reading HLDD 001's Open Question 6 already
calls for.

## Non-Goals

1. **Not a positive "padlock is ON" detector.** No signal in this design
   ever sets `True`. A real ON-detector is future work (see Open Question 1)
   — until one exists, `Unknown` is the honest answer for "padlock might be
   on," and every consumer must treat it that way.
2. **Not re-enabling HLDD 001 Phase 1's actuation.** Wiring
   `terrain_avoidance`'s trigger to gate on `padlock_state() is False` is a
   follow-on change to `tick_handlers.py`, made only after this detector has
   its own live validation — re-graduating the terrain trigger itself stays
   the operator's own explicit decision, per HLDD 001's existing framing of
   its 2026-09-16 graduation.
3. **Not re-enabling `ensure_padlock_off`'s auto-toggle.** ADR 136 D4's
   ring-based detector and its disabled call site are untouched. A future
   ADR could point `ensure_padlock_off` at this new signal instead, but that
   is a separate decision with its own live-trial needs (the auto-toggle
   actively presses a key up to 3 times based on what it reads — a much
   higher-consequence use of a signal than passively gating a climb
   trigger).
4. **Not broadening `is_secondary_weapon_active()`.** It stays exactly what
   ADR 136 defined — true only once the current eject/heatdive dive has
   pressed `SWITCH_WEAPON`, cleared by `stop_eject_sequence()`. See Open
   Question 2 below for why this matters here.

## Open Questions

1. **Padlock-ON comparison is unverified.** Everything measured in this ADR
   is from confirmed-off frames. Before D4 is trusted live, capture real
   padlock-ON reference frames (deliberately toggle it and screenshot,
   mirroring how ADR 136 D4's own before/after comparison was done) and
   confirm the center dot actually differs — disappears, changes color, or
   moves — while padlocked. If it turns out to be present in both states,
   D4 as designed produces confident false confirmations and needs
   rethinking before it ships, not after. **Quantified (not yet closed) by
   the second live trial below**: across 5,066 real presses and 22,773 raw
   reads over two full sessions, pressing roughly doubles the dot's
   flip rate versus a measured no-press baseline (32-36% vs 15-17%) — a
   real statistical effect, not noise — and the specific context that
   produced zero flips in the first trial (`secondary_weapon=True`) shows
   the *highest* flip rate of any stratum measured (60-65%), overturning
   that as a systematic concern. Ground-truth confirmation (a human-verified
   "padlock is on right now" frame) still does not exist — the controlled
   test is still the one thing that would close this fully — but the
   statistical case now favors this detector tracking something real.
2. **`is_mission_running()` may rarely coincide with
   `is_secondary_weapon_active()` for a full 2 seconds.** `mission_j20`'s
   runner thread polls `_mission_cancel` every 0.5s
   (`controller.py:4262`) and `eject_and_dive` calls `cancel_mission()` at
   its own start, before `switch_weapon()` ever runs — so
   `is_mission_running()` may already read `False` for most of the window
   `is_secondary_weapon_active()` is `True`. If live measurement confirms
   this, D4's fused condition as written may be satisfied rarely or never
   during an actual heatdive, and either the `is_mission_running()` leg
   needs reconsidering (e.g. "not GAME_LOBBY" instead of the strict mission
   lock) or `is_secondary_weapon_active()` needs broadening beyond its
   current heatdive-only scope (Non-Goal 4) — do not guess which before the
   measurement exists. **Measured by the first live trial below: this
   speculation was wrong.** The condition is reachable, and not rarely —
   one streak held 15+ seconds. The actual mechanism is an eject that aborts
   without a respawn (Anomaly 003's path), which leaves
   `is_secondary_weapon_active()` stuck `True` while `is_mission_running()`
   stays `True` throughout. A more specific concern replaces this one — see
   the trial section's own writeup, which folds back into Open Question 1.
3. **Resolution/aspect independence is untested.** All measurements here are
   from 1920x1200 captures. `region_pct` is fractional, matching this
   project's existing crop convention, so it should generalize — but that
   has not been confirmed against any other capture resolution this project
   supports.
4. **False-positive risk beyond these 8 frames is unmeasured.** Respawn
   screens, post-match reward overlays (the "Choose Rewards" stall class
   ADR 137's eighth trial found this session), and other HUD states have not
   been checked for a stray match to `(55, 76, 224)` inside the tight center
   crop. Shadow-mode logging (Testing plan below) exists specifically to
   surface this before anything acts on the signal.

## Consequences

**Positive:** gives HLDD 001's terrain trigger (and any future
forward-view-dependent feature — the operator specifically flagged "other
jets and modes" as a reason this needs to be general, not a one-off) a real,
measured, narrowly-scoped signal for "definitely not padlocked," without
reusing or patching the detector ADR 136 D4 already proved wrong. Reuses an
existing dead field (`_padlock_engaged`) and two existing choke points
(`padlock_camera()`, the manual-press hotkey handler) rather than adding new
call-site surface area.

**Costs / risks:** a new config block and detector to maintain; the
`Unknown` state will be the common case early on (most ticks fire none of
D2-D4), so early consumers must be written to fail closed, not to assume
"not `True`" is good enough. The fusion condition's real hit rate is
unmeasured (Open Question 2) — this may ship providing far fewer confirmed
`False` windows than hoped until that is resolved.

## Testing plan

Shadow-first, the same discipline HLDD 001 Phase 1 and ADR 073 both already
established for this codebase:

- **Unit**: `detect_padlock_center_dot` against the 8 real captured frames
  (added as a small corpus, same convention as `test_screenshots/`) — all 8
  must read positive; plus synthetic frames (no dot, dot shifted outside the
  crop, a near-color decoy elsewhere in frame) for the negative/edge cases.
  State-machine tests for D2-D5: respawn forces `False` regardless of prior
  state; any `padlock_camera()` call forces `Unknown` regardless of prior
  state; a manual press (via the existing hotkey-handler test harness) does
  the same; the fused condition requires all three legs AND the full 2s
  window, and a single missed read mid-window resets the streak rather than
  forgiving it.
- **Live**: log every state transition and every raw detector read for a
  full session before wiring `padlock_state()` into anything — no actuation,
  no gating, purely observational, mirroring HLDD 001 Phase 1's own
  shadow-mode precedent. Specifically watch for: how often D4's fused
  condition is even reachable (Open Question 2), and any transition to
  `False` that a manual operator check contradicts (Open Question 1) before
  trusting the signal for anything.

## First live trial (2026-09-17)

Restarted with the implemented code (previous session predated it), purely
observational — nothing consumes `padlock_state()`. Session health over the
watched window: 135 respawns, 8 `crash_with_missiles` (~6%), zero
tracebacks/errors from the new code paths. Unrelated to this ADR: normal
background crash variance, none traced to today's changes.

**Open Question 2, reachability — measured, not just theoretical.** The
fused condition fired multiple times, both traced to the same real
mechanism: `eject_and_dive` presses `SWITCH_WEAPON` (setting
`is_secondary_weapon_active() = True`), then aborts via the Anomaly 003
"telemetry confirms aircraft alive and flying" path — **without a
respawn**, so `stop_eject_sequence()` never fires and the flag stays `True`
into ordinary post-abort `mission_j20` play. `is_mission_running()` is
`True` the whole time (the eject never actually took the mission lock away
for long, contrary to this ADR's own speculation in Open Question 2 below).
One streak held continuously from a first confirmation at 3.0s up to at
least 15.0s before the watch moved on — not a one-tick fluke.

**Open Question 1, a new and more specific concern — NOT resolved, flagged
here rather than guessed at.** During that same long streak, at least three
real `padlock_camera()` presses fired (09:35:44.478, :50.601, :53.130 —
confirmed from the log, not inferred), each of which correctly reset
`padlock_state()` to `Unknown` per D3. Every time, the very next tick's
fusion check already found the center dot still present and re-confirmed
`False` again — the dot never once read absent across three real toggles of
the key. Two explanations are both consistent with this and neither is
confirmed: (a) the dot genuinely does not change when padlock is pressed in
this specific context (a real problem — same failure class as ADR 136 D4's
ring), or (b) `PADLOCK_CAMERA` may not actually re-engage the camera at all
under these conditions (mid/post-eject-abort, `rings=0/0/1`-`0/0/4` — long
contacts present, nothing confirmed near boresight) — i.e. the toggle may be
conditional on having something to padlock onto, which would mean it
genuinely wasn't toggling here regardless of the key press landing. This
session's evidence cannot distinguish the two. Do not treat D4 as validated
until a controlled test (deliberately padlock onto a nearby, boresight
target and check the dot) settles it — this is now the single most
important open item before HLDD 001 re-graduation is even considered.

**Follow-up the same session, strengthens rather than resolves the
concern.** A later streak (first confirmed 09:40:28, 13.5s in) was followed
three seconds later by a burst of **three more** presses in quick
succession: one solo (09:40:33.234) then two back-to-back via
`padlock_target_switch` (09:40:35.798/:36.253 — the "2 missiles fired —
switching padlock target" cadence, `controller.py:1420-1433`). Each reset
`padlock_state()` to `Unknown` as designed. But `_padlock_dot_streak_since`
— which only resets when the *detector itself* reads negative, not on a
press — was never broken across any of them: the fusion re-confirmed
`False` at the very next tick every single time, and a fresh log line at
09:40:37.308 shows only 3.0s elapsed since the streak's own true start
(~09:40:34.3, immediately after the first of these three presses), meaning
the raw detector read positive on every poll through this entire second
burst too. That is six real presses total across two episodes this
session, zero of which coincided with the center dot ever reading absent.
This does not confirm hypothesis (a) or (b) above — it is still consistent
with either — but it raises the bar for what "the dot changes when
padlocked" would need to look like if it's true, and makes it less likely
this is simply poll-timing luck.

## Second live trial (2026-09-18) — Open Question 1, quantified

Same day, added one line (`Controller.note_padlock_center_dot`) logging the
**raw** per-tick detector read (`padlock center-dot raw=%s mission=%s
secondary_weapon=%s`) alongside the fused end-state, specifically so a
later session could correlate real presses against the raw signal directly
instead of inferring it from `padlock_state()` transitions the way the
first trial had to. Landed mid-session (did not require a restart to ship —
queued for the next natural one), gate-verified, no live risk since it is
pure logging. Two full sessions ran afterward with it in place:
`logs/wingman_20260918_010822.log` (2026-09-17 17:59 - 2026-09-18 01:08,
7h09m) and the current `wingman.log` (01:09 - 06:05, 4h56m) — 3,077 + 1,989
= 5,066 real `padlock_camera()` presses, 13,345 + 9,428 = 22,773 raw reads,
reviewed the morning after via `git log`-adjacent script analysis, not
guessed at.

**Baseline (no-press) noise rate, measured first so the press comparison has
something to be compared against:** across consecutive raw reads 0.8-3.0s
apart with **no press anywhere between them**, the dot's value flips on its
own only 15.1-17.4% of the time (10,469 and 7,618 comparable pairs). This is
the rate against which "does pressing do anything" must be judged — not
against 0%.

**Isolated presses (no other press within 3s either side, so a rapid
double-press can't corrupt a single before/after comparison) — 1,895 and
1,051 comparable instances:** the dot flips **31.6-35.8%** of the time in
the 0.8-3.0s after a press — roughly **double** the no-press baseline in
both sessions independently. This is a real, repeated, statistically
distinguishable effect, not noise: pressing the key measurably changes what
the detector reads, at a rate clearly above the do-nothing baseline.

**The specific worry from the first live trial — checked directly, and
overturned.** Stratifying isolated presses by `secondary_weapon` at press
time (the exact condition both of yesterday's zero-flip episodes shared)
gives the opposite of what those two episodes suggested: flip rate is
**60.7-65.4%** when `secondary_weapon=True` (61 and 52 comparable presses)
versus **29.8-35.0%** when `secondary_weapon=False` (1,834 and 999
comparable presses) — higher in the heatdive-like context, not lower or
zero. Getting zero flips across six presses in that context, as observed
2026-09-17, is consistent with a small-sample outcome (roughly 0.2-0.4%
under this session's own measured rate for that context) landing on an
unlucky run — most plausibly explained by no valid lock target being
present in that *specific* window (the log showed only long-range minimap
contacts, `rings=0/0/1`-`0/0/4`, nothing confirmed near boresight), not by
a systematic flaw tied to the eject-abort context itself.

**Still not fully resolved — the gap that remains is narrower now, not
closed.** This establishes a real, repeatable *statistical* correlation
between pressing and the dot changing, well above chance, across two
independent full sessions. It does not yet establish *ground truth* — no
frame in this analysis has been independently confirmed (by a human, or a
second detector) to show the camera actually locked onto a target at the
moment the dot read `False`. A ~32-36% overall flip rate is also
consistent with "padlock only actually engages a minority of presses"
(no valid target most of the time) layered on top of a detector that
tracks it correctly — which is a different, milder finding than "the
detector is unreliable." The controlled test from the first trial (padlock
deliberately onto a nearby, boresight target, screenshot before and after)
is still the one experiment that would close this decisively, but the
statistical case for proceeding to that test — rather than abandoning this
detector — is now much stronger than it was after the first trial alone.

## Related Documents

- `docs/adr/136-heatseeker-dive-invokable-mode.md` — D4's broken
  `detect_padlock_off` (not reused here), `ensure_padlock_off`,
  `is_secondary_weapon_active`/`_eject_weapon_switched`, and the `p`-is-a-
  pure-toggle finding this ADR builds directly on.
- `docs/adr/137-emergency-climb-airbrake-and-crash-instrument.md` — same
  investigative session; unrelated mechanism (a climb pitch-pulse
  regression, already fixed), cited here only for the session timeline.
- `docs/hldd/001-terrain-avoidance-hldd.md` — the consumer this detector
  unblocks; its Open Question 6 and its 2026-09-17 `shadow: true` reversion
  both name this exact gap.
- `tests/test-output/padlock_off/screenshot_20260917_*.png` — the 8 real
  frames this ADR's measurements are drawn from.
