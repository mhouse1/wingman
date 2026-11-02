# ADR 171 — The Jet Picks the Mission, and J20 Comes Back from a Rearm

| Status | Date       | Wingman Version |
|--------|------------|-----------------|
| Draft  | 2026-11-01 | 1.9.4           |

Operator, 2026-11-01: "integrate J20 mission into ACS, the next phase in ACS will be integrating
fighter jet detection, when J20 is detected it runs mission J20, in the future we'll add more custom
missions for specific jets i.e. activating special ability depending on which jet is flying. in the
new j20 mission it flies normal mission but after missiles are exausted it descends and resupplies
then climbs back to higher altitude and resume search and destroy". After the first live rounds:
"currently when it switches to secondary it flies down too fast and crashes, it needs to descend
altitude level out and attack using secondary weapons and boresight, effectivly using normal acs
mode". And, on what it does before that: "j20 flies out of the arena due to padlock camera changing
views where normally itd fly towards the icons and targets, this can be improved by when padlock
turns off steer towards targets and if padlock is on steering continues existing trajectory".

This is [Design 011](../hldd/011-acs-mode-hldd.md)'s jet detection phase, and it answers that
design's first open question. It builds on [ADR 167](167-read-each-rounds-jet-from-the-lobby.md) and
[ADR 168](168-read-the-jet-again-through-the-wait-for-a-match.md) for the jet,
[ADR 152](152-pursuit-resupply-priority.md) for the resupply and
[ADR 075](075-afterburner-fuel-perception-and-fully-adaptive-j20-mission.md) for the J-20's doctrine.
The spec is [`docs/missions/j20.md`](../missions/j20.md).

## Decision

**D1. A jet's profile can name its mission.** `jet_profile.profiles.<x>` gains `match` and `mission`.
At launch, the jet on record is compared with each `match`, and the first it starts with names the
mission. Any other jet, and a round whose jet was not read, flies `mission.default_mission`, still
`acs`. A jet with a custom mission later needs a profile and nothing else in the launch path.

- It is one shared launch path, `_start_default_mission`, so battle entry, `u` and the no-prior-mission
  restart cannot disagree.
- The record is the one the round is tagged from (`stats_tracker.jet`), so the two cannot disagree.
- `match` is the start of the name, letters and digits only, as `same_jet` compares. A profile names
  the mark because the rest of the label is unproven (see Consequences).
- A respawn restarts the last mission, as before: a jet changes only in the lobby, and `o` puts a
  J-20 on acs by hand.

**D2. mission_j20 goes round.** Its doctrine is unchanged until the primaries run out. Then:

1. **Descend and level off, at the switch to the secondary.** The pursuit comes down to
   `acs_mission.climb_alt_m` (3000) at `descent_angle_deg` (-20), wings level, with the airbrake
   holding it near `approach_brake.cap_kph`, and levels at `acs_mission.nose_angle_deg` (-10). One
   pulse per new altitude sample. Nothing else steers until it is level: a target or an objective's
   marker below the nose is what ran it away. Firing carries on. A recovery or an evade outranks it,
   and nothing the chase can see holds the crash recovery off or ends it. Nothing pulls up under
   `descent_min_pull_kph` (450). Bounded by `descent_max_s` (90).
2. **Attack as ACS does.** From there it is the ACS pursuit, unchanged: secondary weapon, boresight,
   tracker, icons, and the dive limit under 3000 m.
3. **Resupply.** With every rack empty, the existing search, marker approach, pass detection and 3 s
   climb-out, unchanged. If the pursuit's search climb has taken it back up, it descends again first.
4. **End the pursuit at the confirmed rearm**, once the climb-out is over. The nose-up is what pulls
   the aircraft off the terrain, so the hand-back waits for it.
5. **Climb back.** `mission_j20(climb_back=True)` takes the lock in `GAME_BATTLE` and the tree climbs,
   as after a respawn: an armed aircraft under its floor with a mission running. The mission writes
   no pitch.
6. **Resume.** It puts the primary back if the rearm left the secondary selected, holds the
   search-and-destroy loops off until `resume_alt_m` (4000), then starts them. The wait is bounded by
   `climb_back_max_s` (90). The turn guard is not armed: that heading is not a spawn's.

