# ADR 148 — A Dive Recovery Flies Through a Pursuit

| Status | Date       | Wingman Version |
|--------|------------|-----------------|
| Draft  | 2026-09-25 | 1.8.11          |

## Context

The operator, 2026-09-25, after watching the 00:03 session: "after it switched to secondary weapons it crashed to
the ground, it should have continued pursuit sequence."

The weapon switch was not the cause (measured below), but the observation is right: a chase that has found and killed
a target should not fly the aircraft into the ground. Both chases in that session ended that way.

## Evidence (measured from the 00:03 to 00:08 log and the archived frames; inferred parts are marked)

**The switch and the kill.** At 00:05:24.495 `pursue_and_engage - selected weapon empty (3 consecutive zero reads),
switching to the secondary`; the HUD frame archived at 00:05:26 (`pursuit_mode_20260925_000526_8.png`) reads
`+100 DESTROYED Gabagool` with the R-73 rack now selected, so the lock that ended at the switch was a kill, not a loss.
Nothing else started: no `eject_and_dive`, no fall-through, and the summary is `PURSUIT SUMMARY: end=external:respawn_detected
dur=54.5s`, so the pursuit loop ran until the death.

**The dive began 18 s before the switch.** Chasing that target down, the aircraft was at nose -22 degrees at 4372 m at 00:05:06
and reached 1159 KPH; the tree's `DIVE RECOVERY - 29s to ground (alt=3552m rate=-120m/s)` fired at 00:05:20.4, and altitudes
at 3 s steps around it were 3566 (00:05:15), 3205, 2840, 2429, 1974 and 1431 m. Respawn at 00:05:36. Life 2 (no lock, a search-roll spiral
after a near-stall, inferred from the attitude readings) did the same from 4550 m at 00:06:40 to 2 m at 00:07:07.

**The recovery was a nudge.** The session logged 24 `CLIMB - holding nose up + airbrake (EMERGENCY ...)` starts and 38 holds that
ended `climb complete (state_exit, ...)`, 33 of them after 0.3 s. Cause, in code: `_run_climb_hold` releases on any game state
other than `GAME_BATTLE` (the SAF-001 backstop: a climb must never keep flying an airframe the state machine says wingman no
longer owns), and a pursuit lives in `GAME_BATTLE_EJECT`, where `make_idle_condition` deliberately lets the emergency climb start.
So the hold started, released 0.25 s later, and the tree restarted it 1.5 s after that: a 20% duty cycle, while the chase kept
holding roll (search or target) and pitch on top. ADR 144 had recorded the two-writer half of this; the state exit makes it fatal.

## Decision

A **hard-emergency** climb hold (time to ground or terrain, not the altitude floor) flies through `GAME_BATTLE_EJECT` while a
pursuit is running, and the pursuit yields its axes to it.

1. In `_run_climb_hold` the state check keeps the hold flying when all of these hold: the state is `GAME_BATTLE_EJECT`,
   `_pursuing` is set, `pursuit_mode.recovery_max_s` is above 0, and the hold is (or has been, since it latches) an
   emergency. Any other state, the pursuit ending, an eject or evade pre-emption, and the operator's takeover
   (`GAME_BATTLE_MANUAL`, so SAF-001 stands) still release it. `recovery_since` latches, so an emergency that clears
   mid-recovery does not release a low aircraft back to a chase that dives; the hold then ends as any hold does (target
   altitude reached) or at `recovery_max_s` (`recovery_cap`, 30 s shipped; the tree starts a new one at once if the
   emergency is still on).
2. While that mode is on (`Controller.pursuit_recovery_active()`), `pursue_and_engage` writes neither pitch nor roll: it
   releases its sustained holds once (so the search roll ends and the wings can come level), skips steering, and resumes
   when the mode ends. Tracking, ammo handling and firing carry on. Two INFO lines mark the yield and the resume.
3. Instrumentation only: `HOLD[pitch]` DEBUG lines, mirroring `HOLD[roll]`. Pitch key holds were not logged at all, so pitch
   behaviour could not be read from a log.

