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

Operator, 2026-10-26, two cues for the same separation: terrain can be inferred from the nose angle, which
helps find "a golden path to nav to escape"; and terrain can be recognised by "objects with long straight
lines which wouldnt be sky", naming a mountain in `tests/test-output/screenshot_20261014_053706.png` and the
pipeline-like structure running from the bottom left to the top middle right of
`test_screenshots/telemetry/telemetryM_20260601_060322_spd748_alt5591_night.png`. Both are taken up under
Method, in "Two picture cues, as inputs to the existing planner", with two looks under Results.

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

### Two picture cues, as inputs to the existing planner (2026-10-26)

The escape direction the operator's two cues point to is already computed, in shadow, on the pursuit's own
frames. `PathPlanner` (`wingman/clear_path.py`, Code Review 019 step 3d) follows the gap in image space: DIRECT
to the goal, DETOUR to the nearest free air with a clear line to it, ESCAPE to "the free air nearest the flight
path, up before down". It counts a textured outline closing within 8 s as an obstacle, and smooth picture
joined to the top of the view as open (HLDD 001, "Outlines from the picture itself"). So the cues are not a
second planner. They are candidate changes to what this one counts as an obstacle and as open, each aimed at
one of the two kinds of false alarm measured on its ESCAPE. Over 498 missions of 2026-10-22 to 10-24, 6.5% of
1,206 ESCAPE episodes were followed by a crash in 10 s; of the live pull-out trigger's alarms in the 4-hour run
of 10-24, 11.8% (Code Review 019, offline row of 2026-10-24; HLDD 001). The two kinds:

- **Open sky**, a third of the episodes: with half the view free or more, 402 episodes and 2.0% followed by a
  crash. Code Review 019 read them as cloud, from 8 frames. Pre-labelled on the 34 such episodes of the
  recorded session (Results, third look), they are mostly terrain below the path (15) and HUD symbols, marker
  rings and trails (12); cloud 3. Outlines are found by texture, a cloud is textured, and in one frame its
  corners do not tell it from rock either (4.0 against 4.1 a 1,000 px). A free-air filter already removes
  this class, and without it ESCAPE reaches 8.7% (under 50% free) to 10.0% (under 25%): still under the
  pull-out trigger, so no change was made.
- **Real terrain below or beside the flight path**, the rest: ground the aircraft is passing, not flying at.

**The line cue, for the cloud class.** Ridges, cliff faces, roads, pipelines and buildings have long straight
edges; a cumulus cloud has none (the first look, and frame 16100 of the second, under Results). The count of
long straight segments inside an outline is therefore a candidate for telling terrain from cloud, which
nothing in one frame does today. Its ceiling is set by the free-air filter: the open-sky cloud alarms can
already be removed, and removing them leaves ESCAPE under the trigger. So the cue adds something only for cloud
the filter cannot remove, in views with little free air, and only where the second look says it holds. On the
recorded session's pre-labels cloud is 4 of the 105 ESCAPE episodes (Results, third look), so there it has
little to act on. It does not hold everywhere:

- A field of small broken clouds gives segments: the transform strings many small cloud edges into crossing
  lines.
- The own aircraft's wings and fins, its wingtip trails, missile trails and the HUD's symbols are long straight
  lines in or near the middle of the view, where the planner looks. The trails cross open sky.
- Lit, textured rock and ice give many segments, dark rock and night ground few. At the setting that finds
  ridges, the segments are the same edge texture the outlines are already drawn from.
- The own aircraft cannot be cut out by a fixed region: ACS flies any airframe (operator, 2026-10-03), and the
  aircraft banks on screen, so the static-edge mask that removes the HUD does not hold it either. It has to be
  found at run time.
- A line says where terrain is, not how near: a pipeline 1,700 m below is as clear as one 100 m ahead.

**The horizon, for the terrain class.** The pilot's rule: terrain that stands above the horizon is higher than
the aircraft; terrain whose top stays below it is lower, and is passed over in level flight. An outline wholly
below the horizon is then ground being passed, and one that rises through it has to be climbed over or turned
away from. The planner treats the two alike today. The horizon would also replace the planner's assumption
that the sky joins the top of the view, which fails in a steep dive, in a loop, and in a held bank, where the
camera rolls the sky to the side of the picture (Results, third look).

