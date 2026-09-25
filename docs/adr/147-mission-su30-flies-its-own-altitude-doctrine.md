# ADR 147 — mission_su30 Flies Its Own Altitude Doctrine: Floor at the Level-Off, No Sustain Climb

| Status | Date       | Wingman Version |
|--------|------------|-----------------|
| Draft  | 2026-09-24 | 1.8.11          |

## Context

The operator's report (2026-09-24): during a chase the aircraft keeps rotating at about 4000 m altitude, and it
should do that at 3000 m. `docs/missions/su30.md` says the same thing: reach 3000, set the nose to -10 degrees,
activate pursuit. ADR 144 had already recorded that the tree's altitude doctrine sits above that number
(its "specified altitude is below ADR 141's altitude floor" consequence and open question 2) and left the choice
to the operator. This ADR is the answer.

## Evidence (measured in the 18:57 to 19:18 session on 2026-09-24; inferred parts are marked)

**The tree restarts the climb the script just stopped.** First life:

| Time | Log line |
|------|----------|
| 18:59:27.358 | `mission_su30 - altitude 3192 m at or above 3000 m`, then `climb complete (stopped, 18.5s)` |
| 18:59:27.909 | `nose angle +58 deg, target -10: pulsing nose down` |
| 18:59:28.650 | `CLIMB - holding nose up + afterburner (target alt 5000, cap 90s)`, 1.3 s after the script stopped |
| 18:59:29.022 | `a climb hold owns the pitch axis, nose-angle step waiting` |
| 18:59:47.526 | `nose angle -10 deg not confirmed within 20s, continuing to pursuit` |

Pursuit began at 4750 m. The "not confirmed" warning ended the script on 24 of the 26 su30 hand-offs across the four
sessions of 2026-09-24 (6 of 6 in this one; `make sr` counts it). The cause is in code: 3192 m is under the hard floor
(`behavior_tree.climb.alt_floor_m`, 4000) and under the armed sustain band (`sustain` 4000 to 5000, which needs
missiles aboard and a mission running, both true during steps 1 to 3). The sustain band's target is 5000 m.

**The chase then settles on the floor.** In pursuit the mission has ended (`mission=False` in the `BT[active]`
lines), so only the floor acts, and the game state is `GAME_BATTLE_EJECT`, where a climb hold releases itself
(`climb - game state GAME_BATTLE_EJECT, releasing keys`). Measured: 136 climb holds in the session ended with
`state_exit`, 52 of them after 0.3 s, 33 after 1.3 s and 38 after 6.3 s (the ADR 086 exit push's three pulses), so a
floor climb in the chase is a short nose-up nudge, not a climb to its 5000 m target. Altitude over the long chases:

| Chase | Altitude while chasing | `Climb` selected |
|-------|------------------------|------------------|
| 19:04:22 to 19:06:59 (157 s) | 3870 to 4170 m from 19:04:32 to 19:05:44 | 68 of 105 ticks |
| 19:09:50 to 19:11:27 (98 s) | 3320 to 4560 m | 53 of 66 ticks |
| 19:15:10 to 19:17:56 (166 s) | 3970 to 4230 m for 100 s, floor climbs at 19:15:29, 19:16:08, 19:16:44, 19:17:11 | 48 of 111 ticks |

13 `ALTITUDE FLOOR` warnings in this session (31 across the four sessions, every one citing 4000 m). Median chase altitude
by `make sr`: 3923 m over 214 readings in this session (10th to 90th percentile 1674 to 4255 m; the low end is chase 1,
which dived and spent about 100 s at 1400 to 2200 m and 700 to 1100 KPH: the HUD frame archived at 19:00:46 reads
3060 m and 1044 KPH), and 3544, 4609 and 4365 m in the other three sessions. One misread is worth knowing: at 19:00:45
the telemetry read 35 m and 85 KPH (the HUD said about 3060 m) and the floor started an emergency airbrake climb from it. That the floor is what holds the chase near 4000 m is **inferred** from this pattern
(a slow sink, a floor trigger, a small lift); no trace separates it from other causes.

**The continuous rotation** is the pursuit's search default (roll with no target, `HOLD[roll]: ... left/search`),
unchanged by this ADR: it happens wherever the aircraft is, and the operator asked for where.

## Decision

`su30_mission.alt_floor_m` (new, shipped as 3000). While `mission_su30` is the mission in play:

1. the tree's hard altitude floor is that value instead of `behavior_tree.climb.alt_floor_m`;
2. the armed sustain band stands aside.

Everything else is untouched: the ttg dive recovery and the terrain-ahead trigger, every other mission (J20 and
JAS39 keep 4000 m and the sustain band), and the tree's floor for a mission-less aircraft. Removing the key
restores the previous doctrine for su30.

Mechanism: `ClimbCondition(alt_floor_override_fn=...)` reads the floor each tick and can only replace a floor that
exists (`alt_floor_m` unset is still no floor for everyone); `make_sustain_climb_condition(suppressed_fn=...)` still
evaluates the band so its hysteresis tracks altitude and withholds only the verdict; `build_tree` threads both;
`Controller.altitude_floor_override_m()` and `Controller.sustain_climb_suppressed()` answer from "the last launched
mission is su30" (the test `is_padlock_blocked` already uses), taking the lock with a timeout (no override for a
tick beats a stalled main loop); `BehaviorTreeHandler` wires them.

A config test pins the two relations that matter: the su30 floor is at or below `climb_alt_m` (above it the tree undoes
the level-off again) and below the tree's floor (otherwise it overrides nothing).

## Alternatives considered

