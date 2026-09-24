# ADR 144 — mission_su30: The Scripted Su-30 Sequence

| Status | Date       | Wingman Version |
|--------|------------|-----------------|
| Draft  | 2026-09-24 | 1.8.11          |

## Context

The operator supplied a four-step mission for the Su-30 in
`docs/missions/su30.md`:

1. nose up on battle starting or respawn
2. switch to the secondary weapon
3. after reaching 3000 altitude, set the nose angle to -10 degrees
4. activate pursuit mode

**Revised 2026-09-24 (operator): step 2 is not an action at the start of the
life.** The operator asked that the mission "not switch weapons until it runs
out". The spawn weapon stays selected and fires; the switch to the secondary
happens only once that weapon is empty (D4). `docs/missions/su30.md` carries the
same wording.

`mission_j20` is the structural reference (lock, cancel, runner thread, wait
loop, single entry point for battle entry and respawn), but its *doctrine* is
the opposite of this sequence. ADR 075 made J20 fully adaptive: the mission
thread only runs the search-and-destroy loops and the behavior tree owns every
in-battle decision. The Su-30 sequence is a script with a fixed order, so it
borrows J20's shape and none of its content.

## Decision

**D1. A scripted mission, built from existing actuators.** `mission_su30`
(`controller.py`) runs the four steps once per life and reuses what already
exists rather than adding new flight code: `climb_mode` for step 1,
`switch_weapon` for step 2 (deferred until the weapon is empty, D4),
`pursue_and_engage` (HLDD 015) for step 4. Only
step 3 is new, because nothing in the codebase flew to a *commanded* flight-path
angle.

**D2. Boresight engagement only — never search-and-destroy.** J20 starts
`start_search_and_destroy_loop`, whose padlock loop toggles the padlock camera
every 6 s; this mission does not want the camera touched. Boresight engagement
is the same weapon-fire loop *without* the padlock loop, so the nose is the aim
point.

It is implemented as its own pair, `start_boresight_engage_loop` /
`stop_boresight_engage_loop`, and `search_and_destroy` is not modified. The two
share nothing: separate stop event, thread and lifecycle lock, no call from one
into the other, and no "mode" flag threaded through the existing loop. Either
can be started or stopped in either order without affecting the other, which is
what lets a mission choose its engagement mode (`mission_j20` keeps
search-and-destroy, `mission_su30` uses this). It reuses the same cadence knob
(`mission.weapon_loop_interval`) and the ADR 118 start/stop serialisation, and
ends on `_mission_cancel` like the loops it is modelled on.

Not carried over: search-and-destroy's `target_painting_mode` fire suppression.
That is J20's painting doctrine (hold back the last primary missile); this mode
fires the loadout it is given.

It is a writer on the fire key, so it is stopped wherever the other writers are:
manual takeover (`release_for_manual_takeover`, SAF-001) and `cleanup()`.

**Nothing else may press the padlock camera either — a shared change.** Not
starting `search_and_destroy` was not enough: two shared paths press the padlock
key on their own, and this mission triggers both.

- ADR 140 D6 (`_maybe_correct_padlock_unknown`) presses to "correct" an Unknown
  padlock state whenever `is_secondary_weapon_active()` is true. Step 2 used to
  leave it true for the whole life, so it pressed up to three times per life.
  Since the 2026-09-24 revision it is true only from the moment the spawn weapon
  runs out and is switched away from; the block below applies from then on and
  is unchanged.
- The target-spread handler (`AmmoEventsHandler.tick_missile_count`) presses
  padlock twice after two missiles "fire". Switching from a 4-missile primary
  rack to a 2-missile secondary reads as two fired, and so does firing the
  heat-seekers.

The rule is one check, `Controller.is_padlock_blocked()`, true only while the
last-launched mission is `su30`. It is read in three places: `padlock_camera()`
(the choke point every programmatic press goes through, so the backstop, which
also covers a search-and-destroy loop that `disengage_roll_right` may start), the
ADR 140 correction, and the spread handler. Every other mission is unaffected
because the check is false for them; tests pin both directions.

The mission starts the boresight loop at step 2, where it fires whichever weapon
is selected — the spawn weapon, since nothing switches it any more (before the
2026-09-24 revision it started right after a step-2 switch so that it could only
fire the secondary) — and stops it at the pursuit hand-off, where
`pursue_and_engage` fires for itself every cycle and a second writer would only
double the cadence. Every mission exit path stops it, so a fire loop cannot
outlive its mission into the next life. If pursuit cannot start (D6) the loop
keeps running while the mission holds, as J20's search-and-destroy does.

