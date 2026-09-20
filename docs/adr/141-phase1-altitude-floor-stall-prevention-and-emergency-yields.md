# ADR 141 — Phase 1 Reintroduction: Altitude Floor, Stall Prevention, and the Emergency-Yield Companion Fixes

| Status | Date       | Wingman Version |
|--------|------------|-----------------|
| Draft  | 2026-09-20 | 1.8.9           |

## Context

Operator directive, verbatim: *"if speed below 300KPH it should deactivate
break and activate afterburner, during evade manuvers and mission_j20 if
altitude below [the floor] it should automatically fly up."*

This was first implemented on a branch (`test1`) over one long session,
alongside the rest of HLDD 001 Phase 1 (`docs/hldd/001-terrain-avoidance-hldd.md`).
The operator caught and reported six distinct live bugs in that session, in
order:

1. The forced climb didn't fully avoid a crash on respawn (led to an
   emergency-climb-on-respawn buildout).
2. A stuck-at-600m telemetry report (resolved as a genuine low respawn, not
   a bug).
3. "Telemetry goes blank right before death" (resolved: the game's own
   death-cinematic camera, not a wingman fault).
4. A nose-oscillation crash traced to the exit push fighting an
   immediately-restarting emergency hold.
5. A second, distinct nose-oscillation crash traced to blind pulses right
   after a respawn hitting the pitch ceiling.
6. `alt_floor_m` shipped at 3000 while ADR 081 d2's own armed sustain floor
   was already 4000 — a mission_j20 doctrine mismatch between two config
   values that should have agreed from the start.

Every individual fix was live-validated in isolation, but each was stacked
on the same hot code path (`ClimbCondition` in `wingman/behavior_tree.py`,
and the climb-hold actuator in `wingman/controller.py`) before the previous
fix had a chance to soak. The operator reverted the entire branch as a
"major regression" — `git log` on `test1`'s tip shows 2367 insertions across
19 files, all from that one session — and branched a clean `test2` from the
last commit before any of it began.

**This ADR documents the reintroduction as one deliberate, coherent batch**
("Phase 1"), decided after a full post-mortem of the discarded session's own
changelog, instead of replaying that session's many small incremental ADR
edits. The batch is intentionally larger than the operator's three literal
asks (altitude floor, stall prevention, missile-evade yield): it also
includes four companion fixes (D2, D3, D5) that are logically necessary to
make the altitude floor safe to run unattended. Omitting any of them
reproduces a bug the operator already directly witnessed — see each
decision below for which incident it closes.

## Decision

**D1. Altitude floor — a third, rate-independent OR-term on
`ClimbCondition.emergency_active`.** `wingman/behavior_tree.py`'s
`ClimbCondition` already computes an emergency from two triggers: predicted
time-to-ground (ADR 086) and forward terrain occlusion (HLDD 001 Phase 1,
still shadow-only). Neither can see a near-stall: ttg needs a negative
altitude rate to compute anything, and a stalled aircraft can show a
near-zero or even briefly positive rate while critically low. The floor is
deliberately the simplest possible check — `snapshot.altitude <
alt_floor_m` — no confirm-reads debounce beyond that, and folded into
`emergency_active` via plain `or`.

Shipped at `alt_floor_m: 4000` in `config.yaml`, matching
[ADR 081](081-climb-pitch-ceiling-and-sustain-floor.md) d2's existing armed
sustain floor exactly — mission_j20 doctrine is "stay above 4000" in both
places. `tests/test_controller_config.py::test_mission_j20_altitude_doctrine_is_4000m_everywhere`
pins `climb.alt_floor_m` and `climb.sustain.enter_below_alt` to the same
value against the shipped config, specifically so incident 6 above (the
3000-vs-4000 mismatch) cannot silently recur.