**D2b. The J-20 has a ceiling.** While `mission_j20` runs, above `ceiling_alt_m` (5500) with the path
still above `ceiling_path_deg` (5), the mission ends a routine climb and holds the nose down until it
stops rising. A hard emergency is never ended. Nothing is pressed while a climb, the spawn guard, an
evade or a boundary turn owns the axis, or on a stale reading. This lives in the mission and not in
the climb, which is shared.

**D2c. The rings steer only with the padlock confirmed off.** With the padlock in use, unless
`padlock_state()` is False, `_actuate_engage` commands nothing and feeds the navigator nothing, so the
aircraft holds its trajectory. That covers Engage, Regroup and the steering during a climb. Unknown
counts as on, because no detector can say on (ADR 140). acs never presses the padlock and is not gated.

**D3. The descent uses acs's own numbers.** "Effectively using normal acs mode" is one source, so a
change to acs's level-off moves the J-20's with it.

**D4. Search and destroy stays off through the climb back,** because the spec's order is climb, then
resume. `resume_alt_m: 0` starts the loops at once, if the unarmed climb proves too costly.

**D5. A second per-mission exception in shared code.** Default 8 of
[`docs/missions/README.md`](../missions/README.md) names this as the operator's choice, and this
request makes it. `pursue_and_engage` asks `_j20_cycle_in_play()` once, at its start: true only with
`j20_mission.resupply_return` on and j20 as the mission in play. Under any other mission the pursuit
has no new behavior. `resupply_return: false` restores J20's old pursuit too.

## Consequences

- **The first design was wrong.** It put the descent at every rack empty, from logs with no J-20 in
  them. No J-20 got that far: all four live pursuits were run away at the switch to the secondary,
  before it began. See the trial record.
- **The aircraft flies wings level and does not chase for about 40 to 60 s** from its usual height.
  A target in view is passed by. That is the price of not arriving at 2,000 KPH; V4 records it.
- **Above 3000 m a chase is still unlimited.** The pursuit's search climb goes to 7000 m, and a
  target sighted from there is chased as before; the dive limit is `dive_below_m` for every mission.
  Only an empty rack gets a second descent. Watch for it.
- **An unread J-20 loses the cycle and nothing else.** It flies acs for that round.
- **No missiles on the way up.** The aircraft climbs from about 1400 m to 4000 m unarmed in effect.
  The time is not measured; V5 records it.
- **A rearm the HUD cannot show stays a pursuit.** A resupply taken with a full secondary selected
  changes no count (2026-10-05, 11:18:45), so no rearm confirms and the pursuit carries on as it does
  today. Nothing is lost but the return.
- **A round reached without `GAME_STARTING`** (Good Luck missed, via `GAME_UNKNOWN`) restarts the
  previous round's mission, as it always has, so a new jet can fly the old one for that round.
- **`has_padlock` and `jet_profile.active` are untouched.** They are still read once at startup, and
  nothing branches on them. The detected jet does not feed them.
- **Whether this scores better is a separate question.** A round's score has a standard deviation of
  about 200, so only randomized arms within a session can say. `resupply_return` is the switch for
  such a trial; none is drawn here.
- **Implementation.** `wingman/jet_name.py` (`mission_for_jet`), `wingman/resupply.py`
  (`ControlledDescent`), `wingman/controller.py`, `wingman/main.py` (`set_jet_source`).

## Why

Measured on the 98 session logs of 2026-10-23 to 11-01 and the two J-20 sessions of 2026-11-01:

| Pursuit began at | Pursuits | Passed -300 m/s | Median fastest descent |
|---|---:|---:|---:|
| under 2000 m | 3,959 | 8% | -94 m/s |
| 2000 to 3500 m | 1,422 | 8% | -124 m/s |
| 3500 to 5000 m | 28 | 39% | -254 m/s |
| J-20, 5000 to 7075 m | 4 | 3 of 4 | -572 m/s |

- **Why it dived.** At that height nothing limits a chase. `dive_below_m` is 3000, so the dive brake
  does not act; `dive_safety` is off, so no guard does; and the crash recovery waits while the
  pursuit has its target in view (operator, 2026-10-08). So the J-20 followed its target down on
  the afterburner. The nose comes up 21 deg/s at 500 KPH and 3 to 5 above 1,000.
- **Why not just raise the brake's limit.** `dive_below_m` is shared, and a limit alone leaves a
  chase at 6000 m, which no mission has flown. The spec asks for the ACS pursuit, so it gets it
  where ACS flies it.
