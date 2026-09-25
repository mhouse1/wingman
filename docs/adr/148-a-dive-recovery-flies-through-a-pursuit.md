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

## References

ADR 086 (dive recovery on time to ground), ADR 137 (emergency climb), ADR 141 (altitude floor), ADR 143 (died-armed
classification), ADR 144 (mission_su30; two writers), ADR 147, HLDD 015 (pursuit mode), requirement SAF-001,
`docs/action-item/001-target-tracking-lock-stability-and-false-positives.md` (Cycle 16 live result, Cycle 17).
