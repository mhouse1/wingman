# ADR 136 — Tracking-Guided Roll During the Automatic Eject Dive

| Status | Date       | Wingman Version |
|--------|------------|-----------------|
| Draft  | 2026-09-09 | 1.8.9           |

## Context

When `AmmoEventsHandler.handle_no_missiles` confirms zero primary missiles
(`wingman/tick_handlers.py:618-667`, debounced against
`no_missiles_consecutive_required`), the current path is `fire_eject()`
(`tick_handlers.py:675-689`) into `Controller.eject_and_dive`
(`controller.py:1762-1776`, ADR 069/058). That is a deliberate, working
design, not a bug: ADR 106 and ADR 109 both state the reasoning explicitly —
*"Eject exists to trade an empty airframe for a rearmed one, and dying is the
point"* (ADR 106) and *"an aircraft with no missiles is worth trading for a
rearmed one, and the dive is how the trade is made"* (ADR 109). The
closed-loop descent controller (`_eject_descent_control`, ADR 069) alternates
bounded `NOSE_DOWN` pulses with a hands-off ballistic phase until respawn,
over-rotation guard, pulse budget, or timeout ends it. The operator's own
framing for this ADR: the aircraft "crashes into the ground" — accurately
describing what the design already intends.

The improvement opportunity is that nothing during that dive tries to
extract further value from the airframe. Two things make that possible that
the codebase does not yet use together:

1. **`SWITCH_WEAPON` exists and is unused.** `wingman/keybindings.py:30`
   defines `SWITCH_WEAPON = 'g'`, imported into `controller.py:117`. Grepping
   the whole repo turns up zero call sites that press it — only keybinding-
   collision tests reference the constant. MetalStorm binds a secondary
   heat-seeking loadout to this key; wingman has never pressed it. Operator
   confirmation (2026-09-09): pressing it reveals a fixed secondary loadout
   of **two** heat-seeking missiles, and **the existing `AMMO_MISSILE` crop
   is reused** for the secondary count — the game redraws the same HUD
   region to show whichever weapon is currently selected. This means
   `Analyzer.get_ammo_missiles()` reads the secondary count for free once
   switched: no new crop, no new calibration pass. See Decision D1 step 4.
2. **Target tracking (Design 005) now has a validated sensing path that runs
   outside `GAME_BATTLE`'s normal actuation gate.** This session split
   `TrackingHudHandler` into sensing (runs in `GAME_BATTLE` and
   `GAME_BATTLE_MANUAL`, no key press) and actuation (`tracking.actuate`,
   still strictly gated to `GAME_BATTLE` + running mission). The roll
   controller itself, `Controller.orient_nose_to_target(error_norm)`
   (`controller.py:1277-1312`), has been live-reachable since Design 003/005
   but per Design 005's own Non-Goals is roll-only — no pitch.

Put together: while the aircraft is diving anyway — an airframe already
being deliberately sacrificed, per ADR 106/109 — switch to the secondary
heat-seeker loadout and roll toward whichever enemy the tracker selects
using the sensing/actuation infrastructure that already exists. Whether this
meaningfully extends a life's kill count before it ends is exactly what
needs live measurement.

### Revision note (2026-09-09)

An earlier draft of this ADR proposed a standalone `mission_heatdive`
mission mode with its own hotkey, built on the same
`mission_j20`/`mission_loiter` shape (own `_mission_lock` ownership, own
thread). Review surfaced that this would race the *existing* automatic eject
path rather than replace or coexist cleanly with it:
`handle_no_missiles` only suppresses itself while
`not self._ctrl.is_mission_running()` (`tick_handlers.py:618-620`), and
`is_mission_running()` is exactly `self._mission_lock.locked()`
(`controller.py:4136-4138`). A second mission holding that same lock would
satisfy that gate, not trip it — so `fire_eject()` → `eject_and_dive()` would
still fire automatically the moment missiles hit zero, in parallel with the
new mode's own pitch control, both pressing `NOSE_DOWN`/`AFTERBURNER` and
mutating overlapping `self._eject_*` state. Reusing `_eject_descent_control`
directly compounded this: it is a zero-parameter, all-`self` method that
`eject_and_dive` initializes extensively before calling
(`controller.py:1777-1801`, including the `self._ejecting` reentrancy guard
that a second caller would never touch), so calling it from an independent
thread meant either duplicating that init (fragile) or racing the real
sequence outright.

