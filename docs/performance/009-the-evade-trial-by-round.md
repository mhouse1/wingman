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
| 2026-10-29, `run_20261029_025029_acct2`, `logs/wingman_20261029_035120.log` | 2 | 5 / 1 | 2 / 0 | 10 of 23 (44%) / 6 of 9 (67%) | First session. Ended with Backspace (standby); MetalStorm closed |

## Totals by arm

Per round read; engagements counted by the arm drawn.

| Arm | Rounds | Read | Score | Kills | Deaths | Assists | Engagements survived 10 s |
|---|---|---|---|---|---|---|---|
| Evade | 5 | 2 | 308.5 | 1.5 | 3.5 | 1.5 | 10 of 23 (44%) |
| Control | 1 | 0 | | | | | 6 of 9 (67%) |

Too early to read: the control arm is one round, and the arms' engagements cluster by round.

## V2, the first live session (measured, 2026-10-29)

- **One draw a round.** 6 rounds, 6 `MISSILE EVADE[pursuit]: round N` lines, each at the round's first
  engagement inside a pursuit. Evade 5,
  control 1; one arm taking 5 of 6 happens by chance about 1 time in 5.
- **Each engagement carries its round's arm.** 32 `engagement` lines: 23 evade, 9 control, none differing
  from its round's draw.
- **The evade flies only in evade rounds.** 22 `MISSILE EVADE — holding` lines, all in the five evade
  rounds, none in the control round. The pursuit yielded to the evade 22 times.
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

## V3, the verdict (measured, 2026-11-04)

Every stats file in `docs/performance/current/` with a round carrying `trial_arm`: 76 sessions, from
`run_20261029_025029_acct2` to `run_20261104_114138_acct3`, all four prefixes, every jet the lobby held.
The arm is drawn inside each session, so the jet and the lobby fall on both arms alike.

| Per round read | Evade arm | Control arm | Evade minus control | t |
|---|---|---|---|---|
| Rounds read | 431 | 417 | | |
| Score | 382 | 398 | -16 | -0.74 |
| Kills | 1.93 | 2.07 | -0.15 | -1.17 |
| Deaths | 3.58 | 3.41 | +0.17 | +1.60 |
| Assists | 1.33 | 1.46 | -0.13 | -1.32 |

Score, within each session and weighted by its rounds: control ahead by 7 (se 20).

| Engagement survival (10 s after the alert) | Evade arm | Control arm | |
|---|---|---|---|
| By engagement | 64.0% (1,230 of 1,923) | 69.3% (1,304 of 1,881) | z = -3.5 |
| By round, mean of each round's share | 63.4% (591 rounds) | 67.0% (585 rounds) | t = -2.1 |

By account, per round read (score, then K / D / A):

| Account | Evade arm | Control arm |
|---|---|---|
| main | 42 rounds: 489, 2.48 / 3.24 / 1.43 | 47 rounds: 497, 2.62 / 3.43 / 1.91 |
| acct1 | 126 rounds: 345, 1.69 / 3.98 / 1.35 | 115 rounds: 317, 1.69 / 3.76 / 1.29 |
| acct2 | 88 rounds: 358, 1.73 / 3.27 / 1.35 | 109 rounds: 368, 1.75 / 3.46 / 1.67 |
| acct3 | 175 rounds: 394, 2.06 / 3.53 / 1.29 | 146 rounds: 452, 2.44 / 3.10 / 1.29 |

The trial was reopened because assists fell after ADR 163's share of 0. Drawn per round, the evade arm
earned fewer assists, not more. No per-round measure favours the evade. The evade arm loses aircraft:
the engagement survival gap is significant both ways it is counted. The score gap is inside the noise. At
this size of trial it rules out an evade worth more than about 30 points a round.

| Run at share 0 | Account, jet | Code | To check |
|---|---|---|---|
| 2026-11-04, `make r`, log `wingman.log` | acct1, SU-57 FELON in the lobby | the uncommitted tree on `acs-j20` plus `pursuit_share` 0.0; full gate 3,956 passed | No `MISSILE EVADE[pursuit]` line; no `trial_arm` on any mission in the stats file; score and K / D / A per round read, against this jet's own history and not the trial's |
| 2026-11-04, 69 min in | as above | as above | 10 rounds read: 705, 1,220, 715, 1,300, 1,645, 1,535, 1,695, 605, 1,085, 975; 1,148 a round, K / D / A 5.5 / 2.8 / 3.1. No `MISSILE EVADE[pursuit]` line. The SU-57 has no earlier rounds to compare with, so this measures the jet, not the share. One more round went unflown: ADR 102 called the match not started, and it started with wingman in GAME_LOBBY |
| 2026-11-04, 77 min in | acct1; the lobby jet became TORNADO ADV, not by wingman | as above | Round 11 (score 1,895, K/D/A 7/1/2) is not wingman's: a manual takeover (`enter`, `source=unknown`) flew it to the end. Left out of the tally |
| 2026-11-04, result | stopped by the operator's `z` in matchmaking; log `logs/wingman_20261104_154646.log` | as above | 13 missions, 10 rounds read (3 taken over, left out by the summary): 1,148 a round, K / D / A 6.0 / 2.8 / 3.1. No `MISSILE EVADE[pursuit]` line all session. One round lost unflown to a false ADR 102 call. |

## The round reader

The PERFORMANCE panel was read on 2 of 6 rounds, both evade rounds. Rounds 3 to 6 logged `not read — no
KDA on any of 4 frames`, the failure [Action Item 005](../action-item/005-round-performance-reader-and-hldd-018-corrections.md)
covers. At a third of rounds read, the 56 read rounds an arm take about 32 hours of play rather than 19.
The false reading of page 5 (`kda '63/4/040'`, another player's row) stayed in shadow: round 5
has no reading in the stats file, so the sums by arm are clean.

## Adding a session

From the session summary's `evade arm` and `control arm` lines and its `pursuit trial` line, or from the
stats file: `evade_trial_rounds` for rounds and readings by arm, `missions[].trial_arm` with
`missions[].performance` for each round, and `missile_engagements.trial_*` for survival. V3 compares the
figures per round read, each with its t, pooled over accounts.
