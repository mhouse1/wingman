# Performance 009 — The Evade Trial by Round

| Status | Date       | Wingman Version |
|--------|------------|-----------------|
| Draft  | 2026-10-29 | 1.9.3           |

## Summary

The sessions of [ADR 165](../adr/165-the-evade-trial-draws-its-arm-per-round.md)'s trial, by arm. The arm
is drawn once a round, so each round is scored against one arm. **V2 (the first live session) is met. V3
(the verdict) needs 56 read rounds an arm; after one session the evade arm has 2 and the control arm 0.**

## Sessions

| Session | Account | Rounds, evade / control | Read, evade / control | Engagements survived 10 s, evade / control | Notes |
|---|---|---|---|---|---|
| 2026-10-29 02:50 to 03:25, `run_20261029_025029_acct2`, `logs/wingman_20261029_035120.log` | 2 | 5 / 1 | 2 / 0 | 10 of 23 (44%) / 6 of 9 (67%) | First session. Ended with Backspace at 03:25:48 (standby); MetalStorm closed at 03:51:19 |

## Totals by arm

Per round read; engagements counted by the arm drawn.

| Arm | Rounds | Read | Score | Kills | Deaths | Assists | Engagements survived 10 s |
|---|---|---|---|---|---|---|---|
| Evade | 5 | 2 | 308.5 | 1.5 | 3.5 | 1.5 | 10 of 23 (44%) |
| Control | 1 | 0 | | | | | 6 of 9 (67%) |

Too early to read: the control arm is one round, and the arms' engagements cluster by round.

## V2, the first live session (measured, 2026-10-29)

- **One draw a round.** 6 rounds, 6 `MISSILE EVADE[pursuit]: round N` lines, each at the round's first
  engagement inside a pursuit (02:51:33, 02:57:58, 03:05:23, 03:10:32, 03:15:28, 03:21:39). Evade 5,
  control 1; one arm taking 5 of 6 happens by chance about 1 time in 5.
- **Each engagement carries its round's arm.** 32 `engagement` lines: 23 evade, 9 control, none differing
  from its round's draw.
- **The evade flies only in evade rounds.** 22 `MISSILE EVADE — holding` lines, all in the five evade
  rounds, none in the control round (03:10:32 to 03:14:22). The pursuit yielded to the evade 22 times.
- **The record.** The stats file's six missions carry `trial_arm`; `evade_trial_rounds` matches them; the
  session summary printed both arm lines.
- **No fault.** No traceback, no `[ERROR]`, no failure of `pursuit_evade` or `pursuit_evade_new_round`, no
  manual takeover.
- **ADR 163's safety checks.** 51 climbs that were not hard emergencies waited for an evade, in 18
  stretches, as [ADR 163](../adr/163-the-missile-evade-flies-through-a-pursuit.md) D2 designs. 11 stretches
  were followed by a death within 10 s, and each death card names a missile kill: AIM-54 Phoenix 7,
  Meteor, IRIS-T, AIM-120 AMRAAM and R-74 Archer 1 each. All 23 deaths had an enemy's card. A hard
  emergency ended 6 evades (ADR 163 D5). The 3 deaths classed as terrain came 49 to 76 s after the last
  alert, with no evade flying.

## The round reader

The PERFORMANCE panel was read on 2 of 6 rounds, both evade rounds. Rounds 3 to 6 logged `not read — no
KDA on any of 4 frames`, the failure [Action Item 005](../action-item/005-round-performance-reader-and-hldd-018-corrections.md)
covers. At a third of rounds read, the 56 read rounds an arm take about 32 hours of play rather than 19.
The false reading of page 5 at 03:18:40 (`kda '63/4/040'`, another player's row) stayed in shadow: round 5
has no reading in the stats file, so the sums by arm are clean.

## Adding a session

From the session summary's `evade arm` and `control arm` lines and its `pursuit trial` line, or from the
stats file: `evade_trial_rounds` for rounds and readings by arm, `missions[].trial_arm` with
`missions[].performance` for each round, and `missile_engagements.trial_*` for survival. V3 compares the
figures per round read, each with its t, pooled over accounts.
