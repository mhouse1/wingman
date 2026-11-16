# ADR 172 — Agreeing Terrain Warnings Leave the Resupply Run in Half the Rounds

| Status | Date       | Wingman Version |
|--------|------------|-----------------|
| Draft  | 2026-11-04 | 1.9.4           |

Operator, 2026-11-04, on a `v` screenshot (`tests/test-output/screenshot_20261104_163717.png`): "it had
clear path to resupply but nosed up missing it", then "proceed with recommended changes to improve
'Score per round' dont ask me".

## Decision

**D1. A trial, drawn once a round.** At the round start ADR 165 uses (entering a battle state from
outside one), the round draws an arm: `holdoff` with probability
`behavior_tree.climb.warning_agreement.resupply_holdoff_share`, `override` otherwise. The log names it:
`TERRAIN TRIAL: round N — <arm> arm (ADR 172, share 0.50)`.

**D2. In a `holdoff` round, two agreeing terrain warnings do not break off a resupply approach.** Code
Review 019 cycle 25 lets the agreement set aside the crash recovery's objective hold-off; in a `holdoff`
round it does not for the resupply marker, at the recovery's start or at its hand-back when the marker
comes into view (`Controller: two terrain warnings agree with the resupply marker in view — this round's
trial arm keeps the hold-off (ADR 172)`). The tree still asks for the climb; the hold-off answers it, as
it does for one warning alone. Every other objective, and the pull-out alarm's own break of the hold-off
under 300 m (cycle 36), are unchanged in both arms. An `override` round is cycle 25 as it was.

**D3. The share is 0.5.** The schema default is 0, the trial off.

**D4. Each round records its arm and its no-card deaths.** The stats file's missions carry
`tags.agree_resupply` (`holdoff` or `override`) and `no_card_deaths`, the deaths the kill card read no
card for (`KillCardReader` now reports each verdict).

## Why

The agreement fires on a dive at anything on the ground, a marker included. One case (J-20, acct1): the
marker centred and the jet descending at -29 deg, 925 KPH, 1,408 m; `TERRAIN WARNINGS AGREE: LOOM and
ESCAPE`, then `crash recovery starts with the resupply marker in view — two terrain warnings agree`, an
emergency climb, and the rearm missed.

Over the logs from the override's first line (2026-10-26) to 2026-11-04 (measured,
`agree_override.py`):

| The agreement broke off | Times | A crash (no kill card) within 12 s | A rearm within 25 s anyway |
|---|---|---|---|
| a resupply approach | 340 | 46 (13.5%) | 163 (48%) |
| a chase (target) | 667 | 42 (6.3%) | 45 |
| a control point | 73 | 8 (11.0%) | 1 |
| a priority target | 41 | 12 (29.3%) | 2 |

Wingman's own rate is about 4% crashes in any 12 s (12.4 an hour), so the agreement does pick out risky
moments. But 13.5% still crash after the forced climb, and the 86.5% that did not mix the climbs that
saved an aircraft with false alarms like the one above; a log cannot tell them apart. The operator's flying
(Research 020) set the agreement off 10 times in 394 s of low flying without a crash.

Only the resupply approach is in the trial: it is where the override costs score (a rearm is missiles,
and kills are the score's main term, about 147 points each over 1,257 rounds), and it is the case the
operator reported. The priority target's 29% stays protected.

## Consequences

- In half the rounds a resupply run is flown through two agreeing warnings, protected only by the
  pull-out's break under 300 m. If the agreement's climbs on resupply runs save aircraft, the `holdoff`
  arm crashes more on them while the trial runs.
- A round with no resupply approach under agreement is the same in both arms; it still carries an arm.
- `no_card_deaths` counts every no-card death, crash or missed card (a J-20 shoot-down whose altitude
  froze, a card read once). Both arms carry the same misreads.

## Validation

- **V1, tests.** `tests/test_agreement_trial.py` (the draw, share 0, only the resupply held off, the
  shipped share); `tests/test_pursuit_recovery.py` with a real Controller, `WarningAgreement` and
  `AgreementTrial` (a `holdoff` round keeps the resupply approach and logs it, an `override` round breaks
  it off, a `holdoff` round still breaks off a chase, the hand-back to the marker, the round's draw and
  its tag); `tests/test_mission_stats.py` (both trials' tags on a round, `no_card_deaths`);
  `tests/test_kill_card.py` (each verdict reaches the listener; a failing listener costs nothing);
  `tests/test_tick_handlers.py` (the same round start).
- **V2, the first live session.** One `TERRAIN TRIAL: round N` line per round; the stats file's missions
  carry `tags.agree_resupply` and `no_card_deaths`; in `holdoff` rounds, `trial arm keeps the hold-off`
  lines and no `crash recovery starts with the resupply marker in view — two terrain warnings agree`.
  **Draw and tags met on 2026-11-04** (acct1, MiG-31, 5 rounds: 3 holdoff, 2 override); the hold-off itself
  did not come up, with one rearm all session
  ([Performance 010](../performance/010-the-resupply-agreement-trial-by-round.md)).
- **V3, the verdict, by arm per round:** rearms (`rearm confirmed`), `no_card_deaths`, and score per
  round read, pooled over sessions and accounts. A difference of 0.1 crashes a round (SD about 1) needs
  about 1,600 rounds an arm; rearms move more and are read first. Stop the `holdoff` arm early if its
  no-card deaths within 12 s of a held-off agreement on a resupply run clearly exceed the `override`
  arm's 13.5%.

## References

- [Performance 010 — The Resupply Agreement Trial by Round](../performance/010-the-resupply-agreement-trial-by-round.md):
  the trial's sessions.
- [Research 020 — The Operator's Flying as the Terrain Reference](../research/020-the-operators-flying-as-the-terrain-reference.md)
- [Code Review 019](../code-review/019-2026-10.md): cycle 25, the agreement; cycle 36, the pull-out's break.
- [ADR 165 — The Evade Trial Draws Its Arm per Round](165-the-evade-trial-draws-its-arm-per-round.md): the
  round start and the trial design.
- [ADR 171 — The Jet Picks the Mission](171-the-jet-picks-the-mission-and-j20-comes-back-from-a-rearm.md):
  the round tag.
- [Design 001 — Terrain Avoidance](../hldd/001-terrain-avoidance-hldd.md).