**D3. One entry point for battle entry and respawn.** Exactly as for J20,
battle entry and every respawn restart come through `mission_su30`, so step 1's
"on battle starting or respawn" needs no respawn wiring. Three things route to
it:

- the `o` hotkey (`MISSION_SU30_KEY`), which forces `GAME_BATTLE` like `u` does
  and runs with `preempt=True` — a running mission is cancelled and taken over,
  not ignored (the ADR 111 lesson: a silent no-op on a keypress is the worst
  failure a hotkey can have);
- `restart_last_mission`, which gained an `"su30"` branch, so a respawn resumes
  whichever mission ran last;
- `mission.default_mission` (`j20` or `su30`), read by both GAME_STARTING launch
  paths and the no-prior-mission fallback through one helper,
  `_start_default_mission`. It shipped as `j20` so that nothing changed for an
  existing setup, and the operator set it to `su30` on 2026-09-24 after a run
  showed battle entry still launching J20 (and its search-and-destroy padlock
  loop). The code-level fallback, used when a config names no mission, stays
  `j20`. A test pins the shipped value.

Automatic launches use `preempt=False` and skip when a mission holds the lock,
as J20 does.

**D4. The weapon switch is deferred until the weapon runs out, and happens at
most once per life.** `SWITCH_WEAPON` is a toggle (ADR 136, and
`eject_and_dive`'s `weapon_already_switched` note recording a live double-press
that left the secondary never selected). *Revised 2026-09-24 (operator: "not
switch weapons until it runs out").* Step 2 presses nothing and leaves
`_eject_weapon_switched` — the flag `is_secondary_weapon_active()` reads and
`stop_eject_sequence()` clears on respawn — exactly as it found it. The flag is
the only record of whether the key was pressed this life, so it becomes true only
when the switch really happens, and a hotkey re-press or a resume from manual
takeover cannot toggle back to primary.

The switch itself happens in whichever path is running when the spawn weapon
empties:

- **Steps 1-3, mission still holding the lock:** the existing missiles-empty
  response (`AmmoEventsHandler.handle_no_missiles`, gated on
  `is_mission_running()` and `GAME_BATTLE`) calls `pursue_and_engage` in its
  default form, which switches at its start and pursues. Unchanged code; it was
  simply never reached for this mission while step 2 had already switched.
- **Step 4 onward:** the mission has ended, so `handle_no_missiles` no longer
  applies and pursuit's own loop must do it. `pursue_and_engage` gained
  `defer_switch_until_empty`: it presses nothing at its start, leaves the flag
  alone, and presses `SWITCH_WEAPON` once when the selected weapon's ammo has read
  0 for `pursuit_mode.empty_confirm_reads` (3) consecutive cycles. One misread 0
  would swap away a rack that still has missiles, and the only undo is a second
  press, so an unreadable count (`None`) leaves the run as it is and any positive
  read resets it. After the switch the existing ammo-zero fall-through applies,
  with its `ammo_zero_grace_s` measured from the switch, since the HUD count lags
  a switch by seconds.

*Update 2026-09-24 (operator): the cap is off.* `pursuit_max_duration_s` now ships as 0 (no cap), so
this fall-through no longer happens on a timer; the pursuit continues until the ammo is exhausted or a
respawn, takeover or shutdown stops it. The text below describes the fall-through that still applies if a
positive cap is set, and the dated live-trial paragraphs record the behavior while the cap was 20 s.

If `pursuit_max_duration_s` ends the encounter before the weapon ran out, the
fall-through calls `eject_and_dive(defer_switch_until_empty=True)`. The dive
presses no `SWITCH_WEAPON` at its start and leaves the flag alone; its heatdive
loop fires the selected weapon and presses the key once, when that weapon has read
0 for `empty_confirm_reads` consecutive cycles (the same confirmation rule as
pursuit), then fires regardless for `ammo_zero_grace_s` while the HUD count
catches up. *Revised 2026-09-24 (operator):* the first version of this paragraph
let the dive make its own switch at the cap, calling it an existing
`eject_and_dive` rule. That switched away from a loaded rack on every capped
pursuit (06:57:29, 07:02:45, 07:05:17, 08:16:24 and 08:19:57), and the last one
was caught by the operator's `v` screenshot at 08:20:14: the 2/2 secondary
selected in the dive, the 6/6 primary untouched. The dive itself is unchanged
otherwise — it still dives. The two ADR 088 rearm-abort checks, whose premise is
an EMPTY rack, stand down while the switch is deferred (`_eject_defer_switch`);
without that a loaded rack would read as "rearmed" and abort the dive, which
would change what the dive does rather than when the weapon switches. Whether a
capped pursuit should dive at all with missiles left is a separate question this
ADR does not answer.

`weapon_already_switched` (added earlier for this mission's step 2) stays on
`pursue_and_engage` with its old meaning; nothing in this mission passes it now.
The defaults of both parameters leave the missiles-empty path byte-identical.

**D5. Step 3 is a bounded, new-sample, pulsed correction.** After `climb_alt_m`
the mission stops whichever climb hold is running and pulses NOSE_DOWN or
NOSE_UP toward `nose_angle_deg`:

- the angle is the **flight-path angle** (altitude rate over speed,
  `TelemetrySnapshot.pitch_angle_deg`), not nose attitude — the same caveat
  ADR 067 records — and it refreshes about every 3 s, so the step acts on **new
  samples only** and pulses rather than holds (ADR 068/069: a held key overshoots
  on a lagging reading);
- it never commands on stale telemetry (ADR 038);
- it presses nothing while any climb hold is running. This is the one-pitch-
  writer rule the spawn guard already follows, and it is what lets ADR 141's
  altitude-floor climb outrank the script (see Consequences);
- it is bounded by `angle_max_s`. On timeout the mission logs a warning and
  proceeds to pursuit, whose own pitch loop corrects the rest; on cancel it
  stops.

**D6. Pursuit goes through the eject FSM seam, and never falls back to a dive.**
Step 4 triggers `eject_started` before `pursue_and_engage` and `eject_complete`
in its completion callback, exactly as `AmmoEventsHandler.fire_eject` does.
Pursuit is a behavior *inside* `GAME_BATTLE_EJECT` (HLDD 015 D1): that state is
what makes the tree yield the airframe and what routes a respawn to
`stop_eject_sequence`. Starting pursuit from `GAME_BATTLE` would leave the tree
steering roll against the tracker.

`pursue_and_engage` falls back to `eject_and_dive` when no `TargetTracker` is
wired. That is right for an empty rack and wrong here, so the mission checks
first: with no tracker, or if the FSM refuses `eject_started`, it logs an error
and holds until cancelled. An armed aircraft is not dived because of a wiring
fault. (`main.py` always wires a tracker, so this is a guard, not a live path.)

**D7. The mission ends at the hand-off.** `pursue_and_engage` cancels the
mission to own both tracking axes, so `mission_su30` ends at step 4 rather than
running until cancelled, and releases the lock so the next respawn's restart is
not refused. Pursuit's own end (secondary ammo exhausted, or a positive
`pursuit_max_duration_s`; the shipped value has been 0, no cap, since 2026-09-24)
falls through to `eject_and_dive` (HLDD 015 D3); the respawn after that restarts
this mission. With no cap a life is climb → pursue (until ammo, respawn or
shutdown) → dive only if the ammo ran out → respawn. An abandoned script (cancel, exit, no tracker)
stops any climb it left running, as `mission_loiter` does.

## Consequences

**The specified altitude is below ADR 141's altitude floor.** `climb_alt_m` is
3000 (revised from 4000 by the operator on 2026-09-24) and
`behavior_tree.climb.alt_floor_m` is 4000. Below the floor the tree's Climb
emergency is selected on every tick whether or not a mission is running, and its
leaf restarts a climb hold whenever none is running (`ConditionTactic.update`).
So the climb this mission stops at 3000 m is restarted by the tree within a tick
and runs on toward the tree's own exit altitude, and nothing in the mission keeps
the nose at -10 degrees while that happens:

- the nose-angle step stands aside for any climb hold (D5), so it cannot take the
  pitch axis until the aircraft is above the floor, and after `angle_max_s` it
  gives up and hands over to pursuit;
- once above the floor, a -10 degree flight path sinks back through it, and the
  floor climb takes the axis again;
- `make_idle_condition` deliberately lets the floor climb start inside
  `GAME_BATTLE_EJECT`, so it starts during pursuit too, and `pursue_and_engage`
  has no yield of its own — below 4000 m the floor climb and pursuit's pitch loop
  both write NOSE_UP/NOSE_DOWN. That two-writer case is not new (any pursuit
  below the floor has it), but this altitude makes it the normal one.

As shipped, therefore, 3000 m is where the script stops asking for altitude, not
a level-off altitude, and **the -10 degree nose angle is not held.** Two ways
out, both the operator's call: raise `su30_mission.climb_alt_m` to the floor or
above so the descent has room, or lower `alt_floor_m` deliberately. This ADR does
neither — the floor is ADR 141 D1's unconditional backstop for near-stall cases,
reintroduced after a revert.

**Pursuit is reached without the HLDD 015 hard gate.** Step 4 calls
`pursue_and_engage` directly, so it is not gated by `pursuit_mode.enabled`
(which governs only the missiles-empty trigger). HLDD 015's precondition —
Design 005 Two-Axis Rollout at Phase 2 — is still unmet, and the flag was
already flipped on by explicit operator instruction on 2026-09-23. Selecting
this mission is the same kind of explicit instruction, and the first live
pitch actuation from tracking will happen inside it.

**The behavior tree keeps ticking during steps 1-3.** The mission holds the
lock, so `mission_running` is true and the tree's roll steering (Engage,
Regroup), missile evade and boundary turn stay live. That is intended: they
command roll, evade or heading, not the pitch axis the script uses. Only the
Climb tactic is a pitch writer, and D5 covers it.

