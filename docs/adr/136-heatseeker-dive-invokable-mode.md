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
