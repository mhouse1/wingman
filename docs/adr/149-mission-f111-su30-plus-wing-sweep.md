# ADR 149 — mission_f111: The Su-30 Script Plus the F-111 Wing Sweep

| Status | Date       | Wingman Version |
|--------|------------|-----------------|
| Draft  | 2026-09-25 | 1.8.11          |

## Context

`docs/missions/f111.md` asks for `mission_su30` (ADR 144) flown on the F-111, with one addition: the wing sweep,
bound in-game as `ToggleWingSweep` on `w` (job aid 011). The wings are swept for the climb and unswept when the
aircraft comes back down to the level-off altitude. The spec's six bullets:

1. nose up on battle start or respawn, and as the climb starts press `w` once to sweep the wings
2. boresight engage mode only
3. no weapon switch until the current weapon runs out, then the secondary
4. after reaching 3000 m set the nose angle to -10 degrees
5. when the altitude descends back to 3000 m press `w` once to unsweep; wait at most 30 s, and on timeout unsweep
   anyway
6. activate pursuit mode

The mission levels off at 3000 m, below the tree's 4000 m floor and the armed sustain band. For su30 that
collision restarted the climb 1.3 s after the level-off until ADR 147 gave su30 its own floor. The operator decided on
2026-09-25 to extend the same exception to this mission.

## Decision

**D1. Bullets to building blocks.**