**`o` is unverified against the game's own bindings.** The hotkey uses
`suppress=False`, like `u` and `y`, so the game also receives the key. If
MetalStorm binds `o`, pressing it flies that action too. `test_keybindings`
guards collisions with the keys wingman injects, not with the game's.

**Boresight engagement fires blind, on a timer, from the start of the life.**
Like search-and-destroy's weapon loop it presses the fire key on
`weapon_loop_interval` (1.0 s) with no target or lock check, so the spawn weapon
(before the 2026-09-24 revision, the secondary) is exposed to that cadence for the
whole climb to `climb_alt_m`, not only once pursuit has a target. Pursuit itself
fires on every cycle regardless of tracker visibility (HLDD 015 D2), so this
matches the codebase's existing model. Whether the game launches a missile with no
lock is not established for the spawn weapon. It was observed not to for the
secondary: in the 2026-09-24 06:01-06:08 run every readable `BT[active]` reading
between mission start (06:02:41) and 06:03:29 was `missiles=4` (31 readings, 2
unreadable) across 61 fire-key presses, and the HUD read 4/4 on both racks at
06:03:35-40. If the spawn weapon does launch
blind, its rack empties during the climb, the missiles-empty response above
switches and pursues early, and the script's remaining steps are abandoned. Moving
the `start_boresight_engage_loop()` call to just before the hand-off, or gating it
on tracker visibility, are the two ways to change this.

