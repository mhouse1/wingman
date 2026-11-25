# ADR 204 — Skip the start climb when the level path is clear, and level the nose after one

| Status   | Date       | Wingman Version |
|----------|------------|-----------------|
| Draft    | 2026-11-25 | 1.9.5           |

## Decision

Operator, 2026-11-25: "improve the round start nose up behavior, currently it nose up to
avoid terrain but often times its not necessary. ideally we avoid nose up if possible, if not
possible it should compensate to lower nose to 0 to -15 degrees to course correct for
overshoot".

- **D1 — skip.** In a `skip` round, an acs life that sees no target in its ADR 191 watch
  asks the terrain service whether the path flown *level* from the last fix stays
  `clearance_margin_m` (150 m) over the ground for `start_level_horizon_s` (8 s), on the track
  and 15 deg either side. Clear: no climb, straight to the pursuit. Not clear: the climb, as
  before. No fix yet: it flies level up to `start_terrain_wait_s` (2 s) more, still looking for
  a target, with ordinary climbs held off as in the watch; no verdict by then means the climb.
- **D2 — the arm is drawn per round**, `acs_mission.start_skip_share: 0.5`, tagged
  `start_climb` (`skip` or `climb`) in the round record. A `climb` round climbs exactly as
  before and logs the verdict, so the two arms compare on the lives where the skip would apply.
  ADR 191 D7 is the reason for the control arm: flying lives level raised terrain deaths there.
- **D3 — level the nose.** A start climb that a target in view cut short brings the flight
  path into `recover_nose_max_deg`..`recover_nose_min_deg` (0 to -15 deg) before the pursuit:
  pulses of `angle_pulse_s`, on new path samples only, for at most `recover_nose_max_s` (6 s).
  Ordinary climbs, the 3000 m floor among them, are held off meanwhile; a hard terrain or
  time-to-ground emergency is not. A climb that reaches 3000 m keeps its -10 deg step.

Schema defaults leave both off (`start_skip_share: 0`, `recover_nose_max_s: 0`);
`wingman/config.yaml` turns them on.

## Consequences

- A skipped start does not mean the nose stays level. The pursuit's own no-target search
  climb (`pursuit_mode.search_climb_alt_m` 7000, at most `search_climb_max_deg` 30) still
  flies. That is a 30 deg climb, where the script's climb flew the tree's 80 deg limit with
  1.5 s pulses.
- Half the battle entries never get a fix: on 255 of 456 (2026-11-23 to 25) no `TERRAIN:` fix
  came in the round at all (maps without data, `weak-match`). Those lives climb, as before.
- On a map that never fixes, the `skip` arm only adds start_terrain_wait_s of level flight
  before the same climb (5 s since the amendment below). Seen live 2026-11-25 05:32, ICEBOUND (`fix=none(weak-match)` on
  every read): `skip arm, climbing: the level path is unknown (no fix with a ground speed,
  +4.0s)`. If the first rows show many of these, skip the wait when the round has had no fix.
- D3 delays the chase of the target that ended the climb by up to 6 s.
- Respawn lives are out of scope. The spawn guard's nose-up pulses (ADR 076/078, kept by
  ADR 191 D7) put those lives at +90 deg before the decision, and D1 does not undo that.

## Evidence (measured, twelve sessions, 2026-11-23 19:49 to 2026-11-25 01:32)

Counted from the logs (analysis scripts in the session scratchpad, not kept), 1,343 acs life
decisions:

| | lives | crash (no kill card) within 20 s | within 40 s |
|---|---|---|---|
| battle entry, climbed | 389 | 0 | 7 (1.8%) |
| battle entry, target at the start, no climb | 57 | 0 | 1 (1.8%) |
| respawn, climbed | 269 | 1 (0.4%) | 4 (1.5%) |
| respawn, target at the start, no climb | 627 | 1 (0.2%) | 22 (3.5%) |

- Battle-entry climbs took the nose (`Nose:`, the flight-path angle) to a median +60 deg in
  their first 25 s; 75 of 232 reached +80 deg or more.
- Of 491 climbs in flight, 459 (94%) were cut short by a target and handed to the pursuit
  with no nose step, at a median +43 deg (80 at +80 deg or more). 32 reached 3000 m and ran the
  -10 deg step. In the 12 s after a hand-off the path fell to -30 deg or below 84 times in
  455, and to -60 deg or below 45 times.
- At the 2 s decision, 182 of 389 battle-entry climbs had a fix reading `ttc_y=clear
  ttc_m=clear` and 189 had none yet. Those ttc fields look 3 s ahead (`horizon_s`), too short
  to stand in for a climb, so D1 adds its own 8 s level projection.
- 167 further "climbs" began on the end-of-round transition (`EJECT → BATTLE`, then
  `GAME_END_B` within seconds). Those were not flight, and the table leaves them out.

