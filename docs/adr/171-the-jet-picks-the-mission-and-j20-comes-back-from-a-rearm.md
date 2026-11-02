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
mode". Then: "ideally the j20 should be flying right above the target and using padlock to fire
missiles". Then: "during secondary weapon it should descende to 4000 then operate like normal acs mode
until resupply which invokes climb and switcing to primary and reuming normal j20 mission". And, on
what it does before that: "j20 flies out of the arena due to padlock camera changing
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
   `descent_alt_m` (4000) at `descent_angle_deg` (-20), with the airbrake holding it near
   `approach_brake.cap_kph`, and levels at `acs_mission.nose_angle_deg` (-10). One pulse per new
   altitude sample, and none sooner than `descent_observe_s` (5) after the last. The descent has the pitch: nothing pulls the nose down until it is level, since a
   target or an objective's marker below it is what ran it away. The roll stays ACS's: at a target in
   view, else at the ring icons, else wings level. During an icon turn the icon has the pitch too,
   because a bank without its pull does not turn. An icon below the horizon, which ACS answers with a
   push and the wings level, gets the turn toward its side instead, as it does under the dive guard:
   the push is the descent's (`withheld=descent`). Firing carries on. A recovery or an evade outranks it,
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
still above `ceiling_path_deg` (5), the mission ends a routine climb and pulses the nose down, one
`ceiling_pulse_s` (0.4) per new sample, until it stops rising. It never holds the key. A hard emergency
is never ended. Nothing is pressed while a climb, the spawn guard, an
evade or a boundary turn owns the axis, or on a stale reading. This lives in the mission and not in
the climb, which is shared.

**D2d. The J-20's routine climb has limits of its own.** While `mission_j20` is in play, a routine
climb uses `climb_max_pitch_deg` (35), `climb_max_rate` (250) and `climb_pulse_s` (0.5) in place of the
configured 80, 450 and 1.5. Above either limit, the climb's own law pulses the nose down, and a
nose-down pulse grows with the overshoot: `climb_pulse_s` at the limit, one more per 15 deg past it, up
to the configured 1.5 s (`_down_pulse_s`). Under these limits a rate of exactly 0 straight after a
climbing read is a stalled altitude reading and is skipped like a misread, where the law used to pull
up on it. A hard
emergency keeps the configured values whatever the mission, because a pull-up from terrain must not
be capped at a cruise climb.

**D2e. The J-20's boundary turn lets its pull go at the pitch limit.** While a mission with climb
limits of its own is in play, the turn releases NOSE_UP once the path reaches the mission's pitch limit
(35) and takes it again 10 deg under it; the roll carries on. A limit that cannot be read leaves the
full pull, so the turn always flies. Every other mission keeps the full pull.

**Why D2b, D2d and D2e.** The doctrine the J-20 is meant to fly is the operator's: high, over the
target, firing with the padlock. Engage already steers at the contacts and orbits over them. What
broke it were excursions out of the band: the climb, the recovery and the boundary turn each threw the
aircraft vertical, it stalled at the top and fell. These three keep it in the band.

**D2c. The rings steer only with the padlock confirmed off.** With the padlock in use, unless
`padlock_state()` is False, `_actuate_engage` commands nothing and feeds the navigator nothing, so the
aircraft holds its trajectory. That covers Engage, Regroup and the steering during a climb. Unknown
counts as on, because no detector can say on (ADR 140). acs never presses the padlock and is not gated.

**D3. The descent ends at the J-20's own altitude and levels at acs's angle.** `descent_alt_m` is 4000
(operator, 2026-11-02). The first version ended at acs's `climb_alt_m` (3000) so that one number served
both; the operator asked for 4000. The level-off angle is still acs's `nose_angle_deg`.

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
- **Why limits on the climb.** The ceiling alone could not work. At 19:32:49 it ended a climb on time
  at 5,523 m, at +59 deg and +288 m/s. With the nose held down for 7 s the aircraft still coasted to
  7,111 m, lost its speed (260 KPH) and fell to terrain with missiles left. Once it is that steep it
  is too late. The climb's law already pulses down past its limits, and those limits were set on
  lesser jets. The code recorded the same failure on 2026-08-21: +73 deg, 1500 m more, 24 KPH.
- **Why the boundary turn.** It holds NOSE_UP with the roll. At 04:06:01 a 9.1 s turn swung the nose
  +0 to +90 deg and took the aircraft from 3,364 m to 5,413 m, at up to +414 m/s, and it was lost 5 s
  later; the ceiling stands aside for a turn, by design.
