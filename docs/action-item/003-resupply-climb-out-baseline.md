# Action Item 003 — Baseline Before the Resupply Climb-Out Moves to the Pass

| Status | Date       | Wingman Version |
|--------|------------|-----------------|
| Draft  | 2026-10-13 | 1.9.2           |

## Request

Operator, 2026-10-13: "create action-item003 to track the pre climb-out data, so
we can compare against implementation after the climb-out is implemented."

The change to come: the climb-out after a resupply (nose-up and afterburner for
`pursuit_mode.resupply_priority.rearm_climb_s`, 3 s) starts at the pass itself,
when the marker that had the steering is lost at close range. Today it starts on
the first missile count that shows the rearm, about 1.4 s later. This document is
the "before", and from "The change" on it says what was built.

## What is flying now

The baseline session ran on `d45d4c5` plus two uncommitted changes of 2026-10-13:

- the resupply marker's own steering deadband, `resupply_priority.deadband: 0.025`;
- the climb-out started on the first count that shows the rearm, with the
  afterburner held (Actuator owner `rearm_climb`).

The reference sessions ran the older climb-out: nose-up only, started when the
rearm was confirmed by three reads, with the marker steered at the target's
deadband of 0.05.

## How it is measured

Time zero is **the pass**, not the missile count, so the figures stay comparable
when the trigger moves.

- **Marker sighting:** a `RESUPPLY: spent=...` debug line with `marker=(x,y)`,
  `proposed=True` and `marker_stale=False`.
- **Close pass:** a run of sightings with no gap over 2 s, at least 4 s long,
  with the marker a median of 120 px or less from the frame centre (960, 600)
  between 5 s and 1.5 s before the run's last sighting. The run must end in a
  pickup, or with its last sighting more than 2.5 times that median from the
  centre (at least 100 px). **The pass** is the run's last sighting.
- **Pickup:** an `Ammo missiles: N` line whose N is above the line before it and
  at most 8, from 1.5 s before the pass to 4 s after it. **Confirmed** is a
  `RESUPPLY: confirmed ammo` line from 1.5 s before to 8 s after. A pass with
  neither is a **miss**.
- **Altitude and nose:** `PADLOCK: ... Altitude | Speed | Nose` lines, altitude
  under 20,000 and speed under 3,000 only. "At the pass" is the last such line
  in the 3.2 s before it; "+3 s" is the nearest line within 1.5 s of that
  moment, and so on.
- **Died:** a `PURSUIT SUMMARY: end=external:respawn_detected` line. A death is
  logged several seconds after it happens, which is why no session shows one
  within 5 s. The cause is the `DIED ARMED ... cause=` line within 12 s, when
  there is one.
- **Dive recovery:** `yielding pitch and roll to the dive recovery` from 1 s
  before the pass to 5 s after.
- **Climb-out start:** the `RESUPPLY: rearm climb-out, nose up` line from 1 s
  before the pass to 8 s after.

The numbers below came from throwaway scripts, not from a script in the
repository. Recompute both columns with one script when the comparison is made.

## Baseline

2026-10-13 08:00:11 to 18:15:29, 10.2 h, 102 missions, account 2. The log was
still `wingman.log` when this was written; the next launch archives it as
`logs/wingman_20261013_181529.log`.

76 close passes: 68 picked up (89%), 49 of them confirmed, 8 missed. That is 6.6
pickups an hour and 0.67 a mission.

**After a pickup (68):**

| Measure | Value |
|---|---|
| Climb-out start after the pass | median 1.4 s, 90th percentile 2.0 s (65 of 68 have the line) |
| At the pass | altitude median 1,360 m (10th percentile 728 m); nose median -10 deg (10th percentile -28); speed median 1,077 KPH |
| Altitude change at +3 s | median -120 m (10th percentile -593 m; n=60) |
| Altitude change at +6 s | median +143 m (10th percentile -573 m; n=50) |
| Altitude change at +9 s | median +838 m (10th percentile -315 m; n=47) |
| Nose at +3 s, +6 s, +9 s | median -6, +24, +56 deg |
| Dive recovery took the airframe within 5 s | 33 (49%) |
| Died within 10 s | 17 (25%) |
| Died within 30 s | 29 (43%) |
| Causes of the 17 | no cause line 11, terrain 2, unclassified 2, enemy fire 1, contested 1 |

**After a miss (8):**

| Measure | Value |
|---|---|
| Died within 10 s, within 30 s | 4 (50%), 6 (75%) |
| Marker steered at again within 10 s, within 20 s | 1 (12%), 2 (25%) |
| A pickup within 30 s, within 60 s | 2 (25%), 5 (62%) |

## Reference: the older climb-out

Same definitions. These are context, not the comparison: different sessions,
and the 01:40 one flew much faster.