- **Why a ceiling.** Where the descent starts decides how it goes (table below). The climb lets the
  J-20 go late, as high as 7,011 m at +757 m/s, because two reads at that rate come about 4 s after
  the target. It coasts to 8,114 m, 9,395 m at most, and slows to 127 to 290 KPH at the top. Of the
  ten descents begun above 6,700 m, nine ended in a hard recovery and one was shot down in 4 s; six
  came within 14 s of a stall warning. The one begun at 5,227 m had no recovery. The warning itself is common (34 in one acs session), so the
  count proves nothing; where it comes does. A climb that let go on time at 5,012 m peaked at 5,577 m,
  which sets 5500.
- **Why the padlock gate.** The rings are read off a view the padlock camera turns from the nose. On
  the J-20 logs of 2026-11-01, 85 of 183 Engage rolls, and 19 of 48 in the next run, came with the
  camera unconfirmed, on about 60% of Engage ticks. The boundary read already waits for the same
  confirmation (ADR 142), and had no reading on 73% of ticks.
- **Why no pull while slow.** Slow, a steep path is a stalling wing and a pull adds drag. This is a
  guard and not the cause: the 17:59 fall was the hardest and had one UP pulse before its recovery.
- **Why -20 degrees.** `recover_below_time_s` is 30, so at 3000 m the descent must stay under 100 m/s
  or the tree's hard recovery takes the airframe. At the 850 KPH cap -20 deg is 81 m/s: 50 s from
  7000 m and 40 s from 6000 m. The angle is a named guess inside that limit.
- **Why wings level.** A nose-down pulse in a bank turns the aircraft and does not lower its path,
  and the search roll under a pushed nose is the spiral ADR 148 recovers from.
- **Why it keeps firing.** The weapon is not what crashed it, and ACS fires on the same cadence. Only
  the steering waits.
- **Why a marker waits too.** At 16:57:37 the crown's marker drew it down at 2,069 KPH.
- **Why the rearm is still low.** Rearms come at a median 1405 m over 1,057 climb-outs, and no search
  has begun above 3331 m, so the search starts from 3000 m, inside what has flown.
- **Why the tree flies the climb.** It already does, from the same height, after every respawn, and
  the J-20's doctrine is that it owns the climb. A second writer on the pitch axis is a bug.

| Descent | Began at | Stall warning before | UP pulses before the recovery | Fastest fall | Hard recovery |
|---|---:|---:|---:|---:|---:|
| 17:37:56 | 8,891 m | 278 KPH, 4 s | 15 | -251 m/s | +35 s |
| 17:42:07 | 5,227 m | none | 0 | -94 m/s | none |
| 17:49:33 | 7,054 m | 239 KPH, 2 s | 9 | -253 m/s | +15 s |
| 17:55:24 | 6,728 m | none, 278 KPH at +29 s | 11 | -237 m/s | +35 s |
| 17:57:11 | 7,759 m | 273 KPH, 7 s | 8 | -257 m/s | +11 s |
| 17:59:01 | 7,555 m | 180 KPH, 0 s | 1 | -401 m/s | +5 s |
| 18:01:49 | 7,524 m | 290 KPH, 2 s | 7 | -319 m/s | +15 s |
| 18:03:24 | 8,674 m | none | 19 | -240 m/s | +48 s |
| 18:08:01 | 7,252 m | 127 KPH, 14 s | 8 | -231 m/s | +31 s |
| 18:11:31 | 7,350 m | none | 0 | none | killed by an AMRAAM at +4 s |
| 18:12:14 | 8,571 m | none | 10 | -224 m/s | +27 s, level at 2,905 m at +47 s |

**The altitude floor does not block the descent.** J20 flies under the tree's 4000 m floor, unlike acs
([ADR 147](147-mission-su30-flies-its-own-altitude-doctrine.md)). Inside a pursuit the floor is not
enforced: `_start_climb` passes only the hard verdict as `emergency`, and `climb_mode` refuses a
non-emergency climb while the pursuit owns the airframe. acs shows this every life, rearming at a
median 1405 m under its 3000 m floor. The time-to-ground recovery still acts, and the descent yields
to it like any other pursuit steering.

## Validation

Log lines for a live trial, in order. Each is emitted by the code and asserted in
`tests/test_mission_j20_rearm.py` or `tests/test_jet_name.py`, except `JET NAME:` (ADR 167).

