# Mission — JAS39 (mission_jas39)

| Status | Date       | Wingman Version |
|--------|------------|-----------------|
| Draft  | 2026-09-24 | 1.8.11          |

`mission_j20` with one addition: the JAS39's special weapon, a cloak, is
activated with `q` whenever it is available. Everything not listed below
follows `docs/missions/README.md` (shared behavior in section 3, defaults in
section 2.2).

Implement per `docs/missions/README.md`.

## name

mission_jas39

## sequence

* on battle start or respawn, fly the spawn heading for 10 seconds with no left or right turns
* engage with search_and_destroy
* at the same time, start a cloak loop that presses `q` every 3 seconds until the mission ends, so the cloak is re-activated within 3 seconds of its cooldown ending
* run until cancelled

## notes

* **Same as J20 otherwise.** The turn guard, `search_and_destroy`, and the respawn restart match `mission_j20` (see `j20.md`). Climbing, evading, the boundary turn, disengage and the missiles-empty response are shared behavior and apply unchanged.
* **Why an interval loop.** Wingman has no signal for when the special ability is off cooldown. Pressing `q` every 3 seconds re-activates the cloak within 3 seconds of it becoming available. Presses during the cooldown have no effect in game.
* **`q` is not a toggle on the JAS39.** The operator confirmed that pressing `q` while the cloak is active does not deactivate it, so repeated presses are safe. README section 5 rule 8 (track toggle state, press once) does not apply to this key on this jet.
* **Press `q` through the controller.** Add a public helper (for example `activate_special_weapon`) that calls `_execute_key_press(SPECIAL_ABILITY, ...)`, as `reload_flares` does. Do not use a raw `keyboard_module` press (README section 5, rule 9).
* **Cloak loop lifecycle.** The loop is a daemon thread that waits with `stop_event.wait(timeout=interval)` and also stops on `_mission_cancel` (README section 5, rule 4). It is stopped in the mission's `finally` (rule 5) and in `release_for_manual_takeover` and `cleanup()` (rule 10), so it never presses `q` into the next life or during manual flight.
* **Config.** The interval goes in a `jas39_mission` block in `wingman/config.yaml` as `cloak_press_interval_s: 3.0`, with a schema entry and a test pinning the shipped value (README section 2.2, default 7).
* **Shared flare reload.** `AmmoEventsHandler.handle_low_flares` also presses `q` (`SPECIAL_ABILITY`) when the flare count drops to 2 (README section 3.3). On the JAS39, `q` is the cloak and is already pressed every 3 seconds, so that extra press is harmless. No change to shared behavior is needed for this mission.
* **Launch: no hotkey of its own (operator, 2026-09-24).** The mission is chosen in the config, not by a key: set `mission.default_mission: jas39` in `wingman/config.yaml`. Battle entry and the `u` hotkey then launch it, and a respawn restarts it. `u` launches whichever mission `default_mission` names, so this replaces README section 2.2 defaults 1 to 3 for this mission. Like J20's `u`, it skips rather than preempts when a mission is already running (ADR 145).
* **Live trial log line.** One `step N/M` line per bullet (default 6). The cloak loop logs its start and stop at INFO and each press at DEBUG, so the 3 s cadence does not flood the console.