Example, 2026-11-25 00:13:08, Stone Forest, battle entry: 1434 m, ground 1237 m under the fix
then 770 m, `ttc_y=clear ttc_m=clear`. The script climbed ("terrain may be hiding it"), and the
nose went +19, +30, +42, +64 deg in 6 s.

## Validation

Recorded in the rows below, per round by `start_climb` tag:

1. The `skip` arm's skipped lives (`START CLIMB: skip arm, no climb`) crash within 40 s no
   more often than the `climb` arm's lives whose verdict was clear (`climb arm, climbing; the
   level path is clear`). Hundreds of lives are needed before a difference means anything.
2. Score per round (`round-scores.py`) by `start_climb` arm, randomized within sessions.
3. D3: the path after a hand-off (`climb cut short by a target`) stays above -30 deg more
   often than the 371 of 455 before it.

| Session | Code | Game UI | skip rounds | climb rounds | skipped lives | crash 40 s skip vs climb-clear | D3 hand-offs, min path |
|---|---|---|---|---|---|---|---|
| 2026-11-25 05:22 to 06:10, ADR 202 relaunch (pid 2039011); ended by the operator's `z` in GAME_STARTING (display :3, injected=False) | working tree on 6da1434, wait 2 s | not checked | 4 (rounds 1, 2, 5, 6) | 3 (rounds 3, 4, 7) | 0: every verdict `unknown` (ICEBOUND never fixes; fixes stale or not yet speeded at the decision) | — | 6 in flight: none confirmed in 6 s; round starts reached the band 2 to 3 s after the hand-off (lowest -19), both vertical respawns did not (+51, +61 at the chase); 2 already in band, no correction |

First live D3 (05:32:14, ICEBOUND): cut short at +90 deg (+55 on the first sample), four
0.6 s pulses took the path +55, +53, +46, +35, +24 in 6 s (about 8 deg a pulse), the step
timed out to the pursuit at +24, and the path went on to +10, -2, -10, -11, -12, -16 over
the next 9 s. One life: no tuning on it.

Second (05:38:40, COUNTDOWN, `climb` round): cut short at +72 deg (+45 on the first sample),
pulses took it +45, +39, +24, +12, 0 in 6 s (about 11 deg a pulse); the 0 sample landed
0.08 s before the bound, which ended the step, and the chase then read -11, -17. Two lives:
6 s at 0.6 s pulses is at the edge of reaching the band, still no tuning.

Third (05:39:26, respawn, `climb` round, the fix 15.2 s old so `unknown`): cut 1.4 s into
the climb at +18 deg; the path still rose under the pulses, +20, +35, then +29, +20 at the
bound, and the chase read +10, -3. Three of three timed out short of the band and reached it
2 to 3 s after the hand-off, none under -17 deg: consistent with a path reading about 3 s
late (inferred), not yet with pulses too weak.

Fourth (05:42:06, respawn, `climb` round, fix 17.0 s old): cut short at +50 deg 3 s into the
climb; under the pulses the path read +71, +90, +90, +70 (90 is the reading's ceiling), the
chase took it at +51 and it read +41, +32, +7 over the next 4.5 s. The first sign that 0.6 s
pulses cannot take a vertical climb back within 6 s. Two of the four (both respawns) gained
least; if the next rows repeat it on respawns, lengthen the pulse there rather than the bound.

Not a flight (05:43:21 to 05:43:39): the script ran on the end-of-round screen (no HUD
reading from 05:43:20, `GAME_END_B` at 05:43:42), a "target" ended the climb, and the step
logged `nose at n/a`, pressed nothing for 6 s, and timed out. Count D3 rows only where the
step saw a path sample.

05:46:12, round start: the nose was -1 deg when a target cut the climb; no correction, the
chase took it at once (the in-band path works live).

Sixth (05:50:27, respawn, fix 18.2 s old): +90, +90, +90, +40, +90 through the step, chase at
+61, still +57 3 s later. Two of two near-vertical respawn climbs: 0.6 s pulses do not move
them, where round-start climbs came down 30 to 45 deg in the same 6 s. Next change, once the
operator agrees: a longer pulse (1.2 s, a named guess) while the path reads 60 deg or more.

**Amendment (05:55): `start_terrain_wait_s` 2 to 5 s.** Every D1 decision so far (2 skip, 4
climb rounds) read `unknown`. Round 5 (05:53, COUNTDOWN, `skip`) decided at +4.1 s with the
newest fix 155.5 s old; the first fix of the life came at +5.1 s and the first with a ground
speed at +6.6 s (ground 12 m, `ttc` clear: it would have read clear). Offline the first
battle-entry fix lands at a median 2.4 s (p75 4.1, p90 5.6) and the speed one fix later, so
2 s ran out before the evidence could exist. The new deadline is 7 s after the watch starts.
It reaches the running session at the next ADR 202 relaunch (after round 30, about 3 h
after 05:22). Respawns read fixes 15 to 21 s old for the same reason.