- **Lower `behavior_tree.climb.alt_floor_m` for every mission.** Rejected: ADR 141 and ADR 081 d2 make 4000 m the armed
  doctrine, `test_mission_j20_altitude_doctrine_is_4000m_everywhere` pins it, and it would change J20 and JAS39 for a
  request about su30.
- **Raise `climb_alt_m` to 4000.** Rejected: the operator revised it from 4000 to 3000 on 2026-09-24 and the mission
  spec says 3000.
- **Override the floor only during pursuit.** Rejected: it leaves the prologue restart above (the climb to 5000 m and
  the chase starting at 4750 m) untouched.
- **Lower the floor and keep the sustain band.** Rejected by test: at 3192 m the band alone still selects Climb
  (`test_the_floor_alone_is_not_enough_the_sustain_band_must_stand_aside_too`).

## Consequences

- **Terrain margin for su30 is 1000 m smaller.** The floor is ADR 141's preventive backstop, and the dive-recovery and
  terrain-ahead triggers remain, but the operator has chosen a lower altitude for this mission and the margin is theirs
  to judge. Terrain height on the maps in play was not measured here.
- **The chase should now settle near 3000 m, not hold it.** Nothing steers altitude in the chase except the floor's
  nose-up pulses, so it will still sit on the floor and bounce. The share of ticks with `Climb` selected (43 to 80% in the
  long chases above) is expected to stay high at the new altitude.
- **A floor-triggered climb outside pursuit still aims at 5000 m.** `_start_climb` targets the sustain exit altitude
  whenever it is enabled, so if the aircraft sinks below 3000 m during steps 1 to 3 of a life (before the hand-off, in
  `GAME_BATTLE`, where the hold does not release itself) the tree starts a full climb toward 5000 m. Not observed in the
  logs above (the script's own climb led at every start, and the nose-down step does not reach 3000 m within its 20 s), so it
  was left alone. If a session shows a rise after step 3, give that climb the mission's level-off altitude as its target.
- The `-10` degree step can now take the pitch axis at all; whether it then holds is not established.
- ADR 141 stays Accepted and unchanged; this adds a per-mission exception. ADR 144 (Draft) is amended: open question 2
  answered.

## Verification

- Tests: 6 for the floor override, 3 for the sustain suppression, 5 replaying the 18:59:27 altitude (3192 m, armed,
  mission running) through the real tree and a real `AnalyzerSnapshot` (the tree's own doctrine selects Climb, as
  measured; the su30 doctrine does not; 2900 m still selects Climb; a mission switch takes effect next tick), 1 for the
  handler wiring to a real Controller, 4 for the Controller predicates (including a stuck lock), 2 for the shipped config.
  Mutation checks: ignoring the override fails 5 tests, dropping the suppression fails 4, dropping the handler wiring
  fails 1.
- Full gate: see the action item entry for this change.

## Live check 1 (2026-09-25 00:18 entry; the operator's session 00:03 to 00:08 on 2026-09-25, two lives)

Measured from the log:

- The floor is wired: `ALTITUDE FLOOR - 721m below 3000m` and 2 more, all citing 3000 m.
- Step 1 climbed to 3019 m and 3191 m and step 3 began there, as designed, and `mission_su30` handed over to pursuit both times.
- **The goal was not met.** The -10 degree step was "not confirmed within 20s" on both lives, and the chases began at about 4160 m and
  3730 m and climbed to 4555 m and 4583 m; nothing settled near 3000 m. `make sr`: pursuit altitude median 3771 m, nose angle not
  confirmed 2 of 2.
- **Two mechanisms, neither the floor and neither what this ADR changed:**
  1. *Life 1, a stale climb latch (real-function repro in the action item).* The floor emergency at 721 m latched the Climb condition; a
     33 s BoundaryTurn then outranked it, and the band's release check runs only when the condition is evaluated, so the latch
     survived. When BoundaryTurn ended at 3651 m the first evaluation returned True and `_start_climb` began a climb toward 5000 m
     (00:04:32.4, "a climb hold owns the pitch axis, nose-angle step waiting"). The same class of staleness Anomaly 007 fixed for the
     emergency verdict; the band hysteresis needs the same per-tick refresh.
  2. *Life 2, BoundaryTurn at the spawn.* The aircraft spawns 0.04 to 0.16 of the map radius from the edge; BoundaryTurn's "banking and
     pulling away" (cap 12 s) ran three times during step 3 and pulled the nose up against the script's nose-down pulses (+46, +32, +13
     degrees while the script asked for -10).
- **A larger finding this ADR did not address: both chases ended by flying into the ground** (action item 001, Cycle 16 live result). The
  3000 m floor gives a chase 1000 m less margin, and the recovery in a chase is a 0.3 s nudge (the climb hold releases on any game state
  other than `GAME_BATTLE`, and pursuit lives in `GAME_BATTLE_EJECT`). Whether the lower floor contributed is not separable from two
  lives; the mechanism does not depend on it.

## Not verified

- **Live behaviour beyond the two lives above.** Still to show: no `target alt 5000` climb after `step 3/4` (life 1 had one, from the
  stale latch), the -10 degree step confirmed, and the chase altitude centred near 3000 m.
- That the -10 degree step is held once the axis is free, and how the pursuit's pitch loop and the floor pulses share the
  axis below 3000 m (the two-writer case ADR 144 recorded).
- Terrain safety at 3000 m on every map.

## References

ADR 144 (mission_su30; open question 2), ADR 141 (altitude floor), ADR 075 (armed sustain climb), ADR 081 d2 (armed
doctrine), HLDD 015 (pursuit mode), `docs/missions/su30.md`,
`docs/action-item/001-target-tracking-lock-stability-and-false-positives.md`.
