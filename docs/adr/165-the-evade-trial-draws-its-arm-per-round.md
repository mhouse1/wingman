# ADR 165 — The Evade Trial Draws Its Arm per Round

| Status | Date       | Wingman Version |
|--------|------------|-----------------|
| Draft  | 2026-10-29 | 1.9.3           |

Operator, 2026-10-29: "make the change", on the recommendation to run
[ADR 163](163-the-missile-evade-flies-through-a-pursuit.md)'s trial again with the arm drawn per round
instead of per engagement.

## Decision

**D1. The trial's arm is drawn once per round.** On the round's first engagement inside a pursuit:
`evade` with probability `behavior_tree.missile_evade.pursuit_share`, `control` otherwise. Every later
engagement inside a pursuit in that round takes the same arm. A round opens where the session summary
opens a mission, on entering a battle state from outside one; the flare handler passes that on
(`Controller.pursuit_evade_new_round`). Engagements are grouped as before, and one that opens outside a
pursuit still carries no arm. This replaces ADR 163 D4's draw per engagement.

**D2. The share is 0.5**, with [ADR 164](164-roll-right-through-the-flares.md)'s flare roll in both arms.
This replaces ADR 163 V3's share of 0 while this trial runs.

**D3. Each round records its arm.** The stats file's missions carry `trial_arm`, and
`evade_trial_rounds` sums each arm's rounds and PERFORMANCE readings. The session summary prints an
`evade arm` and a `control arm` line under Round performance: rounds read, then score and K / D / A per
round read. The log names each round's draw (`MISSILE EVADE[pursuit]: round N — <arm> arm`). ADR 163's
engagement line and its engagement survival by arm (`pursuit trial`) are unchanged.

## Why

ADR 163 judged the evade by engagement survival. Its arms were drawn inside each session, so both faced the
same lobbies. But a round mixed both arms, so its score could not be split by arm.

After the share went to 0, the two accounts flown on both sides of it scored about one standard error less
per round, and earned significantly fewer assists:

| Per round read | Account 2: trial, then share 0 | Account 3: trial, then share 0 |
|---|---|---|
| Rounds read | 32, then 46 | 62, then 62 |
| Score | 416, then 365 (t = -1.1) | 393, then 354 (t = -1.1) |
| Kills | 1.41, then 1.74 | 1.94, then 2.05 |
| Deaths | 2.53, then 2.83 | 3.50, then 3.11 |
| Assists | 2.09, then 1.11 (t = -2.8) | 1.61, then 1.08 (t = -2.6) |

Account 2's trial session had the flare roll; account 3's predates it. Both trial sessions were on
2026-10-27 and both share-0 sessions on 2026-10-28, so a change on the game's side between those days would
look the same.

A comparison between sessions cannot settle it. On account 2, engagements without an evade survived 85%
(90 of 106) in the trial session and 70% (289 of 416) a day later, on the same code (z = -3.7). Only arms
drawn inside the same sessions compare like with like. Only arms drawn per round give each round one arm
to score it against.

## Consequences

- Half the rounds fly the evade inside a pursuit again. With the flare roll, ADR 163 V3 measured the evade
  arm surviving 61% of engagements against 85%; if that holds, the evade arm loses aircraft while this
  trial runs.
- Engagements now cluster by round, so ADR 163's engagement survival by arm overstates its own precision.
  Compare survival round by round as well.
- A round with no engagement inside a pursuit draws no arm and is in neither. Before any difference between
  the arms, such a round is the same under both, so leaving it out biases neither.
- A mission split by a drop out of the battle states (to `GAME_UNKNOWN` and back) is two rounds for the
  trial, as it is two missions for the summary.

## Validation

- **V1, tests.** `tests/test_evade_trial.py`: one draw a round, kept by its later engagements; a fresh draw
  after `new_round`; none outside a pursuit. `tests/test_tick_handlers.py`: a battle entry from outside a
  battle starts a round, moves between battle states do not, and a failing round start costs the state
  change nothing. `tests/test_pursuit_missile_evade.py`: with a real Controller, the round's draw is logged
  once and the next round draws again. `tests/test_mission_stats.py`: the round keeps its arm, the sums by
  arm, and the summary lines.
- **V2, the first live session.** One `MISSILE EVADE[pursuit]: round N` line per round with an engagement
  inside a pursuit; every `engagement` line in that round names the same arm; the stats file's missions
  carry `trial_arm`; the summary prints both arm lines. **Met on 2026-10-29** (account 2, 6 rounds: 5
  evade, 1 control; 22 evades, all in evade rounds; no fault):
  [Performance 009](../performance/009-the-evade-trial-by-round.md).
- **V3, the verdict**, after 56 read rounds an arm, pooled over accounts. The arms are drawn inside each
  session, so the accounts' differences fall on both alike. Assists, kills, deaths and score per round read,
  by arm, each with its t; engagement survival by arm, also by round. 56 read rounds an arm show an assists
  gap of 0.7 with the usual 80% chance (per-round SD 1.3), about 19 hours of play at 10.5 rounds an hour
  with 55% read. Score alone would need about 317 an arm (SD 200, a 45-point gap), so it is read from its
  parts. The operator sets the share from the result.

## References

- [ADR 163 — The Missile Evade Flies Through a Pursuit](163-the-missile-evade-flies-through-a-pursuit.md):
  the evade inside a pursuit, the per-engagement trial and its verdict.
- [ADR 164 — Roll Right Through the Flares](164-roll-right-through-the-flares.md): the flare roll in both arms.
- [ADR 070 — Missile Evade Tactic](070-missile-evade-tactic.md): the evade itself.
- [Design 018](../hldd/018-score-per-round-in-grafana-hldd.md): score per round over time.
- [Performance 009 — The Evade Trial by Round](../performance/009-the-evade-trial-by-round.md): the trial's
  sessions, by arm.
- Stats files: `docs/performance/current/run_20261027_060741_acct3_stats.json`,
  `run_20261027_183245_acct2_stats.json`, `run_20261028_070126_acct3_stats.json`,
  `run_20261028_170643_acct2_stats.json`.