**D2. Hard vs. broad emergency — `ClimbCondition.hard_emergency_active`.**
[ADR 107](107-boundary-turn-tactic.md) D4 gave `BoundaryTurn` a
`yields_to_fn` so a certain ground impact outranks the map-edge turn.
Wiring the altitude floor into the same broad `emergency_active` flag
`BoundaryTurn` already yielded to would have meant a floor-only emergency —
which can legitimately stay true for an entire climb back from a low
respawn, far longer than a ttg spike — locks `BoundaryTurn` out of the map
edge for that whole climb. That is exactly the "flew out of the map" shape
the operator caught live in the `test1` session: an aircraft "safely"
climbing straight through the arena boundary because nothing was allowed to
turn it.

Fix: `hard_emergency_active` is a second property, capturing `ttg or
terrain` *before* the floor is folded in. `BoundaryTurn`'s `yields_to_fn`
now reads the narrow hard signal (`ctx.climb_hard_emergency_fn`);
`MissileEvade` and `Idle` (D3) keep reading the broad signal
(`ctx.climb_emergency_fn`), since a missile-evade maneuver or a stalled
handoff to Idle should still step aside for a floor-only emergency.
ADR 107 D4's own reasoning — "hitting the ground is certain, the boundary is
a countdown" — is true of ttg and terrain; the floor is a softer,
preventive backstop, not that.

