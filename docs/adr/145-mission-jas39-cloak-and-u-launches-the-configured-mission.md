# ADR 145 — mission_jas39: J20 Plus the Cloak, and `u` Launches the Configured Mission

| Status | Date       | Wingman Version |
|--------|------------|-----------------|
| Draft  | 2026-09-24 | 1.8.11          |

## Context

The operator specified a JAS39 mission in `docs/missions/jas39.md`: the same as
`mission_j20`, plus the JAS39's special weapon, a cloak on `q`, activated
whenever it is available:

1. on battle start or respawn, fly the spawn heading for 10 seconds
2. engage with `search_and_destroy`
3. at the same time, press `q` every 3 seconds until the mission ends
4. run until cancelled

Wingman has no signal for when the special ability is off cooldown. The operator
confirmed two facts about the JAS39 that make blind retrying safe: a `q` press
during the cooldown does nothing, and a `q` press while cloaked does not turn
the cloak off. The operator first asked for a 1 s retry, then settled on 3 s.

The missions contract (`docs/missions/README.md` section 2.2, default 1) says a
new mission gets its own hotkey. While this ADR was being implemented, the
operator overruled that: a mission needs no activation key of its own. `u`
activates the mission, and the config file says which mission that is.

## Decision

**D1. `mission_jas39` is J20's shape plus one new routine.** The mission
(`controller.py`) has J20's lock, cancel, runner thread and wait loop, and a
single entry point for battle entry and respawn. It is built from existing
blocks: `arm_turn_guard` for step 1 (ADR 132) and
`start_search_and_destroy_loop` for step 2. Only step 3 is new: the cloak loop
(D2). `mission_j20`, `search_and_destroy` and `boresight_engage` are
unchanged. Like `mission_j20`, the mission takes the lock with
`acquire(blocking=False)` and skips when a mission already holds it.

Each bullet logs one `step N/M` line (README default 6). Every exit path
(cancel, exception, exit request) stops both loops in `finally`, each stop
guarded on its own, so one failing stop can neither skip the other nor keep the
lock held.

**D2. The cloak loop presses `q` on a fixed interval.** It is a new pair,
`start_cloak_loop` / `stop_cloak_loop`, modelled on
`start_boresight_engage_loop` (ADR 144):

- **Its own state.** It has its own stop event, thread and lifecycle lock,
  shares nothing with the engagement loop beside it, and start and stop are
  serialised with a timeout (ADR 118).
- **Timing.** The first press is immediate, because the cloak is available at
  spawn. After that it presses every `jas39_mission.cloak_press_interval_s`
  (3.0 s), timed from the start of the previous press. It waits out the
  interval in 0.1 s slices of `stop.wait`, so a stop or a `_mission_cancel` ends
  it within a slice.
- **Where it stops.** It ends on `_mission_cancel` as well as on its own stop.
  It is stopped in the mission's `finally`, in `release_for_manual_takeover`
  (SAF-001) and in `cleanup()`, so it never presses `q` into the next life or
  into manual flight.

Each press goes through a new public helper, `activate_special_weapon`. It calls
`_execute_key_press(SPECIAL_ABILITY, hold_seconds=0.1)`, as `reload_flares`
does:

- **Hold.** The explicit 0.1 s matters: the default hold is 2.5 s, which on a
  3 s loop would keep `q` down most of the time.
- **Why the helper.** `_execute_key_press` suppresses every key except flares
  during manual takeover (SAF-001). `q` is not in `_WATCHED_MANEUVER_KEYS`, so
  the takeover suppression, not the programmatic-key bracket, is why the helper
  is required.
- **Logging.** The helper adds no log line of its own. `_execute_key_press`
  already logs each press at DEBUG, and the loop logs only its start (with the
  interval) and its stop (with the press count) at INFO.

README section 5 rule 8 (track a toggle, press once) does not apply: `q` is not
a toggle on the JAS39.

