# Missions — Authoring Contract

| Status | Date       | Wingman Version |
|--------|------------|-----------------|
| Draft  | 2026-09-25 | 1.8.11          |

This file is the part that does not change from mission to mission: what a
mission inherits, the building blocks it is made from, the rules it must
follow, and the files it touches. A mission's own file holds only its spec.

| File | Role |
|------|------|
| `README.md` | this contract; read by Claude, not copied |
| `_template.md` | the blank spec form; **copy this** |
| `j20.md` | the form filled in for `mission_j20`, plus how J20 works today |
| `su30.md` | the operator's own spec, and the layout `_template.md` follows; a scripted hand-off mission |
| `jas39.md` | a spec written against this contract; the closest worked example of the current wiring (ADR 145) |
| `f111.md` | `su30.md` plus a wing sweep on `w`; implemented as `mission_f111` (ADR 149) |

The behavior described here was checked against the code and
`wingman/config.yaml`, not copied from ADRs. Where an ADR and the code
disagree, the code wins (see section 8).

## 1. Making a new mission

1. Copy `_template.md` to `docs/missions/<name>.md` and write the sequence.
2. Read section 2.1 while you write it.
3. Send Claude the prompt below.

```text
Implement the mission in docs/missions/<name>.md, following
docs/missions/README.md, including the defaults in section 2.2.

Before writing code, put a table in your reply: for each bullet in "sequence",
the building block that implements it, or "new" with a one-line reason. Flag any
collision with the altitude floor or the armed sustain climb (README section
3.2), and any second writer on the same key.
If a bullet cannot be mapped, state your assumption in the reply and proceed,
except that section 5 rule 13 (never dive on a fault) overrides any assumption.

Do not change existing missions or the engagement loops they use. If a bullet
needs a change to shared behavior (README section 3), stop and report the
options instead of editing it.
Meet the definition of done in section 7 and leave the changes uncommitted.
```

## 2. Writing the sequence

The spec is the `## name` and `## sequence` of the template, in the layout of
`su30.md`. Nothing else is required.

### 2.1 What goes in a bullet