**D3. MissileEvade and Idle yield to the climb emergency.** Extends
ADR 107 D4's `yields_to_fn` pattern to two more tactics, per the operator's
own wording ("during evade maneuvers ... if altitude below [the floor] it
should automatically fly up"):

- `make_missile_evade_condition(yields_to_fn=...)` — checked first, ahead of
  the incoming-missile predicate. Reads the broad `climb_emergency_fn`
  (D2): a missile-evade roll should not itself be exempt from the floor.
- `make_idle_condition(yields_to_fn=...)` — deliberately narrow. `Idle` sits
  first in `_PRIORITY_ORDER` and previously had no way to cede the airframe
  to anything, including a live climb emergency, for the entire time the
  FSM sits outside `GAME_BATTLE`. Live-traced: an `eject_and_dive` sequence
  aborted mid-dive (the aircraft was alive and flying, not actually dead)
  and handed control back while `game_state` was still
  `GAME_BATTLE_EJECT`, not yet `GAME_BATTLE`. `Climb`'s own emergency
  verdict fired and logged correctly, but `Idle` won selection anyway and
  presses nothing — the aircraft kept flying whatever heading it already
  had, forward and level, straight past the floor. Fixed narrowly: `Idle`
  only steps aside when `game_state == GAME_BATTLE_EJECT` *and*
  `yields_to_fn()` is true — `GAME_LOBBY`/`GAME_STARTING` (no aircraft
  exists to fly at all) are untouched, and no other state gets any new
  access to `GAME_BATTLE_EJECT`.

**D4. Stall prevention — `Controller.note_stall_prevention`.** A new
tree-independent, every-tick watchdog (`wingman/controller.py`), mirroring
`note_afterburner_cruise`'s exact shape (ADR 134 D9 precedent): below
`min_speed_kph` (300, config-driven) for `confirm_reads` consecutive reads
(2), release `AIRBRAKE_KEY` and hold `AFTERBURNER_KEY`; recover on the very
first reading back above the floor (the same engage/recover asymmetry
ADR 086 d3 already uses for the ttg trigger's bypass window). Called from
`tick_handlers.py` right after `note_afterburner_cruise`, gated on
`_manual_takeover_active()` and `game_state in (GAME_BATTLE,
GAME_BATTLE_MANUAL, GAME_BATTLE_EJECT)`.

Deliberately does **not** exempt `Climb`'s own emergency airbrake hold —
unlike cruise-afterburner's climb-emergency exception. The whole point is
catching the case where an emergency climb's own airbrake-and-suppressed-
afterburner posture bleeds the aircraft's own speed down toward a stall.
Live-observed the same night this shipped: `MissileEvade` held the airframe
roughly 6 seconds while speed bled to 27 KPH, and altitude then collapsed
about 745 m in under a second — the near-stall neither existing trigger's
rate requirement can see. `tests/test_stall_prevention.py` pins this exact
shape (`test_the_exact_live_27_kph_shape`).

Both `AIRBRAKE_KEY`/`AFTERBURNER_KEY` presses route through
[ADR 139](139-behavior-tree-slot-composition-and-wiring.md) D4's
`_may_hold_key(key, requester)` arbiter, which already centralizes the four
pre-existing call sites — this adds a fifth requester case
(`"stall_prevention"`), unconditionally `True`, same shape as `"eject"` and
`"climb"`.

**D5. Two climb-hold actuator fixes, bundled here because D1 makes the
actuator dramatically more active.** Before this ADR, an emergency climb
fired only on a ttg spike or (shadowed) terrain occlusion — comparatively
rare. The altitude floor makes *any respawn below 4000 m* trigger one
immediately, which is common. Both of the operator-caught oscillation
crashes below happened after the floor made this code path hot; neither
would matter as much, at the old trigger frequency, sitting unfixed.

- **D5a — exit-push overshoot correction.** `_climb_exit_push()`
  (`wingman/controller.py`) treated the flyable band as "anything at or
  below `target`" (`angle <= target`), which accepts a negative — actively
  diving — angle as a successful, in-band handoff. Live: two nose-down
  pulses swung the aircraft from a steep climb straight through level into
  a genuine -38° dive, and the push released control right as the aircraft
  was diving. Fixed: the flyable band is `[0, target]`; a `pitch_key`
  starts at `NOSE_DOWN_KEY` and switches permanently to `NOSE_UP_KEY` the
  first time it observes `angle < 0.0`, logged at WARNING.
- **D5b — exit-push-vs-restart fight.** If a fresh climb emergency
  (`self._climb_emergency_requested`) is still true when a hold completes,
  the tree is about to immediately re-select `Climb` and start a new hold
  pushing `NOSE_UP` — the unconditional exit push was fighting that
  brand-new hold's own nose-up in the same tick. Fixed: `_run_climb_hold`'s
  `finally` block now skips the exit push entirely (logged at INFO) when an
  emergency is still pending. Live: crash at 14:36:02, nose oscillating
  ±90° until impact.
- **D5c — blind-pulse observe gap.** ADR 137 D9's zero-gap emergency pulse
  cadence (re-evaluate immediately rather than waiting `pulse_observe_s`)
  assumed a real angle reading is available to re-evaluate against. The
  floor now routinely forces an emergency climb in the same window a fresh
  respawn's telemetry handoff hasn't produced an angle sample yet, so the
  hold pulsed `NOSE_UP` blind, back to back, with no gap — and by the time
  real telemetry arrived the aircraft was already at the pitch ceiling.
  Fixed: the zero-gap rule now only applies once `last_angle` is no longer
  `None`; while still blind it falls back to the ordinary
  `pulse_observe_s` gap, same as the non-emergency case. Live: crash at
  16:15:56, same ±90° oscillation signature as D5a/b.

**D6. Climb's actuator must also use the hard/broad split, not just
`BoundaryTurn`'s yield.** D2 split `hard_emergency_active` (ttg or terrain)
from the broad `emergency_active` (also includes the floor) so `BoundaryTurn`
would not be locked out by a floor-only emergency. `_start_climb` and
`_update_climb` (`wingman/tick_handlers.py`) still read the broad signal to
decide `_run_climb_hold`'s actuation mode — airbrake held, afterburner
suppressed, zero-gap pulsing — which is the correct response to "recovering
from a fast, dangerous dive" (ADR 137's original case, where braking a real
dive is the point) but starves an ordinary floor-triggered climb of thrust
instead.

First live session after D1 shipped found this immediately (see "First live
trial" below): a fresh respawn well below the floor, with no dive or terrain
hazard in progress, still got the airbrake/no-afterburner treatment purely
because the floor is part of the broad signal. Pitching to the ceiling with
no thrust bled off speed, and the aircraft stalled and fell into a repeated
dive/recover cycle instead of climbing smoothly.

Fixed the same way D2 fixed `BoundaryTurn`: both functions now read
`climb_hard_emergency_fn` instead of `climb_emergency_fn`. A floor-only
climb still wins tree selection (that part was always correct — it goes
through `ClimbCondition.__call__`, which still ORs in the floor) and still
gets the sustain band's fuel-reserve-gated afterburner treatment, same as an
ordinary altitude-recovery climb. Only a genuine ttg or terrain hazard still
gets the airbrake/no-afterburner/zero-gap treatment.

**D7. `ClimbCondition`'s altitude-floor latch must freeze, not reset, on a
blind telemetry tick.** Found reading the log from the D6 incident above,
not separately reported: `wingman/behavior_tree.py`'s alt-floor block
re-derived `alt_floor = False` fresh on every call, so a single ordinary OCR
gap (`snapshot.altitude is None` — ADR 073's own "altitude is None FREEZES
the decision" case, which every other trigger in `ClimbCondition` already
honors) reset `_alt_floor_active` to `False` even while the aircraft was
still genuinely below the floor. The next real reading then saw a
FALSE→TRUE edge that was never really an edge, and re-logged the "BT:
ALTITUDE FLOOR" WARNING. Confirmed cosmetic only by direct trace —
`ClimbCondition.__call__`'s own selection latch (`_active`) does not read
`_alt_floor_active` and is unaffected — but it is the kind of noisy,
misleading log line the "first one has to be impossible to miss, the rest
must not flood the log" discipline (see `iterate` skill) exists to prevent.
Fixed: `_alt_floor_active` is now only ever written on a tick that actually
has an altitude to judge; a blind tick leaves it untouched.

### First live trial (2026-09-20, ~7 min session, first run of this batch)

Session `wingman.log` 06:35:31–06:42:12. Mission entered `GAME_BATTLE` at
06:37:15; the incident is the aircraft's second respawn, `GAME_BATTLE_EJECT
→ GAME_BATTLE` at 06:40:44. Measured directly from the log and
`test_screenshots/terrain_ahead/terrain_20260920_064104_6.png`:

| Time | Alt (m) | Speed (kph) | Nose | alt_rate |
|---|---|---|---|---|
| 06:40:51.3 | 1109 | 1079 | n/a | n/a — emergency escalates here (floor only, alt < 4000) |
| 06:40:54.3 | 1712 | 1008 | +59° climb | +239 m/s |
| 06:40:57.3 | 1120 | 716 | **-82° dive** | -197 m/s, ttg=7s |
| 06:41:00.3 | 1192 | 333 | +15° climb | +24 m/s |
| 06:41:06.3 | 1284 | 215 | -2° level | -2 m/s |
| 06:41:07.8 | — | 215 | — | STALL PREVENTION fires (D4) |
| 06:41:09.3 | 1026 | 505 | -38° dive | -86 m/s |
| 06:41:12.3 | **431** | 927 | -50° dive | **-198 m/s, ttg=5s** |
| 06:41:15.3 | 431 | 711 | +0° level | +0 m/s — pulled out |
| 06:41:18.3 | 823 | 388 | +90° climb | +131 m/s |
| 06:41:21.3 | 1034 | 206 | +90° climb | — stalling again |
| 06:41:22.8 | — | 206 | — | STALL PREVENTION fires again |
| 06:41:27.3 | 1490 | 485 | +45° climb | +95 m/s — finally clear |

Root cause confirmed by direct code trace, not inference: the emergency
escalation at 06:40:51.3 fired with `alt_rate=n/a` — no dive was in progress,
only the floor (1109 < 4000). Stall prevention (D4) fired correctly twice
(215 kph, 206 kph) and is not at fault — it is reactive by design and cannot
prevent the pitch hold from commanding excessive AoA in the first place.
Closest approach to the ground during the whole cycle: 431 m with 5 s to
impact, during the recovery dive from the first stall. No crash resulted —
the aircraft recovered on its own each time — but this is exactly the
oscillation signature D5a/b/c were written to eliminate, from a third,
previously-unseen cause.

## Consequences

- The aircraft should not observably descend below 4000 m during
  mission_j20 (barring extended blind telemetry gaps), matching ADR 081's
  doctrine in a second, independently-enforced place.
- A near-stall is caught even when neither existing emergency trigger can
  see it, including the case where `Climb`'s own posture caused it.
- `BoundaryTurn` keeps working through an extended floor-triggered climb
  instead of being locked out of the map edge for its whole duration.
- `Idle` can no longer strand the aircraft flying straight and level through
  a live climb emergency during the `GAME_BATTLE_EJECT` window.
- Five requester sites now share one AFTERBURNER/AIRBRAKE arbiter
  (`_may_hold_key`, ADR 139 D4) instead of four — no new inconsistency
  introduced, since the new case is unconditional like two of the existing
  four.
- Cost accepted: the very first pulse-to-pulse transition of *any* climb
  hold is always blind by construction (telemetry is fetched after the
  pulse decision each iteration), so D5c's fix is a small, one-time
  conservative gap on every hold's opening pulse, not just the
  respawn-adjacent case it was written for.

## Validation

- `tests/test_stall_prevention.py` (new, 11 tests) — engage/recover
  asymmetry, confirm-reads debounce, manual-takeover block, the exact live
  27 KPH shape, `GAME_BATTLE_EJECT` applicability.
- `tests/test_behavior_tree.py` — `TestAltitudeFloor` (D1),
  `TestHardEmergencyExcludesTheAltitudeFloor` (D2), missile-evade and idle
  `yields_to_fn` unit tests (D3) plus real-tree integration tests
  reproducing the "BoundaryTurn locked out of the map edge" and "Idle wins
  over a live climb emergency" incidents directly, and confirming both
  fixes leave the pre-existing ttg/terrain yields intact.
- `tests/test_climb_mode.py` — overshoot-correction tests for
  `_climb_exit_push` (D5a), exit-push-skip-on-pending-emergency tests
  (D5b), and blind-vs-telemetry-informed pulse cadence tests (D5c).
- `tests/test_controller_config.py::test_mission_j20_altitude_doctrine_is_4000m_everywhere` —
  pins D1's config value against ADR 081's, directly targeting incident 6.
- `tests/test_tick_handlers.py::TestClimbEmergencyActuationUsesTheHardSignal` (D6, new,
  6 tests) — pins `_start_climb`/`_update_climb` to the hard signal, including a
  direct regression test for the exact broad-true/hard-false shape the live
  trial hit.
- Full gate (`make lint && make test`) green: 1663 passed, 2 skipped.
- **Not yet done**: a dedicated, quiet, long-duration live-validation
  session on `test2` for Phase 1 alone, deliberately before adding anything
  further — the lesson taken from the `test1` regression itself. D6 was
  found in the first ~7 minutes of the first attempt at exactly that
  session, which is itself the process working as intended — see the
  "First live trial" section above.

## Deferred, explicitly out of scope for this ADR

- The forward-terrain-occlusion OR-term stays shadow-only
  (`terrain_avoidance.shadow: true`) — its own false-positive-rate history
  (many discovered causes across the prior HLDD 001 Phase 1 work) makes it
  the least deterministic component of this system, and it is not part of
  what the operator asked to reintroduce here.
- Any change to `_may_hold_key`'s existing four requester cases (e.g. giving
  `"climb"` a manual-takeover check it lacks) — ADR 139 D4 already flags
  this as its own future decision, not folded into a consolidation.
