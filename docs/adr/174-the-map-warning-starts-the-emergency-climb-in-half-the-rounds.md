# ADR 174 — The Map Warning Starts the Emergency Climb in Half the Rounds

| Status | Date       | Wingman Version |
|--------|------------|-----------------|
| Draft  | 2026-11-07 | 1.9.4           |

## Decision

[Design 020](../hldd/020-terrain-service-from-the-maps-own-geometry-hldd.md) Phase 5, first step. The
terrain service's warning flies, judged by arms drawn per round.

- **D1 — the warning.** On a battle tick of a map with data, the terrain service's `ttc_y` of 2 s or less
  is a map warning. `ttc_y` is the path, projected from the last two fixes on the track and 15 deg
  either side, meeting the height map with no margin.
- **D2 — the arms.** Each round draws one of two arms, as ADR 172 does. In the `map` arm, a map warning
  starts the existing emergency climb on its own. In the `control` arm it is logged and nothing flies on
  it. The share is `terrain_service.trial_share` (0.5).
- **D3 — the climb is the existing one.** No new manoeuvre in this step. Choosing the way out (pull,
  roll then pull, turn) is the next step, and its own ADR.
- **D5 (revision 1, 2026-11-08) — the way out flies, decided early.** Superseding D1 and D3 within this
  Draft, after the operator's "forced climbs arent always the way to go".
  - In the `map` arm, when the straight path meets the ground within `decide_s` (5 s), the best of the
    terrain service's candidates flies: a turn (left or right, plain or climbing) through the new
    TerrainTurn leaf, or a climb through the existing emergency climb.
  - The TerrainTurn leaf is the boundary turn's bank-and-pull toward the chosen side, for up to
    `turn_max_s` (3 s). It ranks above BoundaryTurn and Climb.
  - The 2 s climb of D1 remains only as the fallback when no candidate beats holding course.
  - Nothing flies when the straight path is clear, which removes the warnings raised only by the 15 deg
    spread. Nothing flies on a reading with the altitude 30 m or more below the ground at the fix.
  - Why: deciding at 5 s found a clear way out for 62 of 107 crashes against 12 at 2 s, and a turn is
    the only way out in about a fifth of path-into-terrain crashes (Design 020, "The way out").
  - Rounds before this revision (2026-11-08 01:07 to 04:2x) flew D1 and are scored apart.
- **D6 (revision 2, 2026-11-08) — a climbing path does not refuse the map's climb.** Inside a pursuit,
  the emergency climb was refused while the path was level or climbing, and it handed back as soon as
  the path pointed up. A canyon wall rises faster than a +18 deg climb.
  - On the 11-08 logs, 42 of 311 map warnings came on a level or climbing path and got no climb.
    Another 90 were held off because a target or the resupply marker was in view.
  - At 17:30:51 the map saw the 1,061 m wall of the north-west canyon 2.9 s out, with the climb
    candidate clear. The climb was refused and the aircraft hit the wall 2.6 s later.
  - Now, while the service said "climb" within the last 2 s, the recovery starts on a climbing path
    and does not hand back for one.
  - The hold-off for an objective in view is unchanged: breaking it would abort resupply approaches.
  - Rounds before this revision are scored apart from those after it.
- **D7 (revision 3, 2026-11-08) — the path is flown at the HUD's speed along the fixes' track.**
  - The horizontal speed from successive fixes matched the HUD on average: a median of 1.05 times
    speed times the cosine of the path angle, over 8,554 readings from 10:00 to 19:30.
  - It was 1.5 times or more on 11% of readings, and 0.83 or less on 10%.
  - At 19:32:44 the fixes gave 185 m/s while the aircraft climbed at +55 deg and 356 kph (57 m/s
    across the ground). The map projected 590 m instead of 180 m and called a climb at +55 deg and
    356 kph, inviting a stall.
  - Now the fixes give only the direction, and the HUD gives the speed when it is fresh.
  - A HUD speed under 0.25 or over 2 times the fixes' is taken as a misread, and the fixes' speed
    stands. A 148 kph misread in a 900 kph dive would otherwise fly the path too slowly and warn late.
  - The TERRAIN line logs both: `speed=57(fix 185)`.