| # | Bullet | Building block |
|---|--------|----------------|
| 1 | nose up, sweep as the climb starts | `climb_mode` (as su30), then **new**: one `Controller.wingsweep()` tap, guarded by a tracked swept state because `w` is a toggle |
| 2 | boresight engage only | `start_boresight_engage_loop` (ADR 144 D2) |
| 3 | no switch until empty | nothing pressed (ADR 144 D4); `pursue_and_engage(defer_switch_until_empty=True)` or the missiles-empty response switches once |
| 4 | 3000 m, then -10 degrees | `_scripted_wait_for_altitude`, `_scripted_stop_climb`, `_scripted_set_nose_angle` (su30's step 3) |
| 5 | unsweep on descent, 30 s bound | **new**: `_f111_unsweep_on_descent`, because nothing waited on a descent to a given altitude |
| 6 | pursuit mode | `_scripted_activate_pursuit` (su30's step 4) |

**D2. The helpers shared with su30.** su30's climb wait, climb stop, nose-angle step and pursuit hand-off were
moved, line for line, into `_scripted_*` helpers that take the log label and the numbers as parameters.
`_su30_wait_for_altitude`, `_su30_stop_climb`, `_su30_set_nose_angle` and `_su30_activate_pursuit` stay and call
them with su30's values, so su30's log lines are unchanged. `mission_su30` and `_run_su30_sequence` were not edited,
and `tests/test_mission_su30.py` passes unchanged.

**D3. The wing-sweep toggle.** `_f111_wings_swept` holds the tracked state. It starts False because the wings are
assumed unswept at spawn (to be confirmed on the first live trial). `stop_eject_sequence` resets it next to
`_eject_weapon_switched`, since that method runs on every respawn and match end. Step 1 does not press when the
state is already True, so a restart in the same life (after the disengage roll, or `u` after a takeover) does not
unsweep the wings. The state changes only after a tap has been sent. A mission cancelled before the tap presses
nothing and records nothing.

The tap is `wingsweep(hold_seconds=0.1)` (`f111_mission.wingsweep_tap_s`), the length `activate_special_weapon` uses for
`q`, rather than the helper's 0.5 s default. A toggle registers once per press, so a longer hold adds nothing. It
goes through `_execute_key_press`, so SAF-001 suppresses it during manual takeover. `w` is already in
`INJECTABLE_KEYS`, is not a watched takeover key, and no other code presses it: this mission is its only writer.

**D4. Bullet 5's wait is inside the mission and bounded.** The wait runs before the pursuit hand-off and delays it by at
most `unsweep_timeout_s` (30 s). No thread outlives the mission (rule 5). Only fresh altitude samples count (rule 7):
`altitude_fresh()`, with a timestamp newer than the sample that was current when the step began. The reading that
ended the climb therefore never counts as the descent. A fresh reading at or below `unsweep_alt_m` (3000 m) unsweeps.
On timeout the step logs a warning and unsweeps anyway, so the chase always starts with the wings in their spawn
position. The step presses no pitch key, so rule 13 is not in play. It does not wait for the floor climb that follows
the crossing (the floor is also 3000 m).

**D5. Cancelled while swept.** If the mission ends between steps 1 and 5 (takeover, End key, missiles empty,
disengage, death), the `finally` presses nothing and logs `mission ended with the wings left swept`. After a takeover
the key would be suppressed anyway, and a death resets the wings.

**D6. ADR 147 extended to f111 (operator decision, 2026-09-25).** The su30-only check `_su30_flies_own_altitude`
became `_own_altitude_floor_in_play_m`, which looks up the last launched mission in `_own_altitude_floors_m`, a
map built in `__init__` from `su30_mission.alt_floor_m` and `f111_mission.alt_floor_m`. A mission is in the map only
when its block sets the key. `altitude_floor_override_m()` and `sustain_climb_suppressed()` have the same signatures
and semantics, and `BehaviorTreeHandler`'s wiring is unchanged. The lock is still taken with a 1 s timeout, and the
timeout warning still contains `last-mission lock timeout`. su30's behavior and ADR 147's tests are unchanged. The
config relation ADR 147 pins (floor at or below the level-off, below the tree's floor) is pinned for the new block by
`test_f111_floor_is_a_lowering_and_sits_at_or_below_its_own_level_off`.

**D7. Config.** `f111_mission` is its own block (`climb_alt_m`, `alt_floor_m`, `climb_max_s`, `nose_angle_deg`,
`angle_tolerance_deg`, `angle_confirm_reads`, `angle_pulse_s`, `angle_max_s`, `tick_s`, `unsweep_alt_m`,
`unsweep_timeout_s`, `wingsweep_tap_s`), with a schema `Section`, so retuning one jet does not retune the other. The
angle-step numbers are su30's, copied with its comment. A test pins them equal to su30's as shipped. The scripted
climb's fuel reserve is read from `behavior_tree.climb.fuel_reserve_pct`, as su30 does. `lock_timeout_s` is not
carried over because only a preempting hotkey uses it.

## Defaults applied (docs/missions/README.md section 2.2)

| # | Default | Applied as |
|---|---------|------------|
| 1 | Launch | no hotkey; `f111` added to `mission.default_mission` choices, `_start_default_mission` mapping and a `restart_last_mission` branch |
| 2 | Hotkey while running | automatic launches skip when the lock is held (`acquire(blocking=False)`), like `mission_jas39` |
| 3 | Shipped default | unchanged (`su30`); `mission.default_mission: f111` selects this mission |
| 4 | End | the last bullet is a hand-off: the mission ends at step 6 when pursuit takes the aircraft |
| 5 | Waits | every wait is bounded: climb by the climb's own cap, nose angle by `angle_max_s`, unsweep by `unsweep_timeout_s`; a missing tracker or FSM refusal holds until cancelled (rule 13) |
| 6 | Logging | one `step N/6` line per bullet |
| 7 | Config | `f111_mission` block with a schema entry and a comment per number |
| 8 | Shared behavior | ADR 147's exception extended to f111 on the operator's decision (D6); the padlock block was **not** extended (below) |
| 9 | Acceptance | live-trial log lines below |

## Deviations and open points

- **The padlock block is still su30 only.** In ADR 144, "boresight engage only" meant two things for su30: never
  starting search-and-destroy, and turning off every shared path that presses the padlock camera
  (`is_padlock_blocked()`: the `padlock_camera()` backstop, the ADR 140 D6 unknown-state correction and the
  target-spread handler). mission_f111 does the first. Doing the second would mean extending
  `is_padlock_blocked()` to `f111`, a shared-behavior change that has not been authorised. So on an F-111 life the
  ADR 140 correction can still press `p` once the secondary is selected, and the spread handler can still press it
  after two missiles fire. Options for the operator: (a) extend `is_padlock_blocked()` to f111 the way D6 extends
  the floor; (b) leave it as it is. Nothing was changed.
- **Wings unswept at spawn** is an assumption (spec note). If the first live trial shows the F-111 spawning swept,
  the initial state and the reset in `stop_eject_sequence` must change.
- `make sr`'s su30 hand-off and nose-angle counts match on `mission_su30` log text and do not count f111.

## Live-trial acceptance (log lines to check)

Each is emitted by the code and asserted in `tests/test_mission_f111.py`. For each life, in order:

1. `Controller: mission_f111 - step 1/6: nose up, climbing to 3000 m`
2. `Controller: mission_f111 - step 1/6: sweeping the wings (w, 0.1s tap)`, or on a same-life restart
   `step 1/6: wings already swept this life, not pressing w`
3. `Controller: mission_f111 - step 2/6: boresight engage on (weapon-fire loop, no padlock)`
4. `Controller: mission_f111 - step 3/6: keeping the selected weapon, switching to the secondary only when it runs out`
5. `Controller: mission_f111 - altitude NNNN m at or above 3000 m`, then
   `step 4/6: 3000 m reached, setting nose angle to -10 deg`
6. `Controller: mission_f111 - step 5/6: waiting up to 30s for the altitude to descend to 3000 m`
7. `Controller: mission_f111 - step 5/6: altitude NNNN m at or below 3000 m` followed by
   `step 5/6: unsweeping the wings (w, 0.1s tap)`; or on timeout the warning
   `step 5/6: no fresh altitude at or below 3000 m within 30s, unsweeping anyway` followed by the same unsweep line
8. `Controller: mission_f111 - step 6/6: activating pursuit mode`
9. On a cancel between steps 1 and 5: `mission_f111 - mission ended with the wings left swept (no unsweep press on exit)`

Also check:

- exactly two wing-sweep lines per life, sweep before unsweep;
- no `CLIMB - holding nose up + afterburner (target alt 5000 ...)` after the level-off (the sustain band stands aside);
- every `BT: ALTITUDE FLOOR — ...m below 3000m` warning while f111 flies cites 3000 m, not 4000 m;
- on the HUD, the wings unswept at spawn (confirms D3's assumption).

## Consequences

- The F-111 can fly the su30 script without a hotkey. `mission.default_mission: f111` selects it.
- su30's scripted steps now run through shared `_scripted_*` helpers. A fix to one of them applies to both missions.
- ADR 147's exception now covers two missions. It is still keyed on mission names in code, not on a setting a mission
  can choose (docs/missions/README.md section 3).

## Related

- ADR 144 (mission_su30), ADR 145 (launch through `default_mission`), ADR 147 (su30's altitude doctrine, extended
  here), ADR 141 (the 4000 m floor), ADR 132 (turn guard), HLDD 015 (pursuit mode)
- `docs/missions/f111.md`, `docs/missions/README.md`