Where the horizon is, and which side of it is sky:

- **From the picture, where the sea horizon is in view.** It is a long straight line (row 458 of 1,200 in the
  day picture under Results).
- **Otherwise from the telemetry's flight-path angle** (`telemetry.pitch_angle_deg()`, the asin of altitude rate
  over speed): one row for each angle, once the camera's vertical field of view and its tilt below the nose are
  fitted on frames where the sea horizon and the angle are both known. The angle is the velocity's direction,
  not the nose's (they differ by the angle of attack, most in hard turns and at low speed), the camera lags the
  nose in a manoeuvre, and the reading is averaged over the HUD cadence and misread at times (Code Review 019
  cycle 35). So the row is a band, not a line, and it holds only with the wings near level (the roll, below).
  On the recorded session the fit could not be made: one frame of pursuit flight shows a clear sea horizon,
  with no path angle logged near it (Results, third look).
- **Which side is sky, from the picture only.** A path angle cannot tell upright from inverted: over the top of
  a loop it reads level while the picture is upside down. That is how the emergency climb of 2026-10-04 02:48
  carried the aircraft over the top and into the ground (HLDD 001).
- **The roll.** Wingman does not read it, and the chase camera does not keep the horizon level: in a held bank
  it rolls with the aircraft, far enough to tilt the ground's edge 60 to 70 degrees from level (Results, third
  look). The aircraft's bank on screen is the part of the roll the camera has not taken, not the roll. So
  while the aircraft turns the horizon is not a row, and has to be found in the picture at whatever tilt it
  has.

The horizon taken from the telemetry adds nothing on its own: it is the telemetry, which the pull-out trigger
already reads. Its use is as a reference line for the outlines in the picture.

**What neither cue changes.** Distance: HLDD 001 names how high the ground ahead is as the missing input, and
Research 017 found no picture measure that adds to the telemetry in ordinary flight. And the controls: Code
Review 019 found on 2026-10-15 that 343 of 353 overnight deaths ended a pursuit, where a terrain verdict
reaches the controls only through the recovery of
[ADR 148](../adr/148-a-dive-recovery-flies-through-a-pursuit.md), which does not start on a level or climbing
path and is held off while a target or objective is in view. A better planner changes what wingman does only
once something steers by it.

## Measures

- Per fly-through: the outcome; the HUD altitude, speed and path angle over the clip; the marker's size and
  position at the pass; the time from the last steering toward the marker to the pull-up.
- Per frame (Phase C): the share of the region the aircraft later flew through that a candidate separation
  calls terrain (it should be none), and the share of the region around an impact it calls sky (it should be
  none).
- The line cue: on the planner's ESCAPE episodes, the long straight segments inside each outline, cloud
  outlines against terrain outlines (labelled by eye), with the own aircraft, its trails and the HUD left out;
  and how many cloud alarms it removes that the free-air filter does not.
- The horizon: the row from the path angle against the sea horizon's row where both are known, in pixels, by
  path angle; and on the ESCAPE episodes on terrain, the share whose outline lies wholly below the horizon,
  false alarms against those before a crash.
- Either cue in the planner: the share of ESCAPE episodes followed by a crash 2 to 10 s later, against the
  live pull-out trigger on the same missions, and against the 15% to 20% at which letting ESCAPE act was to
  be tried (Code Review 019).
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
- The line cue's gain is bounded: the cloud alarms it is aimed at are mostly in open sky, where the free-air
  filter already removes them, and ESCAPE without them is still under the pull-out trigger (Method).
- At the setting that finds ridges the line cue finds rock texture, the measure the outlines are already drawn
  from and that added nothing as a warning ("terrain share by texture" under Background).
- The own aircraft, its trails and the HUD's symbols carry many of the segments in the middle of the view
  (Results). The own aircraft has to be found at run time, not cut out by a fixed region: its shape changes
  with the airframe, and it banks on screen.

## Results

### First look at the line cue (2026-10-26, two pictures)

Two pictures, the two the operator named, so examples of behaviour and not a rate. OpenCV on VEDA: grey,
contrast-limited equalisation (clip 3, 8 x 8 tiles), 5 x 5 blur, Canny 50/150, then probabilistic Hough
(1 px, 1 degree, 50 votes, gap 25 px), with the HUD's top and bottom bars, the minimap and the two tapes
blanked by hand. Segments coloured by angle: red diagonal (15 to 75 degrees), yellow near horizontal, magenta
near vertical.