- **D8 (revision 4, 2026-11-08) — a terrain turn gives way at once to the map's climb.** The
  TerrainTurn leaf holds for at least 2 s and ranks above Climb (D5).
  - In today's logs there were 49 terrain turns. During 31 of them the map's call became a climb,
    and Climb took over a median 1.5 s later.
  - 11 of those 31 were followed by a no-kill-card death within 20 s. The base rate for a random
    20 s battle window is about 6%.
  - Icebound, 20:09:52.9: the left turn hit at 2.5 s and the call became a climb. The climb started
    at 20:09:54.5, with every way out at 0.3 s.
  - Now the turn's hold ends on the tick the map warning asks for the climb. A tick with no call
    keeps the hold.
- **D4 — the verdict.**
  - First: no-kill-card deaths per round, by arm, over at least 40 rounds per arm on maps with data.
  - Then: score per round by arm. Score is too noisy to judge on anything but arms drawn per round
    ([ADR 165](165-the-evade-trial-draws-its-arm-per-round.md)).

## Why

Measured on 63 crashes over four Crimson Canyon sessions, 2026-11-07, by
`test_screenshots/terrain/tools/margin_sweep.py` and `episodes.py` on the live `TERRAIN:` lines:

| Warning | Crashes warned 0.5 to 8 s before | Episodes in ordinary flight |
|---|---|---|
| `ttc_y` 1 s or less | 22 of 63 (35%) | 19 an hour |
| `ttc_y` 2 s or less | 41 of 63 (65%) | 35 an hour, one every 103 s |
| `ttc_y` 3 s or less | 49 of 63 (78%) | 53 an hour |
| 150 m margin, 2 s or less | 47 of 63 (75%) | 51 an hour |

- **The crash rate.** Wingman crashed 1.1 to 1.5 times a round in those sessions: 96 no-card deaths in 72
  rounds, about 10 an hour. (Corrected 2026-11-08: the first draft said 28 an hour, dividing by only
  the hours with a fix on consecutive ticks, and so did the episode rate below. Its "an hour" is per
  hour of tracked flight, about a third of battle time.)
- **The pull-out trigger it would sit beside.** It warns on 26.4% of crashes at 5.3% false alarms
  (Research 017).
- **The cost.** One forced climb every 100 s of tracked flight, against a crash or so a round. In the
  first five `map` rounds (2026-11-08) that came to about 2.4 forced climbs a round. The rate of
  climbs is why D1 uses no margin and 2 s, not the 150 m margin that catches more.

## Consequences

- **Score.** In the `map` arm wingman climbs out of about 35 situations an hour that were not crashes.
  Some of them are near misses its own escape would have handled. Score may fall even if crashes fall,
  hence D4's order.
- **Maps without data.** Nothing changes there: no service, no warning, and the round counts in neither
  arm's map tally.
- **Accuracy.**
  - The warning depends on the minimap fix: 80% of battle ticks have one.
  - A fix 100 m off beside a cliff can warn falsely (2026-11-07 22:48, a dive beside the south mesa
    flagged at 0.1 s).
  - Positions come from the minimap texture and heights from the collision meshes, both read from the
    game files for content build 2026-10-28. A game update can move either.
- **Altitude.** The altitude this compares against is the HUD's signed mesh height, fixed in Design 020
  cycle 7. Below zero it was the absolute value before that.

## Implementation (2026-11-08)

- **`wingman/terrain_service.py`.** `new_round()` draws the arm, `warns()` tests `ttc_y` against `warn_s`,
  and `flies()` is true in the `map` arm. Config: `terrain_service.warn_s` (2.0) and `trial_share` (0.5).
- **`wingman/tick_handlers.py`.**
  - The round's arm is drawn on entering a battle state from outside one, logged as
    `MAP TRIAL: round N — <arm> arm`, and tagged on the round as `map_warning`.
  - The tick's terrain reading now comes before the snapshot. The `TERRAIN:` line gains `arm=` and
    `WARN`, and a flying warning sets `terrain_map_warning` on the snapshot.
- **`wingman/behavior_tree.py`.** The climb condition ORs `terrain_map_warning` into its emergency and
  logs `BT: MAP WARNING … climb forced (ADR 174)` on the rising edge.
- **Tests.** `tests/test_terrain_service.py` and `tests/test_mission_stats.py`.

## Validation

- [ ] `map` and `control` arms drawn and logged per round, with the arm on the round's mission-stats line.
- [ ] 40 or more rounds per arm on maps with data.
- [ ] No-kill-card deaths per round compared by arm; then score per round.

## References

- [Design 020](../hldd/020-terrain-service-from-the-maps-own-geometry-hldd.md): the service, Phase 3's
  scores, the clearance margin sweep.
- [ADR 172](172-agreeing-terrain-warnings-leave-the-resupply-run-in-half-the-rounds.md): the per-round
  arm this copies.