```text
JET NAME: J-20 MIGHTY DRAGON
Controller: jet J-20 MIGHTY DRAGON flies mission j20 (ADR 171)
Controller: mission_j20 - adaptive mission running (S&D loops up, behavior tree owns tactics)
MISSILES EMPTY — pursuing with secondary weapons, both axes (HLDD 015)
DESCENT: to 3000 m at -20 deg, wings level, then level at -10 deg (ADR 171)
DESCENT: over (level at 2xxx m), the ACS pursuit has the aircraft
RESUPPLY: missiles exhausted; maximum urgency reached; searching for resupply, targets ignored until rearm
RESUPPLY: rearm climb-out, nose up and afterburner for 3.0s (...)
RESUPPLY: rearmed, the mission takes the aircraft back after the climb-out (ADR 171)
PURSUIT SUMMARY: end=rearmed ...
Controller: pursue_and_engage — rearmed, handing the aircraft back to the mission (ADR 171)
Controller: mission_j20 - rearmed, climbing back to 4000 m before search and destroy resumes
Controller: mission_j20 - altitude 4xxx m at or above 4000 m
Controller: mission_j20 - adaptive mission running (S&D loops up, behavior tree owns tactics)
```

- **V1, the jet (met).** Live on 2026-11-01: `JET NAME: J-20 MIGHTY DRAGON` and `flies mission j20`.
- **V2, every other jet (met).** `flies mission acs` on the J-10 FIREBIRD, 2 of 2 rounds.
- **V3, the cycle.** The lines above, in order, on a live J-20.
- **V4, the descent.** No rate past -300 m/s and no `yielding pitch and roll to the dive recovery`
  between `DESCENT: to` and `DESCENT: over`, and no crash in that span. If the recovery fires, the
  angle is too steep. Record how long it took and whether a target went by.
- **V5, the climb back.** The time from the hand-back to `at or above 4000 m`, with no `not reached`
  warning. This is the cost of D4.
- **V6, the search.** The rearm rate against the 50 to 65% of searches begun under 3331 m.

`make test` covers the logic, including the whole cycle through the real mission, FSM and pursuit
loop on fake telemetry. No gate covers it live.

## Trial record