Unchanged: the altitude floor's climb (a soft emergency) is still a 0.3 s nudge in a chase; `eject_and_dive` (a deliberate dive
to die) has no pursuit and keeps the backstop; `recovery_max_s: 0` restores the old behaviour exactly.

## Alternatives considered

- **Exempt every climb hold in a pursuit.** Rejected: the floor climb would then run to its 5000 m target every time the
  chase sank under it, and a telemetry misread would interrupt the chase for a full climb (the 18:57 log has 7 altitude reads of
  1 to 41 m in chases, not all necessarily wrong).
- **Release the hold when the emergency clears.** Rejected: the emergency clears as soon as the descent stops, which can be a
  few hundred metres up, and the chase would dive again into the same emergency.
- **Let the chase and the hold share the axes.** Rejected: both press NOSE_UP and NOSE_DOWN, and that is the situation that
  crashed both lives.
- **A preventive dive guard in the chase's pitch loop** (do not follow a target below some altitude). Not done: it is a policy
  about how low a chase may go, which is the operator's call, and the recovery already breaks the chase off at 30 s to ground.

## Consequences

- A hard emergency in a chase now costs the chase up to 30 s, and may repeat. That is the intent. Misread altitudes that
  produce a hard emergency will cost the same; their rate in chases is not measured beyond the 7 reads in one log.
- Recovery assumes releasing the roll key lets the wings come level, as `BoundaryTurn` assumes when it banks and pulls. Not
  verified for the game's flight model; if it is wrong a pull-up from an inverted attitude would pull down.
- The recovery hold's target is the sustain exit altitude (5000 m), above su30's 3000 m level-off; the chase resumes wherever
  the hold ends, which for the cap is about 30 s of climbing.
- ADR 141, ADR 137 and ADR 086 (Accepted) are unchanged; this adds an exception to the state exit their hold shares. SAF-001 is
  preserved: takeover, respawn and every state other than `GAME_BATTLE_EJECT` release the hold as before.

## Verification

- `tests/test_pursuit_recovery.py`, 19 tests with a real Controller, real hold thread and real `GameState`: the hold flies
  through a pursuit; releases with no pursuit, when soft, in any other state, on takeover, when the pursuit ends, at the cap,
  and with `recovery_max_s: 0`; latches through a cleared emergency; recovers when a soft hold escalates; clears its flag; the
  chase writes neither axis while it flies (roll and pitch keys released, firing continues), steers again afterwards, and
  logs both transitions; pitch holds are logged. Mutation checks: making the state check never exempt fails 8, making the chase
  never yield fails 4, never clearing the flag fails 1.
- Full gate: see the action item entry for this change.

## Live check 1 (2026-09-25 00:50 entry; the 00:40 run, one long chase)

Measured from the log. The chase began at 3597 m and dived at 814 to 1079 KPH; `DIVE RECOVERY - 25s to ground` fired at 00:41:52, the hold logged
`hard emergency inside a pursuit: flying through GAME_BATTLE_EJECT and the chase yields (ADR 148, cap 30s)`, and the chase logged `yielding pitch and
roll to the dive recovery`. The aircraft bottomed at 1928 m, climbed, and the chase resumed at 00:42:28 (the hold ran to its 30 s cap). Two more recoveries
followed (minima 2304 m and 2778 m). The life ended with the match after 232 s, against impacts after 54 s and about 85 s in the 00:03 session. Costs seen: a
pull-out through nose +90 degrees at 191 KPH with the airbrake held (near a stall), and the recoveries used roughly a third of the chase. All three dives followed a
locked chase, so the chase does follow targets down repeatedly; whether to prevent that is the policy choice this ADR left to the operator.

## Live check 2 (2026-09-25 01:38 entry; the operator's session 00:49 to 01:36)

