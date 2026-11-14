# Performance 010 — The Resupply Agreement Trial by Round

| Status | Date       | Wingman Version |
|--------|------------|-----------------|
| Draft  | 2026-11-04 | 1.9.4           |

## Summary

The sessions of [ADR 172](../adr/172-agreeing-terrain-warnings-leave-the-resupply-run-in-half-the-rounds.md)'s
trial: in `holdoff` rounds two agreeing terrain warnings do not break off a resupply approach, in
`override` rounds they do (Code Review 019 cycle 25 as it was). The verdict compares the arms per round:
rearms, `no_card_deaths`, and score per round read.

## Sessions

| Session | Account, jet | Code | Rounds, holdoff / override | Notes |
|---|---|---|---|---|
| 2026-11-04, `make r`, log `wingman.log` | acct1, MIG-31 FOXHOUND in the lobby | the uncommitted tree on `acs-j20` with ADR 172 (share 0.5) and ADR 165's share 0; lint clean, 3,974 of 3,975 tests passed, the one failure `test_session_recording.py::test_each_written_frame_has_its_wall_clock_time`, a timing flake in a module ADR 172 does not touch (1 failure in 9 runs alone) | 3 / 2 (scores 150, 585, 485 / 250, 1,920; no-card deaths 0, 2, 0 / 0, 0) | Stopped by the operator's `z` in matchmaking; `run_20261104_172513_acct1_stats.json`. V2 met: one `TERRAIN TRIAL` line a round, `tags.agree_resupply` and `no_card_deaths` on every mission. No agreement came up on a resupply run in either arm (one rearm all session), so the rule was not exercised. |

## Adding a session

From the log: one `TERRAIN TRIAL: round N — <arm> arm` line per round, `trial arm keeps the hold-off
(ADR 172)` lines in `holdoff` rounds, `crash recovery starts with the resupply marker in view — two terrain
warnings agree` lines in `override` rounds only, and `RESUPPLY: rearm climb-out` per rearm. From the stats
file: `missions[].tags.agree_resupply`, `missions[].no_card_deaths`, `missions[].performance`.