| Picture | Shortest segment kept | Segments | Where they fell |
|---|---|---|---|
| Day, mountains (10-14 05:37:06, 1,354 KPH, 1,917 m) | 6% of the width | 894 | 0.1% of line pixels in the sky, which is 23% of the picture (hand-drawn mask); 5 segments on the sea horizon |
| same | 10% of the width | 278 | none in the sky |
| Night, pipeline (06-01 06:03:22, 748 MPH, 5,591 ft) | 12% of the width | 4 | 3 along the pipeline from the bottom left toward the middle, 1 on the INCOMING banner |
| same | 6% of the width | 246 | the pipeline over its whole length, including its turn toward the top middle right; the trench on the right; the lock circle and HUD text |

Pictures with the segments drawn on (6% setting):
[day, mountains](018-sky-and-terrain-from-flown-paths/lines-day-mountains-20261014-053706.jpg),
[night, pipeline](018-sky-and-terrain-from-flown-paths/lines-night-pipeline-20260601-060322.jpg).

What they show (measured on these two, not beyond):

- **Sky gave no lines.** In the day picture the sky above the sea horizon, clouds included, held 0.1% of the
  line pixels at the loose setting and none at the stricter one. Every segment was on terrain, the horizon,
  the HUD or the own aircraft. Not so on the recorded session's frames: the second look below found segments
  on broken cloud and on wingtip trails in open sky.
- **The pipeline is found as the operator described.** At the strict setting three of the four segments in the
  whole picture lie along it, at 34 to 39 degrees. At the loose setting it is traced from the bottom left
  corner to the middle and up its turn toward the top.
- **The mountain is found as an area of lines, not as its outline.** The ridge silhouettes are long but not
  straight, so at the strict setting they give nothing (the two segments found were rock strata on the right
  mountain), and at the loose setting they give short diagonals along the ridges among many more segments of
  rock texture. Either way the lines sit on the mountains and not in the sky, which is what the cue needs; a
  ridge outline as such would need a different detector (a sky-to-terrain boundary, not a straight line).
- **The sea horizon is a line.** Five segments on it at row 458 of 1,200: wings level, 142 px (12% of the
  height) above the picture's centre. That is one measurement of the level-flight offset the horizon cue
  needs, if the path angle at that moment was near zero, which this screenshot cannot say (it has no log).
- **False positives:** the own aircraft's fins and wing edges, the lock circle, the INCOMING banner and the
  kill feed text, all in the picture's centre or on the HUD. The right wingtip's contrail in the day picture
  (long, straight, from the wing to the bottom right corner) runs through a patch of diagonal segments there;
  whether they are the contrail or the rock behind it is not separated. It lay over terrain in this picture,
  but over sky a contrail would be a line in the sky.
- **Cost:** 9.0 ms a picture on the day frame and 6.1 ms on the night frame at 960 x 600 (gap 12 px), one
  thread, against a steering cycle of about 0.17 s: about 4% to 5%, inside the cycle.

The horizon cue has not been tried: neither picture has a logged path angle. On the night picture the
ground fills the whole frame, so the horizon is above its top.

### Second look: ten battle frames from the recorded session (2026-10-26)

Sixteen frames of `logs/session_20261024_181820_acct1.mp4`, one every 1,300 from frame 500, at the recording's
own 960 x 600, the width the live perception works at; ten show a battle under the own HUD, the rest menus,
results and a death. The detector of the first look, with a gap of 12 px and the shortest segment 6% of the
width. Where the segments lie is by eye.

| Frame | Segments | Where they lie |
|---|---|---|
| 500 | 18 | The own aircraft and a wingtip trail; one near the top; none on the dark cloud and ground |
| 1800 | 549 | Lit, icy canyon walls over the whole view; the own aircraft |
| 7000 | 34 | The own aircraft; the GUNS WARNING and LOCK WARNING banners; none on the haze |
| 8300 | 81 | The own aircraft; a lit rock outcrop (the densest); the left wingtip trail; HUD text |
| 9600 | 57 | The own aircraft, along its whole wing; dark rock on both sides; a wingtip trail |
| 10900 | 48 | The own aircraft; the left wingtip trail; the islands; the kill feed |
| 12200 | 125 | The own aircraft; a lit cliff on the right (the densest); the lock circle and name tags |
| 16100 | 12 | All on the own aircraft; none on the cumulus filling half the view |
| 18700 | 46 | The own aircraft; the INCOMING banner; two clusters on small broken clouds in open sky |
| 20000 | 88 | The own aircraft; HUD text; some on the dark ground and its structures |