Operator direction: fold the tracking-guided roll into the eject sequence
that already fires automatically, instead of building a parallel path. The
Decision below reflects that — see Consequences for why this also produces a
*better* validation path for Design 005 than a manually-invoked mode would
have.

## Implementation status (2026-09-09)

D1–D3 are implemented: `Controller.switch_weapon()`, `Controller.set_target_tracker()`,
`Controller._eject_heatdive_loop()`, the `eject_closed_loop.heatdive_enabled`
config key (`config.yaml`/`config_schema.py`), the `main.py` tracker wiring,
and the `eject_and_dive()` start/stop integration are all in place and
covered by `tests/test_eject_heatdive.py`. `heatdive_enabled` defaults
`false`, so this is a no-op change until enabled. One correction found
during implementation, not caught by review: `self._eject_stop` is **not**
set on the dive's natural completion (only external cancellation sets it —
see `controller.py:1943` finally block), so the heatdive thread cannot rely
on that event alone to know when to stop. Fixed by having `eject_and_dive`'s
own `finally` block explicitly signal and join a per-call `heatdive_stop`
event, in addition to the loop watching `self._eject_stop` for prompt
reaction to external cancellation.

**First live trial (2026-09-09, `heatdive_enabled=true`), two dives, both
false-aborted after ~8s** with `Controller: eject_and_dive — ABORT, 2
missile(s) rearmed mid-descent (ADR 088)`. Root cause: ADR 088's rearm-abort
check (`controller.py`, inside `_eject_descent_control` and the post-descent
hold loop) reads `Analyzer.get_ammo_missiles()` and treats any nonzero
result as the primary rack having refilled. It was written before ADR 136
existed and has no way to know `switch_weapon()` just repointed that same
crop at the fixed two-round secondary loadout — so it read "2" and aborted
the dive almost immediately every time, defeating the whole point of this
ADR. This was very likely also the source of an operator report during the
same session ("padlock is still toggling, padlock should not toggle after
primary missile run out"): the false abort ends the dive early, the mission
auto-restarts, and `search_and_destroy_loop`'s padlock cycling resumes
within seconds — reading exactly like "padlock never stopped" from the
operator's seat, even though it *did* stop correctly at eject start each
time (confirmed in the log: `search_and_destroy padlock loop stopped` fires
right on cue both times).

**Fix**: a new `self._eject_weapon_switched` flag, set the moment
`switch_weapon()` succeeds inside `eject_and_dive`, reset to `False` at the
top of every `eject_and_dive()` call. Both ADR 088 rearm-abort check sites
now skip themselves while it's `True` — deliberately trading away ADR 088's
mid-dive rearm protection for the rest of *this* dive once heatdive has
switched weapons, since the shared crop can no longer distinguish "primary
rearmed" from "secondary loadout active." Covered by
`test_rearm_abort_check_skips_secondary_loadout_reading` (the fix doesn't
false-abort), `test_rearm_abort_check_still_fires_without_a_weapon_switch`
(ADR 088's original protection is untouched when heatdive never switches),
and `test_eject_and_dive_resets_weapon_switched_flag_per_dive` (no stale
flag from a previous dive). Also fixed in the same pass, found by directly
measuring recorded press durations in the same live log: `switch_weapon()`,
`fire_active_weapon()`, and `orient_nose_to_target()`'s roll calls were all
being cut to 0-11ms instead of their intended hold, because
`eject_and_dive` calls `cancel_mission()` before the heatdive loop starts
and `_execute_key_press`'s hold loop treats an already-set
`self._mission_cancel` as an immediate cancel signal. Fixed by adding
`ignore_cancel: bool = False` to `fire_active_weapon`, `switch_weapon`,
`roll_left`, `roll_right`, and `orient_nose_to_target` (mirroring the
existing `deploy_flares` precedent), and passing `True` from every call this
ADR's own code makes.

**Padlock-off requirement (2026-09-09, operator observation during the same
live session)**: `orient_nose_to_target`'s roll correction assumes the
on-screen error reflects where the *aircraft's nose* is pointed. With
padlock camera engaged, the view instead follows whatever the game has
locked, so the roll loop would be steering blind against a moving reference
it doesn't control. This wasn't caught in the original design — it surfaced
only once the operator watched a live dive and reported padlock still
toggling (the report that also led to finding the ADR 088 false-abort
above; both symptoms were visible in the same sessions, though only the
rearm-abort was confirmed as their root cause — this padlock-off
requirement is a separate, independently real correctness gap the same
observation prompted).

`PADLOCK_CAMERA` is a pure toggle with no on/off argument
(`Controller.padlock_camera`'s own docstring — confirmed by code search, no
existing on/off state tracking anywhere in the codebase), so the only
reliable way to know which state a press leaves it in is to look at the
screen, not to count presses. **D4** (below) adds a screen-verified
padlock-off check, `Controller.ensure_padlock_off()`, called once when
heatdive switches weapons, before the tracking-roll loop starts.

**D1-D3 live-validated (2026-09-09).** After the fixes above,
`eject.heatdive_enabled=true` ran across many consecutive live ejects in one
session with `heatdive_padlock_verify=false`: switch-weapon, tracking roll,
and fire all fired correctly each time, and — the specific thing being
checked — zero false `rearmed` aborts. Exit reasons across the run were the
legitimate ones only (`respawn_detected`, `established`-then-hold); the
`rearmed` exit reason, which fired on effectively every heatdive-enabled
dive before the fix (measured: 5 of 5 trials that day), did not recur once
across five consecutive dives after it. Status stays `Draft` regardless —
D4 is a known-open item (see below), and "validated" here means the
measured behavior this ADR set out to fix, not the whole feature surface
(no heat-seeker kill has yet been confirmed, for instance — Open Question 2
on the fire trigger is still open in spirit even though D1 step 4 shipped a
default answer).

## Decision

**D1. This is a config-gated addition inside `eject_and_dive`'s existing
thread, not a separate mission.** A new flag under the eject config block —
`eject.heatdive_enabled`, default `false` — is read once at the top of
`eject_and_dive`. When `true`:

1. Immediately after eject's existing init (the same point `eject_and_dive`
   already resets `_eject_stop`, `_eject_held_keys`, `_eject_phase_exit_reason`,
   etc., `controller.py:1777-1801`), press `SWITCH_WEAPON` once, bracketed
   with the project's programmatic-key convention
   (`_inc_programmatic_key`/`_arm_release_grace`/`_dec_programmatic_key`) so
   SAF-001's manual-takeover detection is never confused by this addition's
   own key echo.
2. `_eject_descent_control`'s pitch-down loop runs **completely
   unmodified**. This ADR reuses it, not duplicates it — no second pitch
   controller, no competing `NOSE_DOWN` pulses, no new stop-event category.
3. Alongside that loop — sharing `self._eject_stop` as its own stop signal,
   the same event the pitch loop already watches, rather than inventing a
   second interrupt mechanism (the reasoning ADR 135 already established
   against parallel, ungated key-press paths) — call
   `TargetTracker.update(frame)` + `Controller.orient_nose_to_target(error_norm)`
   toward whatever target the tracker selects, once per cycle the descent
   loop also runs.
4. Firing reuses `search_and_destroy_loop`'s own weapon-loop shape exactly
   (`_start_search_and_destroy_locked`'s `_weapon_loop`, `controller.py:1940-1965`):
   press `fire_active_weapon(hold_seconds=0.1, block=True)` on a fixed
   cadence (`self._weapon_loop_interval`, default 0.2s) with **no lock or
   tone detection** — the game's own weapon system decides when a shot
   actually releases, the same "just fires as soon as ready" behavior
   already relied on for primary missiles today (operator-confirmed,
   2026-09-09). This closes the one piece HLDD-011's `BoresightEngage`
   still needs (`ToneWait`/`LockConfirmed`) and this addition does not:
   heat-seekers don't need it. Stop firing the moment
   `Analyzer.get_ammo_missiles()` reads `0` (the reused `AMMO_MISSILE` crop,
   Context above) — the same debounced-read shape `handle_no_missiles`
   already establishes for the primary loadout, not a hardcoded two-shot
   counter. Eject's own `cancel_mission()` (called at the top of
   `eject_and_dive`) already stops `search_and_destroy_loop`'s weapon/padlock
   threads before this addition's own firing starts, so there is no
   double-fire overlap with the primary SDL loop.

Because this only adds to a sequence that already runs unconditionally on
every missiles-empty event, and touches neither `mission_j20` nor
`_mission_lock` ownership nor the gating in `handle_no_missiles`, there is
nothing left to race: eject fires exactly once, exactly as it does today.
This is purely what happens *during* that one dive.

**D2. Controller needs a stored reference to the tracker — this did not
exist and is new wiring, not reuse.** `Controller` currently holds no
`TargetTracker` reference at all; the dependency today runs the other
direction (`TrackingHudHandler` is constructed with `ctrl`,
`main.py:967-968`). Add `Controller.set_target_tracker(tracker)` (or an
equivalent constructor/setter), called once from `main.py` after both
objects exist. Frame access itself needs no new mechanism:
`Capture.grab_from_thread()` already exists and is already used from other
`Controller`-owned daemon threads (`controller.py:814`, `1054`, `4211`) —
mss's thread-local requirement is already solved for this shape of caller.

**D3. Termination is identical to `eject_and_dive`'s own existing
termination** — respawn detected, over-rotation guard, pulse/time budget,
the existing 120s-class timeout — because nothing new is introduced here.
The roll-tracking addition stops the instant `self._eject_stop` is set; same
event, no separate teardown path. SAF-011/012 ground-collision recovery is
unmodified and still applies underneath exactly as it does today.

**D4. Padlock is verified off, not assumed off, before the roll loop
starts.** `Controller.ensure_padlock_off(max_attempts=3)` reads a new
screen detector — `TargetTracker.detect_padlock_off(frame)`. Since
`padlock_camera()` is a pure toggle with no on/off argument,
`ensure_padlock_off` loops: check the screen, and only if the indicator is
absent, press `padlock_camera()` once and re-check — up to `max_attempts`
(default 3) before giving up and logging a warning. This fails open: it
does not block the dive if it can never confirm off, since the report that
prompted this was a correctness gap in the roll, not something that
threatens the aircraft the way a stuck key would. Called once, right after
`switch_weapon()`/`_eject_weapon_switched` and before the heatdive thread
starts — not re-checked continuously through the dive, since nothing else
presses `PADLOCK_CAMERA` once `cancel_mission()` has stopped
`search_and_destroy_loop` (verified: `_padlock_loop`'s own
`while ... and not self._mission_cancel.is_set()` guard,
`controller.py:2061`, plus `eject_and_dive`'s own wait for
`is_mission_running()` to clear before doing anything else). If a later
session finds padlock re-engaging mid-dive some other way, this would need
to move from a one-time check to a per-cycle one inside
`_eject_heatdive_loop` — not needed yet, so not built yet.

**Detector recalibrated against a live capture (2026-09-09), after the
first design missed the indicator on every trial.** The original detector
assumed a single small filled green dot and never found anything —
`ensure_padlock_off` logged "could not confirm padlock off after 3
attempts" on both of the first two redeployed dives. A frame grabbed
mid-dive (`DISPLAY=:3`, same mss path `grab_from_thread` uses) showed the
real indicator: a **dashed green ring** centered on screen (a small
crosshair dot at its exact center, but the ring is the dominant, reliably
visible feature), made of roughly 40 short dash segments, not one shape.
Measured directly from that frame: dash color H 40-75 / S 30-200 / V
100-255 — markedly less saturated than `tracking_hsv`'s enemy-marker green
(S >= 150), consistent with a translucent HUD reticle rather than a solid
marker; the ring spans roughly `[0.42, 0.39]` to `[0.67, 0.67]` of the
frame, well outside the original `[0.46, 0.44]`-`[0.54, 0.56]` region that
only covered the exact center pixel.

`detect_padlock_off` was rewritten to match: count contours sized like a
single dash (`min_contour_area`/`max_contour_area`, no aspect-ratio filter
since dashes are short curved strokes) and require at least `min_dashes`
(default 6, well under the ~40 observed, so partial occlusion by the
aircraft nose or HUD clutter doesn't cost a false negative) rather than
accepting any one matching contour — a single stray green pixel elsewhere
in the wider region would otherwise false-positive. Verified against the
same captured frame after the fix: `detect_padlock_off` returns `True`.
Config (`padlock_indicator`), synthetic tests (`TestPadlockIndicator`, now
drawing a dash ring via `_draw_dashed_ring` instead of one circle), and
this section were all updated together in the same pass.

**D4 disabled by default after redeployment showed the recalibrated
detector still failed live, intermittently (2026-09-09).** Two more dives
after the recalibration: one succeeded (`detect_padlock_off` confirmed off
on attempt 3), one failed all 3 attempts. Temporary diagnostic
instrumentation saved the exact frame each check saw. Comparing a
`off=False` frame against a `off=True` frame settled it: the dashed ring is
**not fixed at screen center** — in the failure frame it sat in the
upper-right of the screen during a banked turn; in the success frame it was
upper-left-of-center during a different attitude. This is consistent with a
**flight-path/velocity-vector marker** (common in this class of HUD,
showing actual travel direction vs. nose direction) that moves with flight
attitude, not a padlock on/off indicator at a fixed position. Both the
original hand-captured reference frame and the live `off=True` frame that
seemed to confirm the recalibration were very likely coincidences — the
marker happened to drift through the fixed detection region at those
particular moments, not because padlock state had actually changed.

Given this, blindly toggling the real `PADLOCK_CAMERA` key up to 3 times
per dive against a signal that was measuring something else entirely was
actively worse than doing nothing — the final padlock state after
`ensure_padlock_off` gave up was effectively unconstrained (0-3 net
toggles). Operator decision: **disable the auto-toggle call site, keep
D1-D3 running.** A new flag, `eject_closed_loop.heatdive_padlock_verify`
(default `false`), now gates whether `eject_and_dive` calls
`ensure_padlock_off()` at all — `Controller.ensure_padlock_off()` and
`TargetTracker.detect_padlock_off()` are left in place (and still directly
unit-testable) for a future attempt once the actual indicator is correctly
identified, rather than deleted. D1-D3 (weapon switch, tracking roll, fire,
the ADR 088 rearm-abort fix) are unaffected — the rearm-abort fix in
particular was independently confirmed correct across multiple live ejects
this session (`respawn_detected` and `established` exit reasons, no more
false `rearmed` aborts). This is an open item, not a closed one: identifying
the real padlock-off indicator (or an entirely different verification
approach) is still needed before D4 can be re-enabled.

## Non-Goals

1. **Not a separate mission mode.** Superseded by the revision above — no
   new hotkey, no new `_mission_lock` owner.
2. **Not a change to eject's trigger conditions, termination conditions, or
   pitch mechanics.** `handle_no_missiles`, `fire_eject`, and
   `_eject_descent_control`'s own logic are all unmodified.
3. **Not a full boresight lock-and-fire sequence.** HLDD-011's
   `BoresightEngage` (`ToneWait`/`LockConfirmed`) is a more general,
   lock-confirmed design for boresight-only airframes with no existing
   fire-and-forget option. This addition is narrower and does not need
   that: heat-seekers fire the same "hold the trigger, the game decides
   when it releases" way primary missiles already do (D1 step 4), so there
   is no lock cue to detect in the first place, not just a step being
   skipped.
4. **Not a change to the dive's own termination conditions.** Secondary-ammo
   reads (`Analyzer.get_ammo_missiles()`, reused `AMMO_MISSILE` crop) bound
   *firing* — stop pressing `FIRE_ACTIVE_WEAPON` once it reads `0` — but do
   not end the dive early. The dive still runs to whichever of eject's own
   existing guards (D3) fires first, whether or not both heat-seekers were
   used.

## Open Questions

1. **Resolved.** A secondary loadout of two heat-seeking missiles exists
   behind `SWITCH_WEAPON`, and it reuses the existing `AMMO_MISSILE` crop —
   `Analyzer.get_ammo_missiles()` already reads it once switched, no new
   crop or calibration needed (operator-confirmed, 2026-09-09).
2. **Resolved.** Heat-seekers fire the same continuous-hold, no-lock-needed
   way primary missiles already do via `search_and_destroy_loop`'s weapon
   loop; D1 step 4 reuses that shape directly (operator-confirmed,
   2026-09-09).
3. **Decided (default, not yet operator-reviewed):** don't special-case
   `Searching`. The tracker already returns `visible=False` with no target,
   which already produces no roll command from `orient_nose_to_target` —
   adding an explicit early-stop is extra state for no behavioral gain over
   just letting "no target" naturally mean "no roll," and it keeps this
   addition's loop shaped exactly like every other cycle of the descent
   controller it runs alongside (no early-exit branch to keep in sync with
   D3's termination guards).
4. **Decided (default, not yet operator-reviewed):** reuse Design 005's
   existing selection policy as-is (nearest-to-crop-center on first
   acquisition, then nearest-to-last-centroid persistence once locked) — no
   new code, and a persistent lock is arguably *more* useful mid-dive than
   re-targeting every frame, since it avoids rolling back and forth between
   two contacts that are both near center. Flagged as a default rather than
   folded silently into D1 because it is a real behavioral choice (a future
   session could reveal target-switching is actually preferable once this
   is watched live).
5. The original request's Phase 2 — "once refined it may activate as soon
   as the round starts instead of starting mission_j20" — does not map
   cleanly onto this ADR's mechanism anymore. A config-gated addition inside
   an existing automatic sequence has no natural path to becoming "the
   thing that starts the round" the way a standalone invokable mode would
   have. If that longer-term vision is still wanted, it needs its own
   follow-on design rather than being assumed to fall out of this one.

## Consequences

**Positive:** every missiles-empty event happens automatically, many times
per session, on an airframe already being deliberately sacrificed (ADR
106/109). Enabling `eject.heatdive_enabled` turns each of those
already-occurring, already-doomed dives into a free trial of Design 005's
roll controller — with **zero risk to `mission_j20`'s own engage/attack
behavior**, since nothing about when or why eject fires changes, only what
happens during it. This is a materially better validation path for HLDD-005
than a manually-invoked mode would have been: no operator action required
per trial, and rep count comes for free from normal play. If the
tracking-guided roll also lands occasional heat-seeker kills before the
aircraft would have ejected anyway, that is pure upside over the current
all-loss outcome. The config flag is a single, independent on/off switch —
disabling it returns the eject sequence to exactly its current behavior with
zero code-path difference.

**Costs / risks:** this still adds continuous roll actuation during a
maneuver that is, by design, headed toward the ground faster than level
flight. SAF-011/012 remain the backstop. The risk surface is meaningfully
smaller than the original standalone-mode design, though — there is no
second thread and no second pitch controller to race, only one new signal
(tracker-driven roll) layered onto a sequence whose triggering and descent
mechanics are completely unchanged. D2's Controller-to-tracker wiring is
still real, not-yet-done work and should land before any of this can run.

## Related Documents

- `docs/adr/069-eject-impulse-rotation-and-ballistic-descent.md` — source of
  the `NOSE_DOWN` impulse-rotation primitive this addition runs alongside,
  unmodified.
- `docs/adr/058-eject-dive-confirmation-via-raw-descent-rate.md` — the
  raw-descent-rate confirmation criterion `eject_and_dive` uses.
- `docs/adr/106-return-to-battle-rate-tracking.md`,
  `docs/adr/109-eject-yields-to-the-survival-hold.md` — source of the
  "trade an empty airframe for a rearmed one" framing quoted above.
- `docs/adr/135-disengage-roll-ignored-manual-takeover.md` — precedent for
  reusing a single shared stop event rather than inventing a parallel,
  ungated interrupt path.
- `docs/hldd/005-target-tracking-hldd.md` — sensing and roll-controller core
  this addition calls directly; updated alongside this ADR.
- `docs/hldd/011-acs-mode-hldd.md` — the broader boresight/target-priority
  decision layer this addition is a narrower, earlier proof point for;
  updated alongside this ADR.
- `docs/requirements/001-safety.sdoc` (SAF-001) — manual-takeover scope this
  addition's key presses must respect.
