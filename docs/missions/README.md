# Missions — Authoring Contract

| Status | Date       | Wingman Version |
|--------|------------|-----------------|
| Draft  | 2026-09-24 | 1.8.11          |

This file is the part that does not change from mission to mission: what a
mission inherits, the building blocks it is made from, the rules it must
follow, and the files it touches. A mission's own file holds only its spec.

| File | Role |
|------|------|
| `README.md` | this contract; read by Claude, not copied |
| `_template.md` | the blank spec form; **copy this** |
| `j20.md` | the form filled in for `mission_j20`, plus how J20 works today |
| `su30.md` | the operator's own spec, and the layout `_template.md` follows |

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
collision with the 4000 m altitude floor or a second writer on the same key.
If a bullet cannot be mapped, state your assumption in the reply and proceed.

Do not change mission_j20, search_and_destroy or boresight_engage. If a bullet
needs a change to shared behavior (README section 3), stop and report the
options instead of editing it.
Meet the definition of done in section 7 and leave the changes uncommitted.
```

## 2. Writing the sequence

The spec is the `## name` and `## sequence` of the template, in the layout of
`su30.md`. Nothing else is required.

### 2.1 What goes in a bullet

Bullets run in order. Put numbers and conditions in the bullet ("after reaching
3000 altitude set nose angle to -10 degrees"); Claude derives the config keys.

* **Engagement is a bullet.** Name `search_and_destroy` (padlock plus fire) or `boresight_engage` (fire only), and put it where it should start. Position matters: `su30.md` lists boresight before the weapon switch, and the code starts it after, so that it fires the secondary. If the order matters to you, say why in the bullet.
* **Altitude and angle targets respect the floor.** The unconditional altitude floor is 4000 m (`behavior_tree.climb.alt_floor_m`). A scripted flight path below it is overridden by a climb, not held.
* **A mission that returns early turns off the lock-gated behavior** in section 3.1. A hand-off mission should say what takes over.
* **A bullet that asks for something shared behavior already does** (climbing, cruise afterburner, evading) is a change to every mission. See default 8.

### 2.2 When the spec is silent

Claude applies these and records them in the ADR.

1. **Hotkey.** Pick an unused key and flag it for you to check against the game's bindings. `test_keybindings` checks only against keys wingman itself injects, and the hotkey is grabbed with `suppress=False`, so the game also receives it (ADR 144 records `o` as unverified).
2. **Hotkey while a mission runs.** Preempt: cancel the running mission and take over (ADR 111). J20's `u` is the exception and skips.
3. **Default mission.** Not selectable as `mission.default_mission` unless the spec says so.
4. **End.** Cancelled, unless the last bullet is a hand-off.
5. **Waits.** Every wait is bounded. On timeout, log a warning and continue with the next bullet, unless continuing would dive an armed aircraft (rule 13 in section 5), in which case hold until cancelled.
6. **Logging.** One `step N/M` line per bullet, so a live trial is readable.
7. **Config.** Numbers from the bullets go in a `<name>_mission` block in `config.yaml` with a schema entry and a comment giving the reason for each. Nothing is hard-coded without saying so.
8. **Shared behavior.** If a bullet needs a change to shared behavior (section 3), stop and report the options: a global change that affects every mission, or a per-mission switch, which does not exist today and would be new code. The choice is the operator's each time. Never edit shared code silently.
9. **Acceptance.** Claude reports the log lines to check on a live trial in its reply and in the ADR, as ADR 144 did. The spec carries no acceptance section.

## 3. What every mission inherits

None of this belongs to a mission. It is shared tree and watchdog code driven
by global config, and **there is no per-mission switch**: config is read once
and applies to whichever mission is flying. A spec that wants a different value
or an opt-out is asking for a change to every mission (default 8 in
section 2.2).

### 3.1 Behavior gated on the mission lock

These read `is_mission_running()` (the snapshot's `mission_running`), so
holding the lock is what switches them on:

* sustain climb (`make_sustain_climb_condition` in `behavior_tree.py`)
* BoundaryTurn entry (ADR 138)
* the tree's Engage, Regroup, Climb-steering and seek-center actuation (`_may_fly` in `BehaviorTreeHandler.tick`)
* cruise afterburner (`note_afterburner_cruise`)
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
(missiles above zero) below `climb.sustain.enter_below_alt` until
`exit_above_alt`. The floor and the sustain entry are both 4000 and a test
(`test_mission_j20_altitude_doctrine_is_4000m_everywhere`) pins them equal.

### 3.3 Tree-independent, every tick

| What | Where | Rule |
|------|-------|------|
| Cruise afterburner | `note_afterburner_cruise` | hold from `rearm_fuel_pct` down to `min_fuel_pct`, then let it recharge (`behavior_tree.afterburner_cruise`) |
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
| Manual takeover (`i j k l`, arrows, Enter) | `_handle_maneuver_key_press`, `release_for_manual_takeover` | No; `u` resumes |
| End key | `cancel_mission_hotkey` | No; auto restart is switched off |
| Click to continue, lobby entry | `main.py` | No; the match is over |
| Program exit | `_mission_exit_requested()` | No |

A restart runs whichever mission was launched last (`_set_last_mission`).

## 4. Building blocks

A mission is assembled from these. Do not add new flight code when one fits;
`mission_su30` needed exactly one new routine.

**Flight and weapon keys** (`Controller`; all take `hold_seconds`, `block`, `ignore_cancel`). Use these rather than `keyboard_module` so the programmatic-key bracket and takeover gate apply.

| Method | Note |
|--------|------|
| `nose_up`, `nose_down` | pitch; watched takeover keys |
| `roll_left`, `roll_right` | turn-guarded |
| `afterburner`, `airbrake`, `deploy_flares` | |
| `fire_active_weapon` | |
| `switch_weapon` | toggle: primary and secondary; press once per life |
| `padlock_camera` | toggle: resets the tracked padlock state to unknown |

**Holds** (non-blocking, idempotent while the thread is alive):

| Method | Note |
|--------|------|
| `climb_mode(target_alt, max_s, fuel_floor_pct, exit_lead_s, emergency)` | nose up plus afterburner, or airbrake when emergency; refuses while an eject or evade runs |
| `missile_evade_mode()` | |
| `boundary_turn_mode(max_s, lateral)` | |
| `orient_nose_to_target(error_norm, ...)` | roll toward a bearing, shared cooldown |

**Engagement loops** (independent pairs, no shared state, start and stop in either order):

| Pair | Does |
|------|------|
| `start_search_and_destroy_loop` / `stop_...` | padlock plus fire |
| `start_boresight_engage_loop` / `stop_...` | fire only |

**End of life:**

| Method | Note |
|--------|------|
| `pursue_and_engage(on_complete, weapon_already_switched)` | needs a `TargetTracker`; cancels the mission; falls through to a dive |
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

1. **Lock.** Automatic launches use `_mission_lock.acquire(blocking=False)` and return when held. A hotkey that must take over cancels the running mission first and then uses `acquire(timeout=...)`. Release only in `finally`, guarded by `if self._mission_lock.locked()`.
2. **Reset order.** After acquiring the lock, arm the turn guard, then clear `_mission_complete` and `_mission_cancel`, then start the runner.
3. **Wait loop.** The calling thread polls `_mission_complete.wait(0.05)`, calls `cancel_mission()` on `_mission_exit_requested()`, joins the runner for 2 s and sleeps 0.2 s. Copy the skeleton from `mission_j20`.
4. **Cancel-aware waits.** Inside the mission, wait with `_mission_cancel.wait(timeout)`, never `time.sleep`. Any new daemon thread must stop on a `threading.Event` (the stoppable-thread rule in `CLAUDE.md`).
5. **Clean up on every exit.** Stop every loop and hold the mission started in `finally`, so cancel, exception, exit request and hand-off all end them. A loop that outlives its mission keeps pressing keys into the next life.
6. **One writer per axis.** Press pitch keys only when no climb hold, spawn guard or eject owns the axis (`is_climbing()`, `is_spawn_guarding()`, `is_ejecting()`). A key with two writers is a bug.
7. **Fresh data only.** Never command on stale telemetry. Act on new samples by comparing the timestamp, because the altitude and angle values refresh about every 3 s.
8. **Toggles.** `switch_weapon` and `padlock_camera` toggle. Track state and press once. A double press leaves the primary selected and looks like a bad weapon read.
9. **Public helpers only.** Held keys in the watched set must go through the helpers so the programmatic bracket applies. A raw `keyboard_module.press` reads as the player and cancels the mission into manual takeover.
10. **Takeover and cleanup.** Any new key writer must be stopped in `release_for_manual_takeover` and `cleanup()`.
11. **Lock timeouts.** Any lock a background thread can hold uses `acquire(timeout=N)` from mission and main-loop paths.
12. **Record the launch.** Every launch path calls `_set_last_mission(name)` before starting the thread. It enables auto restart and stamps the battle clock.
13. **Never dive on a fault.** A missing collaborator (no `TargetTracker`, FSM refusing `eject_started`) means log an error and hold until cancelled. `pursue_and_engage` would otherwise fall back to `eject_and_dive` and dive an armed aircraft.

## 6. Wiring checklist

Taken from what `mission_su30` touched. `<x>` is the new mission name.

| # | File | Change |
|---|------|--------|
| 1 | `wingman/keybindings.py` | `MISSION_<X>_KEY` |
| 2 | `wingman/controller.py`, `tests/test_keybindings.py` | import the key in the controller's keybindings import, and add its name to the re-export list in the test |
| 3 | `wingman/controller_config.py` | field on `ControllerConfig` and a line in `from_config` reading `cfg.get("<x>_mission")` |
| 4 | `wingman/controller.py` `__init__` | read the block with `getattr(config, "<x>", None) or {}` into `self._<x>_*` |
| 5 | `wingman/controller.py` | `mission_<x>(self, preempt: bool = False)`, with the docstring line `Compatible Jets: ...` |
| 6 | `wingman/controller.py` hotkey section | `start_<x>_mission`: debounce 0.5 s, force `GAME_BATTLE` via `manual_force_battle` unless already there, `_set_last_mission("<x>")`, start the thread |
| 7 | `wingman/controller.py` `restart_last_mission` | a branch for `"<x>"`; without it a respawn silently restarts the default mission |
| 8 | `wingman/controller.py` `_start_default_mission` | only if it can be the default; extend the name mapping |
| 9 | `wingman/config_schema.py` | a `Section` for the block, and `default_mission` `choices` if selectable. A misspelt key must fail validation |
| 10 | `wingman/config.yaml` | the block, with comments giving the reason for each number |
| 11 | `docs/adr/` | next free number, status `Draft`, per `CLAUDE.md` |
| 12 | `tests/test_mission_<x>.py` | follow `tests/test_mission_su30.py`: step order, toggles, stale data, cancel at each step, lock release, hotkey, restart branch, shipped config matches the spec, schema |
| 13 | `README.md`, `docs/architecture.md`, `docs/job-aids/001-setup-and-usage.md` | hotkey row and mission description |

Rows 6, 7, 8 and 9 each hard-code mission names. Missing one fails silently:
a respawn restarts the wrong mission.

`wingman/main.py` needs no change for a new hotkey. The nested lane requires
`ctrl+alt` for every operator-display hotkey already. Touch `INJECTABLE_KEYS`
only if wingman itself presses the key (as it does `u` in the GAME_STARTING loop).

## 7. Definition of done

* Every bullet in the spec's "sequence" is implemented by the block the reply named, or by a `new` routine that has its own test.
* The numbers in the bullets are in `config.yaml` and the schema, and a test pins the shipped config to them.
* `make test` passes.
* The reply and the ADR list the log lines to check on a live trial, and each is emitted by the code and covered by a test that asserts it.
* An ADR exists as `Draft`, recording each deviation from the spec, each default applied from section 2.2 (the chosen hotkey in particular), and any shared-behavior decision.
* The hotkey row is in the README, architecture and job-aid tables.
* The changes are in the working tree, uncommitted.

`make tp` and `make rr-path1-gate` cover the runtime path and are worth running
before a live trial. A live trial is still needed before the ADR leaves `Draft`.

## 8. Notes and caveats

* **ADR 075 lists a shorter priority order** (no BoundaryTurn or Regroup). It is Accepted, so it is not edited; `_PRIORITY_ORDER` is current.
* **The `u` hotkey does not preempt.** Pressing it while a mission holds the lock is a no-op apart from a log line. `o` preempts (ADR 144, ADR 111), and so does any new mission by default (section 2.2, default 2).
* **`j20_mission.*` is mostly shared config despite its name.** Only `target_painting_mode` is J20's (read by the search-and-destroy weapon loop). The rest tunes the shared Engage tactic through `EngageNavigator`, so a new mission that edits it changes J20 too.
* **`jet_profile.has_padlock` is read but nothing branches on it** (Design 011). A boresight-only jet is still a mission-level choice today.
* **`mission_su30` is the scripted counterpart** and is still Draft. Read its code for a worked hand-off mission, but not its ADR as settled.
* **`docs/missions/` files are not numbered.** The `CLAUDE.md` numbering rule for `docs/` subdirectories does not match these names; they keep the names the operator gave them.

## 9. Related documents

* `docs/adr/075-afterburner-fuel-perception-and-fully-adaptive-j20-mission.md`: why J20 is adaptive
* `docs/adr/111-the-hold-pre-empts-the-running-mission.md`: preempting hotkeys
* `docs/adr/118-search-and-destroy-lifecycle-is-serialised.md`: start and stop serialisation for the fire loops
* `docs/adr/128-afterburner-during-an-incoming-alert.md`: the missile afterburner hold
* `docs/adr/132-fly-the-spawn-heading-first.md`: the turn guard
* `docs/adr/138-boundary-turn-requires-mission-running.md`: which tactics read the lock
* `docs/adr/144-mission-su30-scripted-sequence.md`: the scripted counterpart
* `docs/hldd/015-target-tracking-pursuit-mode-hldd.md`: pursuit mode and its FSM seam
