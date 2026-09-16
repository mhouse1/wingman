# ADR 135 — Disengage Roll Ignored Manual Takeover

| Status | Date       | Wingman Version |
|--------|------------|-----------------|
| Draft  | 2026-09-09 | 1.8.8           |

## Context

Reported live: pressing Enter to take manual control, the padlock camera kept
moving on its own during manual flight. The operator's framing was exact and
non-negotiable: *manual control should only be manual control*.

`release_for_manual_takeover()` (SAF-001) stops every tactic hold by setting
a dedicated `threading.Event` each one polls — `_eject_stop`, `_me_stop`,
`_climb_stop`, `_boundary_turn_stop`, `_sg_stop` — then calls
`cancel_mission()` and `stop_search_and_destroy_loop()` directly. Every
padlock/weapon-fire call also routes through `_execute_key_press`, which
independently checks `self._manual_takeover_active()` and suppresses
everything except flares.

`disengage_roll_right` (the no-enemy-for-30s tactic, ADR 024 3.1b) had none
of this. Investigation found:

1. **No stop event existed for it.** `release_for_manual_takeover()`'s list
   above never mentioned disengage at all — not an oversight in one call, a
   whole category missing.
2. **Its ROLL_RIGHT hold bypassed the SAF-001 gate entirely.** The hold loop
   calls `_press_key()` / `keyboard_module.release()` directly — the raw
   primitives — not `_execute_key_press`, which is the *only* place the
   manual-takeover check lives (see its own docstring: "Enforced HERE rather
   than at each caller"). Direct callers are exactly the gap that comment
   warns about.
3. **The hold loop's only interrupt was program exit**, by design — the
   original 2026-07-28 bug (`tests/test_disengage_roll.py`'s docstring) was
   the roll reacting to the very `_mission_cancel` it had just set itself and
   holding for 8 ms instead of its intended duration. The fix at the time was
   correct for that bug and simply never considered a takeover arriving
   mid-roll as a *different* kind of interrupt that should still apply.
4. **`start_search_and_destroy_loop()` runs unconditionally** at the top of
   the roll's thread, with no game-state or takeover check — so a roll whose
   thread starts executing after takeover has already begun would still arm
   padlock and weapon fire, even though `_execute_key_press` would suppress
   the individual presses.

Net effect: a disengage roll already in flight when Enter was pressed kept
holding ROLL_RIGHT — an actual flight axis, wingman actively fighting the
operator's own roll input — for up to its full 10 s default duration, and
kept the search-and-destroy padlock/weapon loop threads alive for the same
window. The camera motion reported live is most consistent with the
uncommanded roll itself, not a padlock action slipping past
`_execute_key_press` (which does appear to gate correctly on inspection and
in the surrounding log evidence) — but the roll bypass is a more serious
finding than the report itself: an active flight-control input surviving a
takeover is exactly the failure class SAF-001 exists to close off, function
by function.

## Decision

**D1. A dedicated `_disengage_stop` event, deliberately separate from
`_mission_cancel`.** Reusing `_mission_cancel` would reintroduce the
2026-07-28 bug this method already carries a regression test for — the roll
must outlive the cancel it issues itself, but it must not outlive a manual
takeover. Two different interrupt sources need two different events.

**D2. The hold loop now waits on `_disengage_stop`** (`Event.wait(timeout)`,
the same idiom `_climb_stop` and `_me_stop` already use) instead of a plain
`time.sleep`, and checks it before starting the search-and-destroy loop at
all. `release_for_manual_takeover()` sets it alongside every other tactic's
stop event. `disengage_roll_right()` clears it at the start of each new call,
so a stale set from a previous takeover cannot silently no-op the next
legitimate roll.

**D3. `_press_key`/raw primitives remain in use for the hold itself** — this
ADR does not migrate the roll to `_execute_key_press`. The stop event
achieves the same outcome (the key comes up promptly on takeover) without
restructuring a hold whose exact timing behavior already has a dedicated
regression test guarding it. Worth noting for whoever next touches a
tactic hold: calling the raw primitives directly is precisely how this gap
was created, and `_execute_key_press`'s own docstring already says so.

## Consequences

Manual takeover during an in-progress disengage roll now releases ROLL_RIGHT
and stops search-and-destroy within one 0.1 s poll, matching every other
tactic. The mission-restart logic at the end of `disengage_roll_right` was
already correctly guarded — `_auto_respawn_restart` is cleared by the same
takeover path — so no separate fix was needed there.

This was found by code inspection after the operator's report, not
reproduced from a captured log: the trigger condition (no enemy visible for
30 s) is infrequent enough that none of the recent sessions checked happened
to have a takeover land during one. The fix is validated by two new unit
tests exercising the exact mechanism (stop event set mid-roll; stop event
pre-set and correctly cleared on the next call), not yet by a live
recurrence.

## Validation

- **V1.** A disengage roll in progress releases ROLL_RIGHT within ~0.1 s of
  `_disengage_stop` being set, not at the full configured duration.
- **V2.** A `_disengage_stop` left set from a previous takeover does not
  prevent the next `disengage_roll_right()` call from starting a fresh roll.
- **V3.** `release_for_manual_takeover()`'s source is asserted (by name) to
  set `_disengage_stop`, alongside every other tactic stop — the same
  regression-guard pattern already covering the other events, extended to
  also cover `_boundary_turn_stop` (wired correctly already, but previously
  unchecked by this guard).
- **V4 — live, still open.** Not yet observed: a session where a manual
  takeover lands mid-disengage-roll, confirming the camera/roll settles
  immediately rather than continuing for several more seconds.

## References

- SAF-001 — the requirement this closes a gap in
- ADR 024 3.1b — Disengage as a first-class behavior-tree leaf
- `wingman/controller.py` — `disengage_roll_right`, `release_for_manual_takeover`, `_execute_key_press`
- `tests/test_disengage_roll.py` — the 2026-07-28 regression this ADR's fix had to coexist with, plus V1/V2
- `tests/test_mission_cancel.py::test_release_covers_every_injectable_key` — V3