| Session | Hours, missions | Close passes | Picked up | Climb-out after the pass | Altitude at +3 s, +6 s, +9 s | Died within 10 s, 30 s of a pickup |
|---|---|---|---|---|---|---|
| 10-11 18:16 to 21:49, `logs/wingman_20261011_214911.log` | 3.5 h, 35 | 34 | 29 (85%) | median 4.5 s | -170, -154, +399 m | 4 (14%), 13 (45%) |
| 10-13, `logs/wingman_20261013_014043.log` | 6.1 h, 65 | 34 | 3 (9%) | median 5.1 s | +14, +468, +1,112 m (n=3) | 1, 2 |
| 10-13, `logs/wingman_20261013_033348.log` | 1.7 h, 17 | 13 | 10 (77%) | median 4.8 s | -115, +459, +588 m | 5 (50%), 5 (50%) |

## The change (2026-10-13, uncommitted on `d45d4c5`)

`pursuit_mode.resupply_priority.pass_trigger`, shipped on and actuating.

The marker going missing is not a pass. In the baseline session the steered
marker went missing for 0.9 s or more 162 times. 49 were pickups. 73 times it
was back within 4 s: it had sat a median 20 px from the centre and not moved
(-2 px in its last second). At a pickup it was a median 60 px out and had moved
28 px outward. So the pass is signed when all of these hold
(`resupply.ResupplyPassWatch`):

- the marker has been missing `missing_s` (0.5 s);
- its last sighting was at least `min_off_centre_px` (35) from the centre;
- it had moved at least `min_outward_px` (5) outward against its place 0.6 to
  1.6 s before;
- it had been followed for 2 s or more and had the steering, and neither the
  dive recovery nor a climb-out had the airframe.

Scored on the baseline log at those values, the rule fires on 36 of the 49
pickups and 5 of the 73 dropouts. Of its 21 other firings, 15 came with the
missile count already at 4 or more (inferred: a full selected rack, where the
count cannot show a pickup and no climb-out started before); 8 of the 21 were
followed by a death within 10 s. That scoring
used the log's 0.35 s lines; the steering sees the marker every 0.17 s, so the
first session is the real check.

The count still starts the climb-out when the pass did not. When the pass did,
the count logs `RESUPPLY: count N read X s after the climb-out started at the
pass` and starts nothing. That line is the lead the pass had, and a pass with
no such line within 4 s was a miss or a full rack.

`actuate: false` logs `RESUPPLY: pass seen, shadow (...)` and presses nothing.

## What to compare afterwards

Run at least 60 pickups before reading anything; the baseline has 68.

- **Climb-out start after the pass:** 1.4 s now. It should be near 0.5 s on the
  passes the rule signs (the time the marker has to be missing), and 1.4 s on
  the rest.
- **What the rule fired on:** count the `(marker passed: ...)` climb-outs, and of
  them those followed by the `count N read` line (a pickup), those where the
  marker was sighted again within 4 s (a dropout: the climb-out broke an
  approach), and the rest. On the baseline log that was 36, 5 and 21.
- **Altitude change at +3 s after a pickup:** -120 m now. This is the figure the
  change acts on first.
- **Died within 10 s of a pickup:** 25% now (17 of 68). The reason for the
  change. A difference of a few points is inside what 68 events can show.
- **Dive recovery within 5 s of the pass:** 49% now. If it stays there, the
  aircraft is still arriving in a descent (nose -10 deg at the median, -28 at
  the 10th percentile), and that is the approach, not the climb-out.
- **The cost, on misses:** a climb-out at the pass also fires on a miss. Watch
  the retry figures: a pickup within 60 s of a miss is 62% now (5 of 8), and
  misses are 11% of passes. Eight misses is too few to compare; report the count.
- **Pickups an hour:** 6.6 now, so a climb that costs retries shows up here too.

## After

To be filled from the first session with the change, same definitions.

First run with it: 2026-10-13 19:25:08, `make r2` (account 2), pid 3735809, on
`d45d4c5` plus the uncommitted changes above. Gate before it: `make lint` clean;
`make test` 3,186 passed, 33 skipped, 2 failed, both in
`tests/test_lobby_ready_squad_layout.py` (the `READY` crop, failing before this
change too). The baseline log is archived as cited.

*Watch notes from that run (measured):*

- 19:28:00, first pass signed: a pickup. The marker was last seen 126 px from the
  centre, 87 px further out than a second before; `count 4 read 0.2s after the
  climb-out started at the pass`.
- 19:29:28, second: a miss, and a real pass. The aircraft dived from 1,579 m to
  748 m at 1,100 to 1,500 KPH with the marker 150 to 240 px below the bore sight
  throughout, and the marker left by the bottom of the frame (last sighting 531 px
  out). The climb-out started 0.6 s after that sighting; the dive recovery took
  the airframe 2 s later.
- 19:51:52, third: **the climb-out broke an approach.** The marker had been in
  view low and left for 5 s while a target was nearer. It took the steering at
  19:51:51.5, 597 px from the centre, blinked out for 0.6 s 5 px further out,
  and the rule signed a pass. The marker was back 0.9 s later. The rule had no
  test that the approach was lined up.
- 19:52:27, fourth: no count and no marker after it. The marker was 57 px from
  the centre a second before and 302 px at its last sighting: a pass, a miss or
  a full rack.