- **Why pulses and not a hold at the ceiling.** All four holds of the first version ran 2.5 to 7 s on
  the lagging path, and each ended in a fast fall, -269 to -435 m/s. The worst was 4.5 s from +8 deg
  at 20:12:12, then -58 deg at 1,614 KPH. The hold was causing dives.
- **Why a shorter climb pulse, sized to the overshoot.** The limits were overrun by the pulse itself:
  one 1.5 s pulse took this jet to +66 deg at 20:08:18, and three stacked to -24 deg at 20:06:55, which
  tripped the hard emergency at 2,100 m. Then 0.5 s was too little: after a respawn at 04:17:12 the
  spawn guard and two blind pulses had it at +67 deg, three 0.5 s pulses left it at +72, and it coasted
  to 6,511 m and 127 KPH. Neither fixed length works for this jet, so the length follows the error.
- **Why a zero rate is skipped.** "up, rate=0.0, angle=0.0" came in climbs at +300 m/s: the altitude
  reading had stalled, the law read "not climbing", and the pull went vertical. 20:07:11 and 04:22:52;
  the second reached the ceiling at +303 m/s and coasted to 8,563 m and 215 KPH.
- **Why not the cruise afterburner.** Tested and dropped: the fastest readings of the J-20 runs, up to
  2,587 KPH, came with cruise off, in steep dives. The speed comes from falling.
- **Why no pull while slow.** Slow, a steep path is a stalling wing and a pull adds drag. This is a
  guard and not the cause: the 17:59 fall was the hardest and had one UP pulse before its recovery.
- **Why -20 degrees.** `recover_below_time_s` is 30, so at 4000 m the descent must stay under 133 m/s
  or the tree's hard recovery takes the airframe. At the 850 KPH cap -20 deg is 81 m/s: 37 s from
  7000 m and 25 s from 6000 m. The angle is a named guess inside that limit.
- **Why a gap between descent pulses.** At 05:21:31 on 2026-11-02 the pursuit began at 2,035 KPH,
  climbing at +18 deg through 4,396 m. The descent sent five 0.6 s pulses in 6 s while the path read
  +17 to +24 deg throughout: the reading lags the nose by 6 s and more at that speed. The altitude
  reading then froze at 5,267 m for about 10 s, so neither the descent nor the hard recovery could
  act, and the next reading was 1,202 m. It levelled there and lived.