Bullets run in order. Put numbers and conditions in the bullet ("after reaching
5000 altitude set nose angle to -10 degrees"); Claude derives the config keys.

* **Engagement is a bullet.** Name `search_and_destroy` (padlock plus fire) or `boresight_engage` (fire only), and put it where it should start. Position matters: the loop fires whatever weapon is selected when it starts, so a loop started before a weapon switch fires the spawn weapon until the switch. If the order matters to you, say why in the bullet.
* **Altitude and angle targets respect the floor.** Below 4000 m the tree's Climb tactic takes the pitch axis: the hard floor (`behavior_tree.climb.alt_floor_m`) always, and the sustain band (4000 to 5000 m) while the aircraft is armed and a mission runs. A scripted flight path below 4000 m is overridden by a climb, not held. Only `mission_su30` (ADR 147) and `mission_f111` (ADR 149) have their own floor; a new mission that needs one is a shared-behavior change (default 8).
* **A mission that returns early turns off the lock-gated behavior** in section 3.1. A hand-off mission should say what takes over.
* **A bullet that asks for something shared behavior already does** (climbing, cruise afterburner, evading) is a change to every mission. See default 8.

### 2.2 When the spec is silent

Claude applies these and records them in the ADR.

1. **Launch.** No hotkey of its own (ADR 145). The mission is added to the `mission.default_mission` choices; `u`, battle entry and the restart fallback all launch whichever mission that key names. A dedicated hotkey is added only when the spec asks for one. Claude then picks an unused key and flags it for you to check against the game's bindings: `test_keybindings` checks only against keys wingman itself injects, and hotkeys are grabbed with `suppress=False`, so the game also receives them.
2. **Hotkey while a mission runs.** `u` skips: it logs and does nothing while a mission holds the lock. A dedicated hotkey preempts: it cancels the running mission and takes over (ADR 111), as `o` does for `mission_su30`.
3. **Shipped default.** The shipped `mission.default_mission` value is not changed unless the spec says so. Selecting the new mission is a one-line config edit for you.
4. **End.** The mission runs until cancelled, unless the last bullet is a hand-off.
5. **Waits.** Every wait is bounded. On timeout, log a warning and continue with the next bullet, unless continuing would dive an armed aircraft (rule 13 in section 5), in which case hold until cancelled.
6. **Logging.** One `step N/M` line per bullet, so a live trial is readable.
7. **Config.** Numbers from the bullets go in a `<name>_mission` block in `config.yaml` with a schema entry and a comment giving the reason for each. Nothing is hard-coded without saying so.
8. **Shared behavior.** If a bullet needs a change to shared behavior (section 3), stop and report the options: a global change that affects every mission, or a per-mission exception. The only per-mission exception today is ADR 147's, for `mission_su30` and (ADR 149) `mission_f111`, each named in code; extending it to another mission is a shared-code change. The choice is the operator's each time. Never edit shared code silently.
9. **Acceptance.** Claude reports the log lines to check on a live trial in its reply and in the ADR, as ADR 144 did. The spec carries no acceptance section.

## 3. What every mission inherits

None of this belongs to a mission. It is shared tree and watchdog code driven
by global config, read once and applied to whichever mission is flying. The one
exception is ADR 147 (extended to `mission_f111` by ADR 149): while `mission_su30`
or `mission_f111` is the mission in play, the tree uses that mission's
`<name>_mission.alt_floor_m` as its floor and the sustain band stands aside
(`Controller.altitude_floor_override_m`, `sustain_climb_suppressed`). That
exception names su30 and f111 in code, so it is not a switch a new mission can set. A
spec that wants a different value or an opt-out is asking for a shared change
(default 8 in section 2.2).

### 3.1 Behavior gated on the mission lock

These read `is_mission_running()` (the snapshot's `mission_running`), so
holding the lock is what switches them on:

* sustain climb (`make_sustain_climb_condition` in `behavior_tree.py`)
* BoundaryTurn entry (ADR 138)
* the tree's Engage, Regroup, Climb-steering and seek-center actuation (`_may_fly` in `BehaviorTreeHandler.tick`)
* cruise afterburner (`note_afterburner_cruise`; it also runs during an eject)
* the missiles-empty verdict (`AmmoEventsHandler.handle_no_missiles` returns when no mission is running)

### 3.2 Tactic selector

Top wins. Source of truth is `_PRIORITY_ORDER` in `wingman/behavior_tree.py`.
Values are shipped defaults; `config.yaml` is authoritative.

| # | Tactic | Selected when | Does |
|---|--------|---------------|------|
| 1 | Idle | FSM is not `GAME_BATTLE` (yields only to a climb emergency during `GAME_BATTLE_EJECT`) | nothing |
| 2 | RespawnWait | respawn screen is up | nothing |
| 3 | Eject | missiles empty, debounced (`mission.no_missiles_consecutive_required`), after a grace (`mission.no_missiles_abort_grace_s`) | `fire_eject`: `pursue_and_engage` when `pursuit_mode.enabled`, else `eject_and_dive` |
| 4 | MissileEvade | incoming detected, sticky while the hold runs | `missile_evade_mode()`; yields to a climb emergency |
| 5 | BoundaryTurn | within `behavior_tree.boundary.turn_frac` of the map edge, mission running | `boundary_turn_mode()`; yields to a hard climb emergency; blocked by the turn guard |
| 6 | Evade | health below a threshold | never selected, the threshold is unset |
| 7 | Disengage | no enemy for `behavior_tree.disengage_after_s` | `disengage_roll_right()`, then restart |
| 8 | Climb | emergency band, or sustain band while armed | `climb_mode()` |
| 9 | Engage | any enemy in the minimap rings | steer with `orient_nose_to_target`, or orbit; tuned by `j20_mission.*` (see section 8) |
| 10 | Regroup | friendlies visible, no enemy (`minimap.regroup_enabled`) | steer toward the friendly centroid |
| 11 | AttackSupport | always | seek-center, shadow only (`seek_center_enabled: false`) |

Climb has two bands. The emergency band covers low altitude, predicted time to
ground, and the altitude floor. The sustain band climbs an armed aircraft
(missiles above zero, mission running) below `climb.sustain.enter_below_alt`
until `exit_above_alt`. The floor and the sustain entry are both 4000 and a test
(`test_mission_j20_altitude_doctrine_is_4000m_everywhere`) pins them equal.
Under ADR 147, `mission_su30` flies with a 3000 m floor and no sustain band, and so, under ADR 149, does `mission_f111`.

### 3.3 Tree-independent, every tick

| What | Where | Rule |
|------|-------|------|
| Cruise afterburner | `note_afterburner_cruise` | while a mission runs or an eject is in progress, hold from `rearm_fuel_pct` down to `min_fuel_pct`, then let it recharge (`behavior_tree.afterburner_cruise`) |
| Stall prevention | `note_stall_prevention` | below `min_speed_kph` release the airbrake and press the afterburner (`behavior_tree.stall_prevention`) |
| Missile afterburner | `note_incoming` | hold while incoming, until clear for `afterburner_clear_s` (ADR 128) |
| Flares | `AmmoEventsHandler.deploy_flares_on_new_incoming` | burst on each new incoming alert; reload with the special ability at 2 flares |
| Spawn guard | `Controller.start_spawn_guard` | from the death latch, pulse nose up until telemetry is fresh, capped (`behavior_tree.climb.spawn_guard`) |
| Respawn restart | `RespawnHandler.handle_alive_transition` | after the respawn screen clears and health returns, `restart_last_mission()`; skipped if missiles read 0 |

### 3.4 Ways a life ends

| Cause | Who cancels | Restarts? |
|-------|-------------|-----------|
| Death | `RespawnHandler` on respawn detection | Yes, on health alive |
| Missiles empty | `pursue_and_engage` or `eject_and_dive` call `cancel_mission()` | Yes, after the respawn that follows |
| No enemy for the disengage window | `disengage_roll_right` cancels, rolls, waits up to 5 s for teardown | Yes, immediately after the roll |
| Manual takeover (`i j k l`, arrows, Enter) | `_handle_maneuver_key_press`, `release_for_manual_takeover` | No; `u` resumes the configured mission |
| End key | `cancel_mission_hotkey` | No; auto restart is switched off |
| Click to continue, lobby entry | `main.py` | No; the match is over |
| Program exit | `_mission_exit_requested()` | No |

A restart runs whichever mission was launched last (`_set_last_mission`).

## 4. Building blocks

A mission is assembled from these. Do not add new flight code when one fits;
`mission_su30` and `mission_jas39` each needed exactly one new routine.

**Flight and weapon keys** (`Controller`; all take `hold_seconds`, `block`, `ignore_cancel`). Use these rather than `keyboard_module` so the programmatic-key bracket and takeover gate apply.

| Method | Note |
|--------|------|
| `nose_up`, `nose_down` | pitch; watched takeover keys |
| `roll_left`, `roll_right` | turn-guarded |
| `afterburner`, `airbrake`, `deploy_flares` | |
| `fire_active_weapon` | |
| `switch_weapon` | toggle: primary and secondary; press once per life |
| `padlock_camera` | toggle: resets the tracked padlock state to unknown |
| `activate_special_weapon(block)` | one 0.1 s tap of the special ability (`q`); what it does depends on the jet (ADR 145) |

**Holds** (non-blocking, idempotent while the thread is alive):

| Method | Note |
|--------|------|
| `climb_mode(target_alt, max_s, fuel_floor_pct, exit_lead_s, emergency)` | nose up plus afterburner, or airbrake when emergency; refuses while an eject or evade runs |
| `missile_evade_mode()` | |
| `boundary_turn_mode(max_s, lateral)` | |
| `orient_nose_to_target(error_norm, ...)` | roll toward a bearing, shared cooldown |

**Loops** (independent pairs, no shared state, start and stop in either order):

| Pair | Does |
|------|------|
| `start_search_and_destroy_loop` / `stop_...` | padlock plus fire |
| `start_boresight_engage_loop` / `stop_...` | fire only |
| `start_cloak_loop` / `stop_cloak_loop` | `activate_special_weapon` every `jas39_mission.cloak_press_interval_s`; safe only on a jet where `q` is not a toggle |

**End of life:**

| Method | Note |
|--------|------|
| `pursue_and_engage(on_complete, weapon_already_switched)` | needs a `TargetTracker`, and falls through to a dive without one (rule 13); cancels the mission |
| `eject_and_dive(on_complete, weapon_already_switched)` | |
| FSM seam | `analyzer.trigger_event("eject_started")` before, `"eject_complete"` in `on_complete`; pursuit is a behavior inside `GAME_BATTLE_EJECT` |

**Perception** (`self._analyzer`):

| Call | Note |
|------|------|
| `get_telemetry()` | snapshot: `altitude_fresh()`, `altitude.stable_value`, `altitude.ts`, `pitch_angle_deg()`; refreshes about every 3 s |
| `get_ammo_missiles()`, `get_ammo_flares()`, `get_afterburner_fuel_pct()` | `None` when unread or stale |
| `game_state` | `GameState` enum |

**Guards and state:** `arm_turn_guard(seconds)`, `is_mission_running()`, `_mission_cancel`,
`_eject_weapon_switched` and `is_secondary_weapon_active()`, `is_climbing()`.

## 5. Implementation contract

Rules every mission follows. Each one exists because of a measured failure.

1. **Lock.** Automatic launches (battle entry, respawn, `u`) use `_mission_lock.acquire(blocking=False)` and return when held. A dedicated hotkey that must take over cancels the running mission first and then uses `acquire(timeout=...)`. Release only in `finally`, guarded by `if self._mission_lock.locked()`.
2. **Reset order.** After acquiring the lock, arm the turn guard, then clear `_mission_complete` and `_mission_cancel`, then start the runner.
3. **Wait loop.** The calling thread polls `_mission_complete.wait(0.05)`, calls `cancel_mission()` on `_mission_exit_requested()`, joins the runner for 2 s and sleeps 0.2 s. Copy the skeleton from `mission_j20` or `mission_jas39`.
4. **Cancel-aware waits.** Inside the mission, wait with `_mission_cancel.wait(timeout)`, never `time.sleep`. Any new daemon thread must stop on a `threading.Event` (the stoppable-thread rule in `CLAUDE.md`).
5. **Clean up on every exit.** Stop every loop and hold the mission started in `finally`, each stop guarded on its own, so cancel, exception, exit request and hand-off all end them and one failing stop cannot skip the next. A loop that outlives its mission keeps pressing keys into the next life.
6. **One writer per axis.** Press pitch keys only when no climb hold, spawn guard or eject owns the axis (`is_climbing()`, `is_spawn_guarding()`, `is_ejecting()`). A key with two writers is a bug.
7. **Fresh data only.** Never command on stale telemetry. Act on new samples by comparing the timestamp, because the altitude and angle values refresh about every 3 s.
8. **Toggles.** `switch_weapon` and `padlock_camera` toggle. Track state and press once. A double press leaves the primary selected and looks like a bad weapon read. A key is repeated blindly only when the operator has confirmed it is not a toggle on that jet (as for `q` on the JAS39).
9. **Public helpers only.** Every key goes through a `Controller` helper. For watched keys the programmatic bracket stops a raw `keyboard_module.press` from reading as the player and cancelling the mission into manual takeover; for every key, `_execute_key_press` suppresses it during manual takeover (SAF-001).
10. **Takeover and cleanup.** Any new key writer must be stopped in `release_for_manual_takeover` and `cleanup()`.
11. **Lock timeouts.** Any lock a background thread can hold uses `acquire(timeout=N)` from mission and main-loop paths.
12. **Record the launch.** Every launch path calls `_set_last_mission(name)` before starting the thread. It enables auto restart and stamps the battle clock. `_start_default_mission` does this for the automatic paths.
13. **Never dive on a fault.** A missing collaborator (no `TargetTracker`, FSM refusing `eject_started`) means log an error and hold until cancelled. `pursue_and_engage` would otherwise fall back to `eject_and_dive` and dive an armed aircraft. This rule overrides default 5 and any assumption made for an unmappable bullet.

## 6. Wiring checklist

Taken from what `mission_jas39` touched. `<x>` is the new mission name.

| # | File | Change |
|---|------|--------|
| 1 | `wingman/controller_config.py` | field on `ControllerConfig` and a line in `from_config` reading `cfg.get("<x>_mission")` |
| 2 | `wingman/controller.py` `__init__` | read the block with `getattr(config, "<x>", None) or {}` into `self._<x>_*` |
| 3 | `wingman/controller.py` | `mission_<x>(self)`, with the docstring line `Compatible Jets: ...` |
| 4 | `wingman/controller.py` `_start_default_mission` | add `"<x>"` to the name mapping |
| 5 | `wingman/controller.py` `restart_last_mission` | a branch for `"<x>"`; without it a respawn silently restarts the default mission |
| 6 | `wingman/config_schema.py` | a `Section` for the block, and `"<x>"` in the `default_mission` `choices`. A misspelt key must fail validation |
| 7 | `wingman/config.yaml` | the block, with comments giving the reason for each number |
| 8 | `docs/adr/` | next free number, status `Draft`, per `CLAUDE.md` |
| 9 | `tests/test_mission_<x>.py` | follow `tests/test_mission_jas39.py` (or `test_mission_su30.py` for a scripted mission): step order, toggles, stale data, cancel at each step, lock release, launch through `default_mission`, restart branch, shipped config matches the spec, schema |
| 10 | `README.md`, `docs/architecture.md`, `docs/job-aids/001-setup-and-usage.md` | mission description, the `default_mission` choices and the `_last_mission` values |

Rows 4, 5 and 6 each hard-code mission names. Missing one fails silently:
`_start_default_mission` falls back to j20 for an unknown name, and a respawn
restarts the wrong mission.

Only when the spec asks for a dedicated hotkey (default 1), also:

| # | File | Change |
|---|------|--------|
| H1 | `wingman/keybindings.py` | `MISSION_<X>_KEY` |
| H2 | `wingman/controller.py`, `tests/test_keybindings.py` | import the key in the controller's keybindings import, and add its name to the re-export list in the test |
| H3 | `wingman/controller.py` hotkey section | `start_<x>_mission`, modelled on `start_su30_mission`: debounce 0.5 s, force `GAME_BATTLE` via `manual_force_battle` unless already there, `_set_last_mission("<x>")`, start the thread with `preempt=True` |
| H4 | the three docs in row 10 | a hotkey row |

`wingman/main.py` needs no change for a new hotkey. The nested lane requires
`ctrl+alt` for every operator-display hotkey already. Touch `INJECTABLE_KEYS`
only if wingman itself presses the key (as it does `u` in the GAME_STARTING loop).

## 7. Definition of done

* Every bullet in the spec's "sequence" is implemented by the block the reply named, or by a `new` routine that has its own test.
* The numbers in the bullets are in `config.yaml` and the schema, and a test pins the shipped config to them.
* `mission.default_mission: <x>` validates and launches the mission, and `restart_last_mission` resumes it.
* `make test` passes.
* The reply and the ADR list the log lines to check on a live trial, and each is emitted by the code and covered by a test that asserts it.
* An ADR exists as `Draft`, recording each deviation from the spec, each default applied from section 2.2, and any shared-behavior decision.
* The mission is in the README, architecture and job-aid docs (row 10, plus H4 if it has a hotkey).
* The changes are in the working tree, uncommitted.

`make tp` and `make rr-path1-gate` cover the runtime path and are worth running
before a live trial. A live trial is still needed before the ADR leaves `Draft`.

## 8. Notes and caveats

* **ADR 075 lists a shorter priority order** (no BoundaryTurn or Regroup). It is Accepted, so it is not edited; `_PRIORITY_ORDER` is current.
* **`u` launches the configured mission and does not preempt** (ADR 145). Its handler is still named `start_j20_mission` and its key `MISSION_J20_KEY`, but it calls `_start_default_mission()`. Pressing it while a mission holds the lock is a no-op apart from a log line. `o` starts `mission_su30` directly and preempts (ADR 144, ADR 111).
* **The altitude floor has one per-mission exception** (ADR 147, extended by ADR 149): `mission_su30` flies with `su30_mission.alt_floor_m` (3000) and no sustain band, and `mission_f111` with `f111_mission.alt_floor_m` (3000). It is keyed on the last launched mission being `"su30"` or `"f111"` (`_own_altitude_floor_in_play_m`), not on a mission-level setting. ADR 141 stays Accepted and unchanged.
* **`j20_mission.*` is mostly shared config despite its name.** Only `target_painting_mode` is J20's (read by the search-and-destroy weapon loop). The rest tunes the shared Engage tactic through `EngageNavigator`, so a new mission that edits it changes J20 too.
* **`jet_profile.has_padlock` is read but nothing branches on it** (Design 011). A boresight-only jet is still a mission-level choice today.
* **`mission_su30` is the scripted counterpart** and is still Draft, as are ADR 144 and ADR 147. Read its code for a worked hand-off mission, but not its ADRs as settled.
* **`mission_loiter` predates this contract.** It has its own hotkey and a `restart_last_mission` branch, but is not a `default_mission` choice. Do not model a new mission on it.
* **`docs/missions/` files are not numbered.** The `CLAUDE.md` numbering rule for `docs/` subdirectories does not match these names; they keep the names the operator gave them.

## 9. Related documents

* `docs/adr/075-afterburner-fuel-perception-and-fully-adaptive-j20-mission.md`: why J20 is adaptive
* `docs/adr/111-the-hold-pre-empts-the-running-mission.md`: preempting hotkeys
* `docs/adr/118-search-and-destroy-lifecycle-is-serialised.md`: start and stop serialisation for the fire loops
* `docs/adr/128-afterburner-during-an-incoming-alert.md`: the missile afterburner hold
* `docs/adr/132-fly-the-spawn-heading-first.md`: the turn guard
* `docs/adr/138-boundary-turn-requires-mission-running.md`: which tactics read the lock
* `docs/adr/141-phase1-altitude-floor-stall-prevention-and-emergency-yields.md`: the 4000 m altitude floor
* `docs/adr/144-mission-su30-scripted-sequence.md`: the scripted counterpart
* `docs/adr/145-mission-jas39-cloak-and-u-launches-the-configured-mission.md`: `u` and `default_mission` as the launch path; the cloak loop
* `docs/adr/147-mission-su30-flies-its-own-altitude-doctrine.md`: su30's floor exception
* `docs/hldd/015-target-tracking-pursuit-mode-hldd.md`: pursuit mode and its FSM seam