Measured from the log. 16 recoveries started and 11 ran to the 30 s cap. Four ended in a death: two ground impacts after 1200+ KPH dives (00:59:04, 01:05:56) and two missile kills
while the aircraft was slow and climbing (01:20:24 at 331 KPH; 01:31:47 at 128 KPH, `DIED ARMED - cause=enemy_fire`); one ended another way. Lives ran to the match end more often than in the
00:03 session, but the recovery still loses lives.
The 01:31 case is the cost this ADR's Consequences anticipated (a misread altitude that produces a hard emergency costs a whole recovery), now measured. One accepted garbage read (314 m between real
reads of 3026 m and 3485 m) poisoned the smoothed altitude and rate for 5 s (`alt=2014.7 alt_rate=-906m/s ttg=2s`), the recovery cap's exit routine pulsed nose-up on it, and a second airbrake hold started
at 3668 m, 668 m above the su30 floor and climbing, with the chase yielding again; speed fell from 430 to 128 KPH in 15 s and a missile followed. The exemption in decision 1 has no height limit, so a
false emergency at altitude costs up to 30 s of flying with the airbrake and no afterburner. A height limit on the exemption is the narrowest fix and is proposed in the action item entry of 2026-09-25 01:38; it is
not built, and this ADR's decision is unchanged until the operator chooses.

## Not verified

- **Live, beyond one chase.** One long chase is one sample. Expected: `climb - hard emergency inside a pursuit`, a hold that lasts longer
  than 0.3 s, the `yielding pitch and roll` line, and no chase ending in an impact. `make sr` counts deaths.
- The level-wings assumption above, and whether a 30 s cap is enough from 1000+ KPH at 3000 m.

## Amendment (2026-09-26): switched off inside a pursuit, by the operator