- **Why the roll stays ACS's.** The first version held the wings level for the whole descent. At
  2026-11-02 04:51:52 (the operator's `v` screenshot) that flew the aircraft straight out of the arena:
  the descent began at 0.35 of the radius from the edge and crossed it about 60 s later, while the icon
  law had an enemy on the ring on 208 ticks and was not allowed to act. Operator: "it should have been
  in normal acs mode where it uses the ring icons to steer towards target". Wings level remains only
  with nothing to steer at, because the search roll under a pushed nose is the spiral ADR 148 recovers
  from.
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
| 2026-11-02 05:50 | `make r1` launched by this cycle at the operator's word before the full gate finished, main prefix, J-20 | adds the gap between pulses (`descent_observe_s`, written 05:22) and the descent's icon turn (D2). The new tests passed alone; the full gate ran alongside. | To check, per descent: icon ticks with the wings level against 1,191 of 1,221, dive recoveries against 4 in 7 descents, length against 17 to 75 s, and no flight out of the arena in a descent. |
| 2026-11-02 05:18, result | the same run, stopped with `z` at 05:38, exited 05:43 | as below, without the gap between pulses (written 05:22, after the launch) | 4 rounds: score 531 a round, K/D/A 2.0/2.8/1.8 (within the noise). Seven descents, begun at 4,396, 6,608, 5,843, 6,332, 8,473, 7,337 and 7,530 m: the racks ran out above the band. Up to 19 DOWN pulses went before the path turned (none in the last, which began falling out of a 44 KPH stall), it fell at up to -262 m/s and lasted 17 to 75 s. Operator's screenshots at 05:27:35, 41 and 44, out of the arena: the 05:26:32 descent ran 75 s with two dive recoveries, and its icon law read the enemy at about 130 deg, below and left, as "down" on all 332 icon ticks, wings level each time; over the seven descents, 1,191 of 1,221 icon ticks held the wings level. The push was the descent's, so nothing turned. No objective marker or pin was in view during it (no PRIORITY or RESUPPLY line from 05:26:32 to 05:27:47). The refused push now turns toward the icon (D2). |
| 2026-11-02 05:18 | `make r1` launched by this cycle, main prefix, J-20 | adds the boundary turn's pull limit (D2e), the down pulse sized to the overshoot, the stalled-read rule (D2d), the descent's ACS roll (D2) and its 4000 m (D3). Gate at 05:17: 3895 passed; `rr-path1-gate` passed. | First descent fell from 5,267 m to 1,202 m on stacked pulses and a frozen altitude (Why); the gap between pulses is from this. The boundary turn let its pull go at +37, +61 and +48 deg in three turns, and a stall warning still followed at 05:23:32: the path reading lags, so the real angle at release is higher. To check: no flight out of the arena during a descent (the operator's 04:51:52 screenshot), `DESCENT: over (level at ~4000 m)`, the boundary turn's `pull let go` lines and no vertical turn, stall warnings per life against 0.27, and search-and-destroy time out of bounds against 31.5%. |
| 2026-11-02 04:01, result | the same run, stopped with `z` at 04:48, exited 04:54 | as below | 8 rounds: score 309 a round, K/D/A 1.3/2.3/1.3, the worst of the day's J-20 runs (within the noise). Out of bounds on 232 of 737 search-and-destroy ticks (31.5%), against 0.3 to 9.6% in the runs before; the banner detector does not run outside GAME_BATTLE, so the pursuit's and the descent's shares are not measured at all. Of those ticks Climb had 105, Engage 75 and BoundaryTurn 45, at a median 5,920 m, with the boundary read on 194. Inferred, not shown: a climb held to 35 deg covers about 6 km of ground where the vertical one covered little, and with the steering held while the padlock is unconfirmed it covers it in a straight line. Next to measure. |
| 2026-11-02 04:01 | `make r1` launched by this cycle, main prefix, J-20 | adds pulses in place of the ceiling's hold and `climb_pulse_s` (D2b, D2d). Gate: 3884 passed; `rr-path1-gate` passed. | Routine climbs now take 0.5 s pulses and read 7 to 26 deg early on; ceiling pushes came at +14 to +32 deg. The first life flew 37 s with no altitude read at all (alt=None throughout; OCR read 900 m at once after the respawn), so nothing could steer it. A boundary turn went vertical (D2e is from this). A correction to earlier rows: in nearly every death the altitude reading freezes 5 to 8 s before the respawn is seen, so the death is at the freeze, and several came at 4,000 to 5,400 m in level flight, where there is no terrain. So a death with no kill card is not a crash: the card is often not read. The no-card counts above say less than they were taken to. |
| 2026-11-01 20:05 to 11-02 03:55 | the same 20:05 run, overnight | as that row | Against the 19:28 baseline: lives above 6,500 m 3 of 11 (27%) against 14 of 17 (82%), median peak 6,077 m against 7,381, stall warnings 0.27 a life against 0.71, deaths with no kill card 5 of 8 against 11 of 14, deaths a round 1.6 against 2.3, score 552 a round over 5 against 426 over 6 (within the noise). Only 5 rounds flew: from 20:16 the account sat in a squad whose leader never queued, readied up and gave up 554 times (`accept_invite: true` in config.local.yaml), then stuck in GAME_UNKNOWN for 3.5 h from 00:27 after an INVITED popup. The liveness guard's hard limit fired at 00:42 and the session did not end until 03:55. Wingman grew 131 MB/h to 3.75 GB. None of these is this ADR's; recorded so the night is not read as the J-20's. |
| 2026-11-01 20:05 | `make r1` launched by this cycle, main prefix, J-20 | adds the climb limits (D2d) and the source of each `z`. Gate at 19:57: 3879 passed; `rr-path1-gate` passed. | The limits took hold: the first climb held 19 to 35 deg and 100 to 170 m/s for 12 s where the old law went vertical, and pushes came at +14 and +21 deg, against +59 to +90 before. One stall warning in the first 8 minutes, against 12 in 35. Two faults of this revision, fixed in the next: the ceiling's hold caused dives, and a 1.5 s pulse overran the limits (Why). A frozen altitude read gives `rate=0` and a false pull-up, 4 of 14 up pulses; not changed. After the level-off, the pursuit's search climb sets off for 7,000 m on held keys. |
| 2026-11-01 19:28 | `make r1` launched by this cycle, main prefix, J-20 | commit 906a437, the operator's, with the padlock gate and the cut-off, without the climb limits | First run with the cut-off firing on time (5,523 to 6,009 m), and the failure it left is measured: 19:32:49 in the Why section, `cause=terrain` at 19:33:23. A descent levelled at 2,772 m with 5 pulls withheld, then the ACS pursuit reached 2,407 KPH at 2,060 m and died with no kill card. D2d is from this run. Stopped with `z` in battle at 19:58:54 and exited at the lobby at 20:03. Baseline over 6 rounds and 14 lives: score 426 a round, K/D/A 2.0/2.3/1.0, median peak 7,779 m, 12 stall warnings, 11 of 14 deaths with no kill card, 0 Engage rolls with the camera unconfirmed, out-of-bounds banner 0.8% of ticks. |
| 2026-11-01 19:01 | `make r1` launched by this cycle, main prefix, J-20 | adds the padlock gate (D2c) and the climb cut-off (D2b). Gate at 18:59: 3871 passed, `rr-path1-gate` passed. | Stopped by the operator with `z` at 19:08:19, after one round: score 200, K/D/A 1/3/0, too little to judge. The padlock gate worked as designed: no Engage roll with the camera unconfirmed, against 41 and 85 in the two runs before, 15 with it confirmed off, and 55 ticks held. The out-of-bounds banner showed on 1 of 282 ticks (0.4%), against 4.3% and 0.9% in those runs: the right way, on one round. All three deaths had kill cards, none a crash, and 2 of 4 descents had a hard recovery. The ceiling still did not hold: median peak 8,519 m over 4 lives (too few to compare, and not better). The cut-off fired on time, 5,666 m at 19:06:07, but the aircraft crossed 5,500 m at +574 m/s and coasted to 8,519 m. At that rate the climb has to end well below the ceiling, on the altitude predicted ahead and not the one read. |
| 2026-11-01 18:47 | the 18:29 run | as that row | A descent ran out its 90 s at 4,620 m, the first time-limit end. It was not slow: it reached 3,364 m by +40 s. Two hard recoveries threw it back up to 5,203 m, because cruise afterburner held the speed up and -150 m/s puts time to ground under 30 s while still at 4,300 m. The descent's rate and `recover_below_time_s` are in tension. Stopped by the operator with `z` at 18:51: 3 rounds, score 280 a round, K/D/A 4/10/3. |
| 2026-11-01 18:37 | the same run | the same | The ceiling did not hold: median peak 7,267 m over 4 lives, against 7,848 m before, and 8,810 m at 18:37. The climb was the zoom: the altitude reading froze at 3,006 m for 5 s, the hold pulled on to 8,100 m, 79 KPH 6 s later, and the ceiling stood aside for it until 8,366 m. Ending a routine climb at the ceiling (D2b) is from this. A fourth level-off, 18:34:42, began with a search climb and died at about 4,500 m with no kill card, so the `icon down` pattern is not established. |
| 2026-11-01 18:29 | `make r1` launched by this cycle, main prefix, J-20 | the ceiling, the recovery fix and the pull limit. Gate at 18:22: 3863 passed. | Running. First ceiling push at 18:32:06, 6,525 m at +35 deg, held 6 s, peak 7,020 m, no stall warning (a boundary turn had zoomed it to 7,173 m first, and the ceiling stands aside for those). First descent, 18:32:27 from 5,872 m: fastest -165 m/s, no recovery, level at 2,744 m at +39 s, V4 met. The icon law then pushed down at once and 8 s later it was gone, no kill card, the altitude reading frozen at 2,747 m. Of three level-offs so far, `icon down` came first twice and both crashed within 16 s; `icon up` came first once and it lived. Three is too few to act on. |
| 2026-11-01 18:13 | the same session | the same | Eleven descents so far. Recoveries inside them ended 7 times `crash_cleared`, 7 `objective_in_view` (the bug) and 2 `state_exit`. Two descents reached the level-off; none has yet flown a long ACS attack: pursuits of 59 to 70 s locked 0 to 17% of scans, most of it spent coming down. |
| 2026-11-01 17:55 | the same session | the same | Three more descents, in the table under Why. The theory of the 17:49 row, that UP pulses on a slow aircraft cause the fall, did not hold: 17:59:01 fell hardest with one. What holds is the height it began at. The ceiling (D2b) is the fix; the pull limit is a guard. Two of the three ended with no kill card. |
| 2026-11-01 16:52 | operator's `make r1`, main prefix, J-20 | the same | Two pursuits, no descent reached. 16:55:49 from 7,075 m: -791 m/s, 2,978 KPH, path -67 deg, pull-out predicted -9,688 m, recovery held off to 12 s, no kill card: a crash. 16:57:21 from 6,071 m: -637 m/s, 2,059 KPH, recovered at 2,238 m, dived again from 2,505 m to 1,175 m. The operator's report. |