| When | Session | Code | Result |
|---|---|---|---|
| 2026-11-01 16:22 | operator's `make r1`, main prefix, J-20 | this ADR's first design, the descent at every rack empty | V1 and V2 met. Two J-20 pursuits, no descent reached. 16:25:50 from 6,554 m: killed at about 5,900 m by a Sparrow. 16:33:12 from 5,030 m, already diving: 2,384 KPH, recovery at 9 s, bottomed at 1,502 m, killed by guns 48 s in. |
| 2026-11-01 17:34 | operator's `make r1`, main prefix, J-20 | the descent at the switch to the secondary, this revision. Gate at 17:41: 3841 passed. | First descent, 17:37:56, from 8,890 m (9,287 m reached in search and destroy): fastest -251 m/s and top speed 1,284 KPH, against -791 and 2,978 at 16:55, and alive at match end, but V4 not met. The path was already about -69 deg at +2.8 s with one DOWN pulse sent, so the descent did not put it there (cause not known). 11 UP pulses took 16 s to bring it to -17 deg, where the emergency hold moved it 54 deg in 3.3 s, so a 0.6 s pulse is far weaker than a hold. 6 DOWN pulses from +18 s stacked on the lag, -7 to -37 deg, and the hard recovery fired at 4,420 m. The match ended 61 s in with the descent unfinished: it never levelled or attacked as ACS. Second descent, 17:42:07, from 5,242 m under incoming: fastest -94 m/s, top speed 1,286 KPH, no hard recovery, and the evade and the flare roll took the airframe as designed. Killed by a MICA IR 14 s in at about 5,000 m, unfinished again. V4 met on that one, on one sample. |
| 2026-11-01 17:49 | the same session | the same | Third descent, 17:49:33, from a level 7,054 m, 1.6 s after a 239 KPH stall warning: the path fell from -7 to -77 deg over 12 s through 9 UP pulses. So the pulses were not shown weak, as the row above reads: the aircraft was stalled. The recovery fired three times and each lasted 0.5 s, `objective_in_view`, a bug in this revision: during a yield the chase reported its target. Fixed, with the ceiling (D2b), both after this run. It still came back, and `DESCENT: over (level at 2684 m)` at +45 s, the first complete descent. 16 s into the ACS pursuit an icon pushed the nose down at 1,228 KPH with sky 0.01 ahead, and it hit terrain: no kill card, an ordinary ACS death. |
| 2026-11-01 19:01 | `make r1` launched by this cycle, main prefix, J-20 | adds the padlock gate (D2c) and the climb cut-off (D2b). Gate at 18:59: 3871 passed, `rr-path1-gate` passed. | Stopped by the operator with `z` at 19:08:19, after one round: score 200, K/D/A 1/3/0, too little to judge. The padlock gate worked as designed: no Engage roll with the camera unconfirmed, against 41 and 85 in the two runs before, 15 with it confirmed off, and 55 ticks held. The out-of-bounds banner showed on 1 of 282 ticks (0.4%), against 4.3% and 0.9% in those runs: the right way, on one round. All three deaths had kill cards, none a crash, and 2 of 4 descents had a hard recovery. The ceiling still did not hold: median peak 8,519 m over 4 lives (too few to compare, and not better). The cut-off fired on time, 5,666 m at 19:06:07, but the aircraft crossed 5,500 m at +574 m/s and coasted to 8,519 m. At that rate the climb has to end well below the ceiling, on the altitude predicted ahead and not the one read. |
| 2026-11-01 18:47 | the 18:29 run | as that row | A descent ran out its 90 s at 4,620 m, the first time-limit end. It was not slow: it reached 3,364 m by +40 s. Two hard recoveries threw it back up to 5,203 m, because cruise afterburner held the speed up and -150 m/s puts time to ground under 30 s while still at 4,300 m. The descent's rate and `recover_below_time_s` are in tension. Stopped by the operator with `z` at 18:51: 3 rounds, score 280 a round, K/D/A 4/10/3. |
| 2026-11-01 18:37 | the same run | the same | The ceiling did not hold: median peak 7,267 m over 4 lives, against 7,848 m before, and 8,810 m at 18:37. The climb was the zoom: the altitude reading froze at 3,006 m for 5 s, the hold pulled on to 8,100 m, 79 KPH 6 s later, and the ceiling stood aside for it until 8,366 m. Ending a routine climb at the ceiling (D2b) is from this. A fourth level-off, 18:34:42, began with a search climb and died at about 4,500 m with no kill card, so the `icon down` pattern is not established. |
| 2026-11-01 18:29 | `make r1` launched by this cycle, main prefix, J-20 | the ceiling, the recovery fix and the pull limit. Gate at 18:22: 3863 passed. | Running. First ceiling push at 18:32:06, 6,525 m at +35 deg, held 6 s, peak 7,020 m, no stall warning (a boundary turn had zoomed it to 7,173 m first, and the ceiling stands aside for those). First descent, 18:32:27 from 5,872 m: fastest -165 m/s, no recovery, level at 2,744 m at +39 s, V4 met. The icon law then pushed down at once and 8 s later it was gone, no kill card, the altitude reading frozen at 2,747 m. Of three level-offs so far, `icon down` came first twice and both crashed within 16 s; `icon up` came first once and it lived. Three is too few to act on. |
| 2026-11-01 18:13 | the same session | the same | Eleven descents so far. Recoveries inside them ended 7 times `crash_cleared`, 7 `objective_in_view` (the bug) and 2 `state_exit`. Two descents reached the level-off; none has yet flown a long ACS attack: pursuits of 59 to 70 s locked 0 to 17% of scans, most of it spent coming down. |
| 2026-11-01 17:55 | the same session | the same | Three more descents, in the table under Why. The theory of the 17:49 row, that UP pulses on a slow aircraft cause the fall, did not hold: 17:59:01 fell hardest with one. What holds is the height it began at. The ceiling (D2b) is the fix; the pull limit is a guard. Two of the three ended with no kill card. |
| 2026-11-01 16:52 | operator's `make r1`, main prefix, J-20 | the same | Two pursuits, no descent reached. 16:55:49 from 7,075 m: -791 m/s, 2,978 KPH, path -67 deg, pull-out predicted -9,688 m, recovery held off to 12 s, no kill card: a crash. 16:57:21 from 6,071 m: -637 m/s, 2,059 KPH, recovered at 2,238 m, dived again from 2,505 m to 1,175 m. The operator's report. |
