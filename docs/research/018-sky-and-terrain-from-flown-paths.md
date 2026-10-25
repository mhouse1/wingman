# Research 018 — Sky and Terrain from the Paths Actually Flown

| Status | Date       | Wingman Version |
|--------|------------|-----------------|
| Draft  | 2026-10-25 | 1.9.3           |

## Question

Can wingman tell sky from terrain along its flight path well enough to steer clear of the ground, if it learns
the difference from where aircraft actually flew (free air) and where they crashed (terrain), instead of from
hand-set thresholds? And how does the operator, flying by hand, keep terrain out of the flight path on the same
objectives?

Operator, 2026-10-25: "the next crux is to improve terrain detection, separate sky from terrain, we can
approach this by tracking all objective flown and the before and after events review these events to
determine what is common among all these event types and compare with manually flown and how doe the manually
flown avoid terrain, possibly identify ACS mode where a free path is", then "create a new research doc to
track this approach".

## Background

### Why this approach

Every terrain lever measured up to 2026-10-25 hit the same limit: wingman cannot reliably see how close the
ground ahead is (HLDD 001, "Where it stands (2026-10-24)"; Action Item 004). Four ways of separating sky from
terrain have been scored, each with thresholds or rules set by hand:

| Attempt | Result |
|---|---|
| Phase 1 sky fraction (colour) | Below its threshold on 95% of ticks on a canyon map; 3.1% of its warnings followed by a crash |
| Phase 2 looming, shapes and the planner (ESCAPE) | 5.3% to 8.2% of warnings followed by a crash; ESCAPE limited by free air 10.0% at best |
| Terrain share by texture in a corridor ahead | On a par with the pull-out trigger on death windows (27.6% of crashes at 5.3% false alarms); adds nothing to the telemetry |
| Depth model proximity | Ranks crashes best on death windows (AUC 0.79), but in ordinary flight adds nothing to the telemetry (Research 017, "Ordinary flight") |

What none of them had is ground truth for a region of the picture. Research 017's step 3 labelled whole
windows of frames, crash or shoot-down; here the label says which part of the picture was free. It comes from
the flight itself: the part of the picture the aircraft flew through in the following seconds was free air;
the place it crashed was terrain. Every second flown gives such a label. Objectives flown through
(`OBJECTIVE: flew through ...` in the logs) are the subset where the aim point, the marker, is known.

### What exists

- **Fly-through events.** 1,664 in the logs of 2026-10-19 to 10-24. Only the recorded session of 2026-10-24
  18:18 to 21:20 (`make r1 v`, account 1, 32 missions) has them on video: 43 (24 air superiority points, 13
  resupply points, 6 priority targets).
- **The recorded session.** `logs/session_20261024_181820_acct1.mp4`, 21,897 frames at 2 a second, 960 x 600,
  each timed in its `frames.csv` (no gap over 0.67 s), with its wingman log (HUD readings, objective events,
  death cards).
- **Crash frames.** The black box's raw frames before 506 deaths (88 crashes, 417 shoot-downs), labelled, in
  `test_screenshots/terrain_dataset` (Research 017, "Step 3 results"); its `tools/` hold the labelling and
  scoring scripts.
- **Manual flight.** No video of the operator flying by hand. Research 016 compared manual flying with the
  tracker from keys and logs. The session recorder has no game-state check, so it should keep recording through
  a manual takeover (inferred from `wingman/session_recording.py`, not yet seen).

## Method

Three phases. Each is scored the way the terrain triggers have been since 2026-10-24: in ordinary flight, not
only in the seconds before deaths (Research 017 showed death windows alone mislead), against the live pull-out
trigger.

**Phase A: the objective fly-throughs, flown by wingman.** Two halves:

- *Telemetry, from the logs (no video needed).* All 1,664 logged fly-throughs: the HUD altitude, speed and path
  angle over the approach, the marker's size at the pass, and the outcome: flew clear, crashed, or shot down
  within 10 s. What the clean passes share against the crashes, at 40 times the sample the video has.
- *Pictures, from the video.* For each fly-through on video, the clip from 8 s before to 5 s after: where sky
  and terrain sit in the picture along the flight path, and when the pull-up came. Each further `make r1 v`
  session adds about 40 events.