**D3. No mission hotkey: `u` launches the configured mission (operator,
2026-09-24).** The `u` handler used to start `mission_j20` by name. It now calls
`_start_default_mission()`, the helper battle entry and the restart fallback
already use, so all three launch whichever mission `mission.default_mission`
names. The config key is therefore how a mission is chosen:

- **Choices.** `default_mission` accepts `jas39` alongside `j20` and `su30`
  (schema `choices`). `_start_default_mission` resolves the name through one
  mapping, and an unknown name falls back to `j20`.
- **Respawn.** `restart_last_mission` gained a `"jas39"` branch, so a respawn
  resumes the mission that ran last.
- **Everything else about `u` is kept.** It still filters wingman's own injected
  presses by the programmatic bracket, still resumes from `GAME_BATTLE_MANUAL`,
  still forces `GAME_BATTLE`, and still skips, rather than preempts, when a
  mission is running (README section 8). With `default_mission` unset (tests,
  replay lanes) or `j20`, `u` behaves exactly as before.
- **`o` is unchanged.** It still starts `mission_su30` directly, with preempt.

**D4. Config.** A new `jas39_mission` block holds the two numbers from the spec:
`turn_guard_s: 10.0` and `cloak_press_interval_s: 3.0`, each with a comment
giving its reason. Both have schema entries, and a test pins the shipped values
to the spec. The turn guard has its own key, although J20 flies the same 10 s
from `mission.j20_turn_guard_s`, so that retuning one mission does not silently
retune the other. `cloak_press_interval_s` has a schema floor of 0.5 s, because
with a 0.1 s tap anything shorter is holding the key down rather than retrying
it.

The shipped `mission.default_mission` stays `su30` (the operator's setting of
2026-09-24). Setting it to `jas39` flies this mission.

**D5. The shared flare reload is not changed.**
`AmmoEventsHandler.handle_low_flares` presses the same `q` when the flare count
reaches 2 (README section 3.3). On the JAS39 that press is a cloak press, one
more on top of the loop, and harmless because `q` does not toggle. Changing it
would be a change to every mission (README default 8), so it is left as is.

### Defaults from `docs/missions/README.md` section 2.2

| Default | Applied |
|---------|---------|
| 1. Hotkey | Overruled by the operator: no hotkey; `u` plus `mission.default_mission` (D3) |
| 2. Hotkey while a mission runs | No own hotkey; `u` keeps its skip behaviour |
| 3. Default mission | Overruled: selectable, because the config is how a mission is chosen. The shipped value is unchanged (`su30`) |
| 4. End | Cancelled; the spec's last bullet is "run until cancelled" |
| 5. Waits | The mission has no condition waits; the cloak interval is a timer, and the run-until-cancelled wait is J20's |
| 6. Logging | One `step N/M` line per bullet (D1) |
| 7. Config | The `jas39_mission` block with comments, schema and a pinning test (D4) |
| 8. Shared behavior | Flare reload left unchanged (D5). The `u` change is shared, and was made on the operator's explicit instruction (D3) |
| 9. Acceptance | The live-trial log lines are under Validation Strategy |

## Consequences

**`u` no longer means J20.** With the shipped `default_mission: su30`, pressing
`u` now starts `mission_su30`, where before it started J20. To fly J20 from `u`,
set `default_mission: j20`. The same holds for a `u` pressed to hand back from
manual takeover: it resumes the configured mission, not J20.

**The cloak ends with the mission.** Anything that cancels the mission stops the
loop, so there are no cloak presses in these cases (apart from the flare reload
in D5):

- the missiles-empty pursuit and eject, since `pursue_and_engage` cancels the
  mission;
- the 10 s disengage roll, until the mission restarts;
- the respawn screen.

**Firing may break the cloak.** `search_and_destroy` presses fire every
`mission.weapon_loop_interval` (1.0 s) with no target check. If firing uncloaks
the aircraft in game, the cloak will barely hold during an engagement.
Unverified. See Open Questions.