After the first live sessions of HLDD 015's icon-directed pursuit, the operator: "the dive recovery should be
abandoned instead since its preventing pursuit of the direction indicated by icons. we should prioritize pursuit
via icons over dive recovery. dive recovery should be redesigned in the future where predictive physics indicate
at the planned trajectory we'll hit the ground this is not something to tackle right now." Asked which safety
mechanisms that covers, the operator chose, for the whole pursuit (icons and the tracker's locked chase): this
ADR's recovery flying through the pursuit, the review 018 dive guard (withheld nose-down and pull-out pulses),
and HLDD 015's -45 deg icon path limit; the no-fresh-angle rule for the icon's nose-down stays.

`pursuit_mode.dive_safety: false` (new key, shipped false; true restores the old behaviour) makes the pursuit's
dive guard return nothing, removes the flying-through from decision 1 (the hold never enters recovery mode
inside a pursuit), and stops an ADR 086 emergency climb from starting while a pursuit flies, so the chase is
the only writer of pitch and roll rather than meeting a 0.3 s nudge every tick. The altitude-floor climb
(ADR 147) was not in scope and is unchanged. Outside a pursuit nothing changes.

What the sessions of 2026-09-26 measured, for the redesign: this recovery flew 22-56% of the long pursuits,
and it did not prevent the terrain deaths recorded in HLDD 015, because the time to ground it and the dive
guard use counts from 0 m (the 05:51 death: 36 s to ground about 5 s before impact). A trajectory-based
prediction against the real terrain height is what the operator asks for next.

## Amendment (2026-10-02): the hard emergency flies through again, as crash recovery

Operator, after the 20:20:08 'v' screenshot (missiles spent, aircraft into a cliff instead of toward the
resupply beacon): "wingman needs to apply brakes and nose up if the altitude and speed change rate would result
into a ground crash".

**Decision.** New key `pursuit_mode.crash_recovery: true`. With `dive_safety` off, the tree's HARD emergency
(ADR 086 time to ground from altitude over descent rate; HLDD 001 terrain ahead only while
`climb.terrain_avoidance.shadow` is false, and it is true, so in practice time to ground alone; never the
altitude floor)
starts the ADR 137 emergency climb (airbrake, nose-up, no afterburner) inside a pursuit and flies decision 1's
recovery through it, so the chase, the resupply search and its look-down taps yield pitch and roll. The dive guard,
the -45 deg icon limit and the floor climb stay off. Unlike decision 1's latch, the recovery hands the chase back
as soon as the emergency has cleared and a fresh path angle is at or above level (log: `crash no longer
predicted`, exit `crash_cleared`); `recovery_max_s` still caps it. `false` restores the 2026-09-26 suppression.

**Why (measured, the 19:26-20:22 session).** The 20:20 death: the tree selected Climb with the emergency at
`ttg=30s` at 20:19:47 (HUD 2189 m), `20s` at 20:19:50 (HUD 1872 m) and `10s` at 20:19:59; every request was
refused (`dive recovery suppressed — the pursuit owns the airframe`, 203 such lines in the session). Missiles ran
out at 20:20:00.277, the resupply search tapped nose-down (`LOOKDOWN`, 20:20:00.430) and the HUD read 218 m,
1497 kph, -33 deg at 20:20:02.7. Across the session, 44 of 62 descents with under 20 s to ground were followed by
a respawn within 25 s (17:38 log 27 of 72, 19:09 log 15 of 30); 41 of the 47 deaths had one in the 20 s before.
From the 2026-09-12 to 09-28 logs, emergency recoveries entered at 1000 kph or more lost a median of about 600 m
before the path came level, about 7 s at the HUD's 3 s cadence (coarse; OCR misreads excluded only by the median).

**Consequences.** The chase loses the airframe for the length of each recovery; the 2026-09-26 cost (recovery in
22-56% of long pursuits) is the number to watch against deaths per mission. Still counted from 0 m, not terrain
height, as the amendment above notes; terrain ahead is the only part that sees a cliff. The tree's altitude is a
3-read mean that runs about 300 m above the HUD in a dive (1231.67 = mean of 1509, 1284 and 902 at 20:19:59), so
its time to ground is late by about one read; not changed here.

**Validation.** Tests in `tests/test_pursuit_recovery.py` (brakes and nose-up with `dive_safety` off; off
restores the suppression; the floor climb stays out; cleared but falling keeps flying; cleared and climbing hands
back). `make lint` clean, `make test` 2,607 passed, 35 skipped. Live: run started 20:41:01 (pid 1151595), code
on `auto-resupply` uncommitted over `d43dba8`, game UI unchanged from the 19:26 session. The measure is deaths
per mission and the share of `hard emergency inside a pursuit` episodes followed by a respawn within 25 s,
against 44 of 62.

**Live, first 4 minutes of the 20:41 run (measured).** Six recoveries from 20:42:15 to 20:44:32, each one
`yielding` then `steering resumes`, hand-backs at +5, +27, +14 and +33 deg (one `path n/a` on an aircraft already
dead at 1231 m, climbing, health 160 to 101, no incoming: not a dive death). Two dives were caught at 98 m and
74 m. New problem: every recovery ends slow (hand-back speeds 336, 415, 359, 278 and 242 kph; the stall-prevention
floor is 300 kph), because the airbrake stays held while the emergency lasts. Both hand-backs below 300 kph lost
the nose within 1.4 s while the chase held nose-up (278 kph at +14 deg to 629 kph at -19 deg and 98 m; 242 kph at
+33 deg to -8 deg), and the emergency re-fired 1.7 s later each time. Inferred, not shown: a stall at hand-back.
Candidate next change, not made mid-run: release the airbrake once the path is above level, or hand back only
above a speed.

The next recovery proved it fatal: from 305 m, 521 kph, -8 deg at 20:44:32 the hold reached 143 m, **45 kph**,
+24 deg at 20:44:40.8 with the airbrake still held (the tree's 3-read mean kept the emergency on), and health was
gone 0.35 s later.

**Whole run, crash recovery only (20:41:01 to the `z` at 21:05:52, measured):** 32 recoveries, 7 followed by a
respawn within 25 s; 9 deaths in about 24 minutes against 47 in 56 minutes in the 19:26 session (a different
denominator from the 44 of 62 above, so indicative only). Hand-back speeds: 16 of 23 below 300 kph (45 to 297).

**Change 2 (2026-10-02 ~21:00), airbrake on the path, not the verdict.** In `_run_climb_hold`, the airbrake is held
only while the emergency holds AND no fresh path angle is at or above level; at or above level it is released and
the ordinary fuel logic may light the burner (`climb — path +N deg, at or above level: airbrake released, thrust
allowed`); back below level it goes on again (`path below level again, airbrake on`). It applies to every emergency
climb, in or out of a pursuit. The emergency verdict and the hand-back rule are unchanged. Test:
`test_the_airbrake_comes_off_above_level_while_the_emergency_lasts`; `tests/test_actuator.py`'s source count of
airbrake-on sites, each paired with a throttle re-evaluation, went from 2 to 3. `make lint` clean, `make test`
2,608 passed, 35 skipped. The measure: hand-back speeds and recoveries followed by a respawn, against the above.

Three deaths during the finishing round (21:07:05, 21:08:30, 21:09:21, health OCR lost) count against "recoveries
followed by a respawn" but are not recovery failures: health went before the recovery started in two and 1.7 s
after it in the third, at HUD 514 m (-8 deg), about 1396 m (-16 deg, 1171 kph) and 1255 m (-17 deg, 1266 kph), no
incoming. Inferred, not shown: terrain at that height or gunfire; the time to ground counts from 0 m and cannot see
either. Terrain height is the open item, not the recovery.

**Change 2 live, interim (run 21:11:05, first 12 minutes, measured).** 15 recoveries, 2 followed by a respawn
within 25 s (first-fix run: 11 of 39); 2 deaths in about 12 minutes (0.17 per minute, against 0.41); the airbrake
came off mid-emergency 7 times (at +0 to +90 deg). Hand-backs below 300 kph: 8 of 14, median 283 (first fix: 19 of
29, median 267). Neither death was a stall or a dive into the ground: 21:15:47 a missile (incoming 1.5 s before)
at 961 m, +57 deg, 177 kph; 21:19:00 unclassified (HUD altitude 1 m for 6 s, speed 28 to 158 kph, health 28, then
an incoming). The release waits for the first fresh angle at or above level, one 3 s HUD read late, so a pull-up
from 1227 kph still bottomed at 175 kph (21:14:29-38) and releases at +90 deg mean the nose went through level
unseen. Twice the HUD read about 145 m at a steep dive and about 850 m 3 s later, which is not physical: assumed
a dropped leading digit. Next candidate if slow hand-backs persist: hand back only above a speed.

**Change 3 (2026-10-02 ~21:35): no angle is not below level.** At 21:32:29.7 the airbrake came off at +90 deg and
177 kph; the next read, 21:32:32.7, was `Nose: n/a` at 18 kph, and change 2 took the missing angle for "not above
level" and put the airbrake back on, logging `path below level again`. That aircraft recovered (379 kph, level,
1342 m) and died later, climbing at 1410 m with no incoming, so this did not cause a death, but the rule was wrong:
a fresh angle below level brakes, a missing one keeps the current state. Test:
`test_a_missing_angle_does_not_put_the_airbrake_back_on`. `make test` 2,609 passed, 35 skipped.

**Change 3 live (run 21:45:11, first 13 minutes, measured).** Five deaths, none a predicted descent the recovery
failed to stop: two gunfire (health 24 to 9 at 21:55:26-31, about 900 m and slow after a 21 s recovery; health 3
when the recovery began at 21:56:47.8); one inconclusive (21:50:37-43: 1646 m to 688 m in 3 s at -17 deg and
1134 kph, about 320 m/s against a 90 m/s sink, then -90 deg at 459 m); two in a dive at about 1300 m (21:48:13)
and 1467 m (21:57:35.7, health lost 0.3 s after the recovery began; 2078 to 1467 m in 3 s matches -36 deg at
1261 kph), no incoming. With 21:08:30 (about 1396 m) that is three deaths at 1300-1500 m in a dive with no
missile. Inferred, not shown: flying into terrain faces, as in the 20:20 screenshot's cliff, which time to ground
cannot predict whatever the altimeter measures. The re-brake worked as designed once (21:49:40-46: +2 deg at
418 kph, -25 deg at 503 kph, airbrake on, +15 deg at 452 kph, off).

Open, for the next cycle: terrain ahead (HLDD 001) against cliff faces; whether the HUD altitude is above ground
or above sea level (the 1646-to-688 m drop and two about-145-to-850 m jumps in 3 s fit either a misread or terrain
passing under the aircraft); and the hand-back speed.

**Terrain ahead is logged and not flown (measured, 21:58).** At 21:58:44.975, 1.5 s before the dive steepened to
-38 deg (death at 21:58:49, about 1000 m): `BT: TERRAIN AHEAD — sky fraction 0.00 below 0.55 threshold — climb
forced (HLDD 001 phase 1) [SHADOW - not actuating]`, while the icon held NOSE_DOWN toward a target below. Deaths
with a shadow TERRAIN AHEAD line in the 15 s before: run 21:45 so far 5 of 6 (all 5 with no incoming); 19:26
session 12 of 47 (9 with no incoming); runs 20:41 and 21:11 0 of 12 and 0 of 10 (inferred: maps without high
ground). `climb.terrain_avoidance.shadow` went back to true on 2026-09-17 by operator decision because the padlock
camera can turn the capture away from the nose; making it actuate again is the operator's call.

**Change 3 run, whole (21:45:11 to a lobby SIGTERM at 22:37:04, measured).** 52 minutes, 9 missions, 25
respawns. Of the 22 deaths classified by 22:30: 12 with an incoming missile in the 6 s before health went, 7 with
a shadow TERRAIN AHEAD in the 10 s before and no missile (one of them, 21:56:55, was gunfire at health 3), 3
other. Several missile kills were in slow steep climbs (218 kph at +55 deg, 588 at +90, 698 at +57, 435 at +69).
One TERRAIN AHEAD was a false positive: sky 0.00 at 3433 m climbing at +27 deg during a missile hit (22:16:20).

**Change 4 (2026-10-02 ~22:25, `wingman/telemetry.py`): one rejected read no longer blinds the descent rate.**
22:18:33.9 1523 m, 22:18:36.9 `14210` rejected by the ADR 097 ceiling, 22:18:39.9 1162 m accepted 6.0 s after
the last accepted read: past `stale_after_s` (6.0), so the stale-seed bypass cleared the history and 1162 carried
no rate (`alt_rate=n/a ttg=n/a`); the next read was 455 m at -45 deg and the emergency began at 22:18:43.2, health
gone 1.7 s later. The anchor that ADR 150 D4 already keeps for the digit-drop check (aged out only by rejections,
within `digit_drop_window_s`, 15) now also gates the next read, so the rate spans the gap (-60 m/s here; time to
ground about 24 s against the mean, under the 30 s threshold). A gap with no rejections still reseeds. Expected
gain is small (about one read earlier), since a 6 s rate understates an accelerating dive. Tests in
`tests/test_altitude_gate_adr150.py`; `make test` 2,611 passed, 35 skipped. Live from the run started 22:37:48.

**Found, not changed (pre-existing, SAF-001).** In the 21:45 session the `:3` key listener was judged deaf at
21:46:12 (`heard no key presses ... restarting it`) and never reported again, while `:0` reported every minute
to the end: `z` went unheard twice and the operator's takeover keys on the game display were presumably dead for
50 minutes. The 20:22, 21:10 and 21:44 logs had no deaf event. `wingman/input_linux.py`'s watchdog restart does
not bring the `:3` recording back; that needs its own fix.

**The path steepens after a third of recovery starts (measured, 150 in-pursuit recoveries, logs 21:10 to 23:06).**
The first HUD read after the start was 5 deg or more steeper than the last read before it in 51, flatter in 54,
about the same in 45; a respawn followed within 15 s in 11 of the 51 and 3 of the 54. The HUD `Nose:` is the
flight-path angle from the altitude rate, not the attitude. Frames saved at recovery start and 1.5 s later
(23:05:34, HUD -27 then -46 deg at 891 m) show the aircraft upright and nose-down into a canyon at the start and
the nose already near the horizon 1.5 s later, banked, facing a canyon wall: the pull works, the path lags it.
Not inverted in that case. Ten pairs were saved (23:02-23:14); all three that steepened (23:05:34, 23:09:29
-28 to -39 deg, 23:12:31 -12 to -21 deg) show the aircraft upright at the start, so the inverted-recovery
hypothesis is refuted for those three: the path lags a working pull. The 23:12:31 frame (HUD 1489 m, 1262 kph)
shows the chase following a red target directly over a rock mesa whose top fills the view just ahead and below:
on the canyon map the terrain ahead, not the altimeter's 0 m, is the threat.

**Change 4 run, first 46 minutes (22:37:48 to 23:23, measured).** 8 matches started, 19 deaths (about 2.4 per
match), 50 in-pursuit recoveries. Deaths: 13 with an incoming missile in the 6 s before health went, 6 other
(gunfire, terrain contact at about 330 m after a vertical dive at 23:01, unexplained level flight), none with a
TERRAIN AHEAD line in the 10 s before. Ten deaths came within 12 s of a recovery start, mostly to missiles during
the slow pull-out. The rejected-read path that change 4 targets was not seen to matter in this run.

**Defect in change 1, seen once (measured, 23:36:28-58).** The recovery began at 23:36:28; the path was +24 deg
at 23:36:29.9, and from 23:36:34 the tree selected `Idle` (no time to ground, rate +66 m/s). The hold never logged
`emergency CLEARED` and ran to the 30 s cap (`climb complete (recovery_cap, 30.3s)`), pulling to +90 deg at
132 kph while the chase yielded. Inferred from the code path: only `BehaviorTreeHandler._update_climb`, which runs
while Climb is the selected leaf, pushes the verdict into the hold (`set_climb_emergency`), so once another leaf
wins, `_climb_emergency_requested` stays at its last value and the `crash_cleared` hand-back can never fire.
1 of about 200 in-pursuit recoveries today (161 ended `crash_cleared`, the rest in a respawn). Candidate fix, not
made: push the hard-emergency verdict every tick whatever leaf is selected, or treat Climb losing selection as
the emergency clearing.

**Changes 5-7 (operator, 2026-10-03 00:00, `/proceed` on the three recommendations above).** Seen again at
23:51:47 (`recovery_cap, 30.3s`) before they shipped.
- 5: `climb.terrain_avoidance.shadow: false`. A TERRAIN AHEAD verdict is part of the hard emergency again, so it
  flies this ADR's crash recovery. The 2026-09-17 padlock concern is unresolved and recorded in the config note.
- 6: in crash-recovery mode a fresh path at or above level hands the chase back whatever the tree's mean says
  (with no angle, the cleared emergency and a climbing rate still do), and the same rule stops a recovery from
  starting on a level or climbing path (`crash recovery not started — path ... is level or climbing`), so the
  mean's lag cannot start and stop a hold every tick.
- 7: `BehaviorTreeHandler._push_climb_emergency` pushes the hard verdict into a running hold every tick whatever
  leaf is selected.
Tests: `tests/test_pursuit_recovery.py` (hand-back and start rules; the two airbrake tests moved to a latched
hold), `tests/test_tick_handlers.py` (the push). `make lint` clean, `make test` 2,615 passed, 35 skipped.

**Change 5 reverted (2026-10-03 00:10, measured).** The run started 00:07:13 was on a night map. `sky_hsv`'s value
floor is 180 and the night sky is far darker, so the real `detect_terrain_ahead` read a sky fraction of 0.000 to
0.001 on six live frames (00:09:59-00:10:07) while the aircraft climbed at +20 to +58 deg into open sky. Terrain
ahead held true continuously and the crash recovery took the airframe each time the path dipped below level
(9 recoveries and 10 declined starts in 3.5 minutes; 00:08:47, 00:09:02, 00:09:17). `shadow` is back to true;
changes 6 and 7 stay. Terrain ahead needs a detector that handles night maps (a sky test relative to the frame's
own brightness, or treating a uniformly dark crop as unreadable) before it actuates again; that design is the
operator's call.

**Recoveries overshoot to vertical (measured).** 23:06:07: -39 deg at 1033 kph, +12 at 700, +90 at 224 within 6 s;
earlier hand-backs at +90 deg and 245, 177 and 175 kph. `climb.max_pitch_deg` is 80, and the 3-read mean keeps the
emergency on after the path has turned up, so the hold keeps pulling. Proposed, not made: in crash-recovery mode,
hand back at the first fresh path at or above level whatever the averaged verdict says.

## References

ADR 086 (dive recovery on time to ground), ADR 137 (emergency climb), ADR 141 (altitude floor), ADR 143 (died-armed
classification), ADR 144 (mission_su30; two writers), ADR 147, HLDD 015 (pursuit mode), requirement SAF-001,
`docs/action-item/001-target-tracking-lock-stability-and-false-positives.md` (Cycle 16 live result, Cycle 17).