- The run was stopped with `z` at 19:52:46 and ended at the lobby at 19:53:28:
  28 min, 3 missions, 4 climb-outs, all four started by the pass, none by the
  count. Its log is the one that ends 19:53:29.
- 19:31:01 to 19:39:51, not the climb-out: the run sat in `GAME_STARTING`, then
  `GAME_UNKNOWN`, behind a dialog no crop covers, "YOU'RE DOING GREAT! ... leave a
  quick rating?" with IT'S OK and LOVE IT! buttons. `GenericCloseRecovery` found no
  close button (its cross is dark on orange), and the liveness guard logged at
  19:38:39. Closed by hand with its cross at (1750, 226) through
  `input_linux._linux_click`; a plain XTest click did not land (ADR 156). Frames:
  `test_screenshots/unknown_anomalies/unknown_20261013_193408_stuck30s.png`,
  `..._193609_stuck151s.png`, `..._193811_stuck272s.png`. It needs a stall crop.

### Change 2 (2026-10-13, after that run)

`pass_trigger.lined_up_px: 120`. The marker must also have been within 120 px of
the centre 0.6 to 1.6 s before its last sighting, the same 120 px this document
uses for a close pass. The 19:51:52 firing (597 px) fails it. On the baseline
log the pickups the rule signs had the marker a median 33 px from the centre at
that moment (130 px at most), so the rule goes from 36 to 35 of the 49 pickups,
and stays at 5 of the 73 dropouts.

Second run, with change 2: 2026-10-13 20:04:27, `make r2`, pid 3778633. Gate
before it: `make lint` clean; `make test` 3,187 passed, 33 skipped, the same 2
failures. The first run's log is `logs/wingman_20261013_195329.log`.

*Watch notes from the second run (measured), neither about the climb-out:*

- 20:04:28: `XKey listener thread died: BadLength` at startup, reconnected 3 s
  later (`hotkeys active again`).
- 20:04:46 to 20:06:26: the game opened on the rewards of the round the first
  run ended on. Wingman clicked REVEAL ALL, then sat in `GAME_UNKNOWN` on a
  crate screen ("+300 SILVER", a crate with a question mark, no button) and
  logged `GAME_UNKNOWN startup classification timeout` every tick from 90 s.
  Advanced by hand with one click on the crate (1280, 770); the lobby was read
  7 s later and PLAY clicked. It needs a stall crop too.

*Second run, about the climb-out (measured):*

- 20:22:06, a climb-out by the count with no pass signed, and 17 s late. The
  aircraft had flown by a resupply point at about 20:21:48 while a nearer target
  had the steering (the marker crossed from (797,718) to (242,1100)). The
  secondary rack then ran down to 0, the pursuit switched back to the primary,
  and its count read 4: the rearm showed only when the rack was selected again.
  The climb-out held nose-up and afterburner for 3 s with nothing behind it.
  The older confirmation-based climb-out did the same, 3 s later still. A count
  that shows a rearm with no marker sighted in the seconds before it is not
  "after a pass"; not changed in this run.

- 20:31:15, the first pass signed under change 2: a pickup. The marker was 56 px
  from the centre a second before and 289 px at its last sighting;
  `count 4 read 1.3s after the climb-out started at the pass`.
- 20:33:37 on, not the climb-out: after the fifth round the game showed "SELECT
  AN AIRCRAFT TO RECEIVE PARTS" (110 parts, CONFIRM). Wingman does not click
  that by design (Anomaly 004: it commits parts to an aircraft, the operator's
  choice), and it was left for the operator. The run sits in `GAME_LOBBY` until
  it is chosen. Tally at that point: 5 rounds, 1 climb-out by the pass (a
  pickup), 1 by the count (the late one above), no broken approach.

- 20:48:37: the liveness guard's hard limit (900 s without progress) ended the
  second run on that screen: 44 min, 5 missions, 20 respawns. The game and `:3`
  stayed up with the parts screen showing. Its log was still `wingman.log` when
  this was written; the next launch archives it under its end time, 20:48.
  **The comparison below is not made:** the two runs together have 2 pickups
  signed by the pass, against the 60 this document asks for.

| Measure | Baseline | After |
|---|---|---|
| Session | 10-13 08:00 to 18:15, 102 missions | |
| Close passes, picked up | 76, 68 (89%) | |
| Climb-out start after the pass | median 1.4 s | |
| Altitude change at +3 s, +6 s, +9 s | -120, +143, +838 m | |
| Dive recovery within 5 s of the pass | 49% | |
| Died within 10 s, 30 s of a pickup | 25%, 43% | |
| After a miss: a pickup within 60 s | 5 of 8 | |
| Pickups an hour | 6.6 | |

## Related

- [Action Item 002](002-emergency-climb-abandons-resupply.md): the dive recovery
  waits while the resupply marker is in view, which is why the pursuit follows a
  marker down.
- [Design 005](../hldd/005-target-tracking-hldd.md), "Hold Model": the steering
  that holds the marker on the way in.
- `wingman/config.yaml`, `pursuit_mode.resupply_priority`: the measurements
  behind `deadband` and `rearm_climb_s`.