**Phase B: the same objectives flown by hand.** The operator flies objectives with recording on (`make r1 v`,
then a manual takeover). The same clips, and the same measures, compared with Phase A: the altitude margin
kept, the approach angle, where open sky sits in the frame, and how early the pull-up comes.

**Phase C: the free path.** Labels from the whole recording, not only the fly-throughs: in the 4-hour run of
2026-10-24 only 6 of 35 crashes had an objective steering in the last 3 s (19 had none, 10 a target), so a
separation learned from objective passes alone would miss most of where crashes happen. The free-air side: the
path actually flown, carried back into the earlier frames; first only over stretches of straight flight (path
angle and heading nearly unchanged), where the path stays near the picture's centre and the chase camera's lag
matters least. The terrain side: the impacts, scarce in any one session (13 in the recorded one), so taken from
the 88 labelled crash sets in `test_screenshots/terrain_dataset` as well. From them, a sky-and-terrain
separation along the flight path, then a "free path" direction the ACS chase could steer toward.

## Measures

- Per fly-through: the outcome; the HUD altitude, speed and path angle over the clip; the marker's size and
  position at the pass; the time from the last steering toward the marker to the pull-up.
- Per frame (Phase C): the share of the region the aircraft later flew through that a candidate separation
  calls terrain (it should be none), and the share of the region around an impact it calls sky (it should be
  none).
- As a warning: alarms a mission, the share followed by a crash in 2 to 10 s, and the crashes warned 2 to 10 s
  ahead, over whole battles, against the live pull-out trigger: 2.44 a mission, 6.4%, 5 of 13 crashes in the
  recorded session alone; 5.4% to 6.5% followed by a crash over 138 missions of 10-24, recomputed from the
  readings (Action Item 004, AI-004-1).
- Its cost live: the time per picture against the steering cycle (about 0.17 s). The depth model's 2026-10-16
  shadow cost the cycle about 6%; a separation that cannot run inside the cycle cannot steer.

## Limits

- 43 fly-throughs on video, and 13 crashes in the recorded session: too few to fit anything until more
  `make r1 v` sessions are recorded.
- The path flown is known in the picture only roughly: the chase camera trails the aircraft and the flight path
  is not drawn, so carrying it back into earlier frames is an approximation (assumed: near the picture's centre
  in straight flight). It is the hardest step of the method, which is why Phase C starts with straight flight.
- Free-air labels outnumber terrain labels by thousands to one in any session; the terrain side rests on the
  impacts, the 13 of the recorded session and the 88 of the black box.
- Phase B depends on the operator's flying time, and a hand-flown pass is one way of flying it, not the only
  safe one.
- The aircraft changes between sessions (its nose-up rate differs from the 10-17 airframes by two to four
  times, Action Item 004), so what a pass looks like may depend on the jet.

## Results

None yet.

## Open Questions

- Does the recorder keep recording through a manual takeover and in standby?
- Does wingman log `OBJECTIVE: flew through` while the operator flies by hand? The fly-through is detected by
  the pursuit, which does not run during a manual takeover (inferred); if not, Phase B needs another way to find
  the passes.
- How far back can a flown path be carried into earlier frames before the camera's lag makes it wrong?
- Is the free path best found in the whole picture, or only in a corridor around the flight path?

## Next Steps

1. Phase A: the telemetry half on the 1,664 logged fly-throughs, then the 43 clips of the recorded session
   with their readings and outcomes, reviewed.
2. Record more sessions with `make r1 v`, so every fly-through has video.
3. Phase B: the operator flies a set of objectives by hand with recording on.

## References

- [HLDD 001 — Terrain Avoidance](../hldd/001-terrain-avoidance-hldd.md), "Where it stands (2026-10-24)"
- [Research 016 — Manual Flight vs Tracker Shadow Comparison](016-manual-flight-vs-tracker-shadow-comparison.md)
- [Research 017 — Camera Vision for Terrain Avoidance](017-camera-vision-for-terrain-avoidance.md)
- [Action Item 004 — Terrain Avoidance: the Levers Left After the Camera Study](../action-item/004-terrain-avoidance-levers-after-the-camera-study.md)
- Code Review 019 (`docs/code-review/019-2026-10.md`): the rows of 2026-10-24 and 10-25