**The flare reload logs a misleading line on this jet.** When flares reach 2,
`reload_flares` logs "Reloading flares via SPECIAL_ABILITY key", but on the JAS39
the press is a cloak press. It is left as is (D5). Read it as a cloak press when
reviewing a JAS39 log.

**The cadence is 3.0 to 3.1 s.** The interval is timed from the start of each
press and waited out in 0.1 s slices, so the next press can land up to one slice
late. At worst, the cloak comes back about 3.1 s after it becomes available.

**`jet_profile.active` is not touched.** It stays `j20` (`has_padlock: true`),
and nothing branches on it (Design 011). The JAS39 engages with the padlock,
which is also why ADR 144's `is_padlock_blocked()` stays false for this mission.

## Open Questions

1. Does firing a missile, or firing the gun, break the cloak? If it does, should
   the cloak loop or the fire loop yield to the other?
2. The cloak's duration and cooldown are unknown. A HUD signal for "special
   ability ready" would let the mission press exactly when it is available,
   instead of retrying on a timer.
3. Should the cloak keep running through pursuit and eject? It currently ends
   with the mission.
4. `docs/missions/README.md` still tells a new mission to pick its own hotkey
   (section 2.2 defaults 1 to 3, section 6 rows 1, 2 and 6, section 7). D3
   replaces that with the config key. Updating the contract is the operator's
   call.

## Validation Strategy

Unit tests (`tests/test_mission_jas39.py`, 37 tests) pin:

- the step order and the turn-guard key;
- that engagement is `search_and_destroy`;
- the step log lines;
- that every exit path stops both loops and releases the lock;
- the skip-when-running behaviour;
- the cloak loop's first press, interval, 0.1 s tap, DEBUG-only press logging,
  its stop within a slice, its cancel exit, and that it runs as one instance;
- the loop stopping on takeover and in `cleanup()`, and SAF-001 suppression;
- an end-to-end run through the real loops;
- `u` following the config, the respawn restart, the fallback, and the shipped
  config and schema.

They are mutation-checked. Each of these breaks fails a test:

- dropping the 0.1 s hold;
- dropping the loop stop on takeover;
- dropping the loop stop in cleanup;
- reverting `u` to J20;
- dropping the restart branch;
- dropping the cancel check;
- dropping the loop stop in the mission's `finally`;
- ignoring the interval.

A live trial is still required before this leaves Draft. Set
`mission.default_mission: jas39` and run `make rd`. What to read in
`wingman.log`, in order:

1. `'u' key pressed - starting the configured mission (jas39, ...)` when started
   by hand, and `mission 'jas39' started`
2. `mission_jas39 - step 1/4: 10s turn guard, flying the spawn heading`
3. `mission_jas39 - step 2/4: engaging with search_and_destroy`
4. `mission_jas39 - step 3/4: cloak loop on (q every 3.0s)` and
   `cloak loop started (q every 3.0s)`
5. `mission_jas39 - step 4/4: running until cancelled`
6. at DEBUG, `activate_special_weapon - pressing 'q' key for 0.1 seconds` about
   every 3 s
7. on death or cancel, `cloak loop stopped after N press(es)`, and after the
   respawn, `restarting last mission (JAS39)`

In the game, watch whether the cloak comes back after each cooldown, and whether
firing drops it (Open Question 1).

## Related Documents

- `docs/missions/jas39.md`: the operator's sequence
- `docs/missions/README.md`: the missions contract; see Open Question 4
- `docs/adr/144-mission-su30-scripted-sequence.md`: the boresight loop this
  loop is modelled on, and `mission.default_mission`
- `docs/adr/132-fly-the-spawn-heading-first.md`: the turn guard
- `docs/adr/118-search-and-destroy-lifecycle-is-serialised.md`: start and stop
  serialisation
- `docs/adr/111-the-hold-pre-empts-the-running-mission.md`: why `o` preempts
  and `u` does not