Pictures:
[frames 7000, 8300, 10900, 18700](018-sky-and-terrain-from-flown-paths/lines-battle-frames-a-20261024.jpg),
[frames 500, 1800, 9600, 12200, 16100, 20000](018-sky-and-terrain-from-flown-paths/lines-battle-frames-b-20261024.jpg),
[the broken cloud of frame 18700, three times enlarged](018-sky-and-terrain-from-flown-paths/lines-broken-cloud-20261024-frame18700.jpg).

What they show (ten frames, examples and not a rate):

- **The own aircraft carried segments in all ten**, and most of them in 500, 7000 and 16100. It sits in the
  middle of the view, where the planner looks.
- **Wingtip trails are lines in the air.** Four frames had segments on one (500, 8300, 9600, 10900); in 7000
  and 18700 the trails were not found. They are long, straight, and cross open sky in hard turns.
- **Cloud is not always free of lines.** None on the cumulus of 16100 or the haze of 7000, but two clusters on
  a field of small broken clouds in 18700: the transform strings many small cloud edges into crossing lines.
- **Terrain gives segments where it is lit and textured** (1800, 8300, 12200, densely), few on dark rock and
  night ground (9600, 10900, 20000), none on the dark ground of 500.
- **HUD symbols beyond the hand-blanked bars and tapes**: banners, the lock circle, name tags and the kill feed,
  in six of the ten.
- **The camera's roll (frame 10900).** The aircraft is banked about 25 degrees on screen while the rock spires
  stand upright. Inferred: the chase camera keeps the horizon close to level. One frame. Not so in a held
  bank: see the third look.

### Third look: the chase camera, and the ESCAPE episodes pre-labelled (2026-10-26)

From the recorded session's pursuit flight: the 13,039 frames with a `PATHVIEW` reading within 0.3 s.

**The camera rolls with the aircraft.** Sixteen frames with a second or more of roll key held in the 3 s
before, viewed:

- Six show the ground at the side or top of the picture with the aircraft banked: 2626 (a snow ridge down the
  right side), 6582, 6586, 7668 and 7672 (a sunset canyon, the ground's edge 60 to 70 degrees from level),
  16059 (the ground top left). In 7672 the aircraft is banked about 35 degrees on screen while the ground's
  edge is tilted 60 to 70 degrees: the camera has taken most of the roll.
- Seven show no horizon to judge (night, straight down, flares, the respawn screen); three show it roughly
  level, with the wings near level too.
- Frame 10900 of the second look (aircraft banked about 25 degrees, spires upright) then reads as a camera
  that follows the roll with a lag, or takes only part of it. Which of the two is not separated.
- The roll key held before a frame says little about the bank in it: frames with 1.2 to 2.0 s of net roll
  held show the wings level (2809, 3254, 3575).

[Frames 6582, 6586, 7668, 7672](018-sky-and-terrain-from-flown-paths/camera-rolls-with-the-aircraft-20261024.jpg).

**The field-of-view fit cannot be made on this recording.** 984 of the 13,039 frames have a long
near-horizontal line (60% of the width or more, within 30 degrees of level, rows 42 to 450). Viewed, 16 of
them: the far edge of the terrain below distant mountains, ridge and slope lines, a video transition and the
DEFEAT screen. A brightness test (smooth, brighter sky above the line, darker ground below) leaves 3: one clear
sky-over-sea horizon (frame 2944) and two night frames where the line is not clearly the horizon. None has a
path angle logged within 0.5 s.

**The 105 ESCAPE episodes, pre-labelled by eye** from the review sheets, by what the outline at the flight
path is: `terrain_ahead` (the path points into terrain: a wall, a peak, the ground in a dive),
`terrain_below` (terrain under the path), `terrain_beside` (a canyon wall or cliff beside the path), `cloud`
(cloud, fog, a sunset sky), `hud_trail` (the own aircraft, HUD symbols, marker rings, banners, missile and
wingtip trails, explosions). 41 are marked for the operator to check (the `check` column of `labels.csv`); my
labels as first written are kept in `prelabels_claude_20261026.csv` beside it.

| Label | Under 25% free | 25 to 50% | 50% or more | All |
|---|---|---|---|---|
| terrain ahead | 30 | 16 | 4 | 50 |
| terrain below | 3 | 10 | 15 | 28 |
| HUD and trails | 0 | 3 | 12 | 15 |
| terrain beside | 1 | 7 | 0 | 8 |
| cloud | 0 | 1 | 3 | 4 |
| All | 34 | 37 | 34 | 105 |

What the pre-labels show, before the operator's check, and with no outcome attached (whether an episode was
followed by a crash is step 3's to add):

- The open views (half free or more) are mostly terrain below the path and HUD symbols and trails, not
  cloud.
- Several HUD outlines move with the picture, so the learned static mask cannot remove them: the resupply
  marker's ring (episodes 4, 46, 67, 73), the lock circle (58, 69), the INCOMING banner (75), missile trails
  and explosions.
- With under a quarter of the view free, 30 of 34 are terrain ahead.

### Fourth look: the cues scored on the recorded session (2026-10-26)

`tools/escape_score.py`, beside `escape_review.py`, uses the dataset tools' own definitions
(`score_video.scan` and `report`). An episode's outcome is the first death within 10 s of its start, a crash
or a shoot-down by its `DEATH CARD` line, timed at the last HUD reading plus 0.75 s. The rates are a
mission's count of episodes (5 s apart), the share followed by a crash 2 to 10 s later, and the crashes warned
2 to 10 s ahead. They reproduce the live pull-out's figures for this session (2.44 a mission, 6.4%, 5 of 13).
Scored on the pre-labels, 41 of them unchecked. 32 missions and 13 crashes are under the 40 missions this
project trusts, so these are examples of behaviour, not rates. Each episode's outcome is in the review
folder's `outcomes.csv`.

| Episodes | A mission | Followed by a crash 2 to 10 s later | Crashes warned 2 to 10 s ahead |
|---|---|---|---|
| Live pull-out alarms (the bar) | 2.44 | 6.4% | 5 of 13 |
| ESCAPE, all | 3.28 | 2.9% | 3 of 13 |
| ESCAPE, free air under 50% | 2.22 | 4.2% | 3 of 13 |
| ESCAPE, free air under 25% | 1.06 | 5.9% | 2 of 13 |
| Line cue's ceiling: every cloud episode removed | 3.16 | 3.0% | 3 of 13 |
| Horizon cue's ceiling: every terrain-below episode removed | 2.41 | 3.9% | 3 of 13 |
| Both ceilings | 2.28 | 4.1% | 3 of 13 |
| Any cue's ceiling: terrain ahead only | 1.56 | 6.0% | 3 of 13 |

| Label | Crash 2 to 10 s later | Shot down within 10 s | Nothing | All |
|---|---|---|---|---|
| terrain ahead | 3 | 3 | 44 | 50 |
| terrain beside | 0 | 2 | 6 | 8 |
| terrain below | 0 | 3 | 25 | 28 |
| HUD and trails | 0 | 3 | 12 | 15 |
| cloud | 0 | 2 | 2 | 4 |

No episode was followed by a crash under 2 s later. The three followed by a crash are episodes 6, 13 and
98, all terrain ahead; 6 and 98 are among the 41 to check.

What it shows:

- **Neither cue lifts ESCAPE to the pull-out trigger, even working perfectly.** Every episode followed by a
  crash is terrain ahead, so removing all cloud or all terrain-below episodes removes only episodes no crash
  followed: 3.0% and 3.9%. Keeping terrain ahead alone, a perfect separation of what ESCAPE sees, gives 6.0%
  and warns 3 of 13 crashes, on a par with the trigger's 6.4% and warning fewer.
- **What limits ESCAPE lies elsewhere.** 47 of the 50 terrain-ahead episodes were followed by no crash
  within 10 s. Some of those will be threats the pursuit or the recovery flew out of, which this scoring
  cannot tell from false alarms. And in 6 of the 13 crashes the planner was not running at all in the 10 s
  before (no `PATH` plan), so ESCAPE could not warn them; of the 7 where it ran, it warned 3.
- **The line cue as built does not separate cloud from terrain.** Straight segments in the outline at the
  flight path: cloud a median of 25 (0 to 34), terrain below 29 (2 to 312), terrain ahead 140 (3 to 332),
  HUD and trails 12 (0 to 61). Keeping only outlines with 20 segments or more removes 25 episodes (10 HUD and
  trails, 9 terrain below, 4 terrain ahead, 2 cloud), none followed by a crash, for 3.8%. It removes no cloud
  episode the free-air filter keeps: the only cloud episode under 50% free air (35) has 34 segments.
- **The horizon cue as built cannot be scored:** the horizon cannot be placed on this recording (third look).
  Its row in the table is the ceiling the labels give.

## Open Questions

- Does the recorder keep recording through a manual takeover and in standby?
- Does wingman log `OBJECTIVE: flew through` while the operator flies by hand? The fly-through is detected by
  the pursuit, which does not run during a manual takeover (inferred); if not, Phase B needs another way to find
  the passes.
- How far back can a flown path be carried into earlier frames before the camera's lag makes it wrong?
- Is the free path best found in the whole picture, or only in a corridor around the flight path?
- How far does the horizon from the path angle stray from the sea horizon where both are known, and does it
  stray most in turns, where the angle of attack and the camera's lag are largest?
- How does the chase camera roll with the aircraft: a lag that catches up, or a fixed share of the roll? Does
  it follow the nose in pitch?
- Does the line cue tell cloud outlines from terrain outlines in views with little free air, where the free-air
  filter cannot?

## Next Steps

1. Phase A: the telemetry half on the 1,664 logged fly-throughs, then the 43 clips of the recorded session
   with their readings and outcomes, reviewed.
2. Record more sessions with `make r1 v`, so every fly-through has video.
3. Phase B: the operator flies a set of objectives by hand with recording on.
4. The line cue on the planner's cloud alarms, offline. The recorded session's log
   (`logs/wingman_20261024_212050.log`) has 285 ESCAPE plans in 105 episodes 5 s apart, all on video. Label
   each episode's outline cloud or terrain by eye, and count the long straight segments inside each, with the
   own aircraft, its trails and the HUD left out. Review sheets for all 105 (2026-10-26): the raw frame beside
   the planner's split, free air blue, objects orange, the picture it does not analyse grey and the straight
   segments white, with the counts and an empty `label` column in `labels.csv`, in
   `test_screenshots/terrain_dataset/escape_review/20261024_181820/` (made by `tools/escape_review.py`). Run
   again on the recording's pictures, the perception gives the free air the live planner logged within 0.039
   on average (7 of 105 differ by more than 0.10). Pre-labelled 2026-10-26 (Results, third look): 41 of the
   105 for the operator to check.
5. The camera's roll, from the recorded session: on frames with rock spires or a sea horizon in view, the
   aircraft's bank on screen against the tilt of the spires or the horizon. Done as far as the recording
   allows (Results, third look): the camera rolls with the aircraft. The field-of-view fit needs a recording
   over the sea with the path angle logged near every frame; this one has one clear sea horizon in pursuit
   flight, with no path angle near it.
6. A cue goes into the planner, in shadow, only if it lifts ESCAPE above the live pull-out trigger on the same
   missions. On the recorded session neither does, even working perfectly (Results, fourth look). Before
   the cues are set aside: score again on the operator's checked labels (`escape_score.py` reads
   `labels.csv`), and on a second recorded session.

## References

- [HLDD 001 — Terrain Avoidance](../hldd/001-terrain-avoidance-hldd.md), "Where it stands (2026-10-24)"
- [Research 016 — Manual Flight vs Tracker Shadow Comparison](016-manual-flight-vs-tracker-shadow-comparison.md)
- [Research 017 — Camera Vision for Terrain Avoidance](017-camera-vision-for-terrain-avoidance.md)
- [Action Item 004 — Terrain Avoidance: the Levers Left After the Camera Study](../action-item/004-terrain-avoidance-levers-after-the-camera-study.md)
- Code Review 019 (`docs/code-review/019-2026-10.md`): the rows of 2026-10-24 and 10-25