**D4 live trial (2026-09-24 06:51-07:06, one 15 m 27 s session, 7 mission
starts).** Measured from that log:

- *Step 2 pressed nothing.* 5 starts logged "keeping the selected weapon" and 2
  logged "already selected this life, nothing to do" (a restart after `HEALTH
  ALIVE` and a resume from manual takeover, each in a life where the flag was
  already set); 0 logged the old "switching to the secondary weapon". No
  `switch_weapon` press occurred at any mission start.
- *The only three presses were `eject_and_dive`'s own,* at the 20 s pursuit cap
  (06:57:29, 07:02:45, 07:05:17), with the spawn weapon still loaded. This was
  written up here as a boundary and left to the operator; the operator then
  objected (`v` screenshot 08:20:14, two more such presses at 08:16:24 and
  08:19:57 in the 07:34-08:24 session) and D4 now defers the dive's switch too.
  That fix was then run live (2026-09-24 08:41-08:56): 6 capped pursuits, 6 `PURSUIT
  CAP ... switch deferred until it is empty` lines and 0 `switch_weapon` presses in the
  whole run; the ammo reading stayed on the 6-rack (one launch, 6 to 5) instead of
  falling to the 2-rack within 0.4 s of a press. Confirmed.
- *The spawn weapon did not launch with no lock.* Its `BT[active]` reading never
  decreased within a life, in this session or in the 07:34-08:24 one. Which rack
  is selected at spawn differs by session (this session's lives read 2 before the
  cap's switch; the 08:18-08:19 life read 6 through the whole climb and pursuit and
  2 within 0.4 s of the cap's `g`), so the reading follows the operator's loadout
  order and the selected rack. This answers the open question above.
- *The deferred switch itself was not exercised:* 0 "selected weapon empty"
  lines, because the spawn weapon never emptied. Covered by unit tests only.
  **Update 2026-09-24 11:10: exercised live, in the next session (the 10:51
  run).** One complete life on the 6-rack: the reading went 6 (11:05:53), then
  5, 4, 3, 2 in pursuit (11:06:50 to 11:06:55); the pursuit cap at 11:07:01
  pressed nothing (`PURSUIT CAP ... switch deferred until it is empty`); the
  dive kept the same rack and fired 2 to 1 (11:07:20) and 1 to 0 (11:07:22.03);
  `selected weapon empty (3 consecutive zero reads)` followed at 11:07:22.47
  with one `switch_weapon` press, and the reading was 2 (the secondary) at
  11:07:23.5. All six primary missiles were used before the only switch of the
  life, which is the operator's requirement. A second life repeated it (cap at
  11:17:36 with no press; the dive read 5, 4, 3, 2, 1, 0; one press at
  11:18:02.86). By 11:18 the run had 11 pursuit caps and 2 presses, both the
  empty-rack press.
- *Pursuit never had a target in view* in any of the 5 pursuits (0 locks), and 2
  ended in death with incoming-missile warnings before them. Neither bears on D4.

**Disengage can still cancel the script.** The tree's Disengage tactic (30 s
without a ring contact, above Climb in priority) calls `disengage_roll_right`,
which cancels the running mission, rolls right for 10 s and restarts the last
mission. A climb that takes longer than that with no contact on the minimap
restarts the script mid-climb. The padlock is not reached that way — the loops
that call starts see `_mission_cancel` already set and exit, and `padlock_camera`
is gated regardless — but the script is disrupted. Not addressed here; suppressing Disengage while this mission runs
(as ADR 110 does for the survival hold) is the obvious change if it shows up.

**J20 is untouched by default.** `mission_j20`'s body, its hotkey and its
turn-guard arming are unchanged, and so is `search_and_destroy`. The three
hard-coded J20 launches were replaced by `_start_default_mission`, which resolves
to whatever `mission.default_mission` names. That is `su30` as shipped, so battle
entry no longer launches J20 unless the config or the `u` hotkey asks for it.

## Open Questions

1. `angle_pulse_s` (0.6 s), `angle_tolerance_deg` (4), `angle_confirm_reads` (2)
   and `angle_max_s` (20 s) are named guesses, the same status
   `pursuit_max_duration_s` carries. A nose 40-50 degrees high at the end of the
   climb may need more pulses than 20 s of 3 s samples allows; the climb's own
   exit push (ADR 086) does part of that work first.
2. Should `climb_alt_m` sit at or above the floor (or the floor come down)? See
   Consequences: at 3000 m the nose angle is not held.
3. Should the fall-through to `eject_and_dive` apply to this mission at all, or
   should pursuit end by returning to normal flight? HLDD 015 Open Question 1
   made the same choice for the missiles-empty case; nothing here revisits it.

## Validation Strategy

Unit tests (`tests/test_mission_su30.py`) pin the step order, the toggle
discipline, the stale-telemetry and one-pitch-writer rules, the failure paths,
the lock handling and the wiring. They are mutation-checked: removing each of
those guards fails a named test.

A live trial is still required before this leaves Draft. What to read in
`wingman.log`, in order: `mission_su30 - step 1/4`, `step 2/4`, `altitude ... at
or above`, `step 3/4` and the `nose angle ... pulsing` lines (does the angle
converge, or does a climb hold take the axis), then `step 4/4` and
`pursue_and_engage — tracking engaged`. The first thing to look for is the
ADR 141 floor climb starting within seconds of step 3.

## Related Documents

- `docs/missions/su30.md` — the operator's sequence.
- `docs/adr/075-afterburner-fuel-perception-and-fully-adaptive-j20-mission.md`
  — the adaptive J20 doctrine this mission deliberately is not.
- `docs/hldd/015-target-tracking-pursuit-mode-hldd.md` — pursuit mode, its
  FSM seam (D1), termination (D3) and hard gate.
- `docs/adr/136-heatseeker-dive-invokable-mode.md` — the secondary loadout and
  the `SWITCH_WEAPON` toggle.
- `docs/adr/141-phase1-altitude-floor-stall-prevention-and-emergency-yields.md`
  — the 4000 m floor that sits above this sequence's 3000 m level-off altitude.
- `docs/adr/111-the-hold-pre-empts-the-running-mission.md` — why the hotkey
  takes the aircraft instead of queuing.
- `docs/adr/132-fly-the-spawn-heading-first.md` — the turn guard, armed here as
  in `mission_j20`.
