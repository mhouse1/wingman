# Research 017 — Camera Vision for Terrain Avoidance

| Status | Date       | Wingman Version |
|--------|------------|-----------------|
| Draft  | 2026-10-19 | 1.9.3           |

## Question

Can wingman see terrain well enough, from the game picture alone, to keep the
chase clear of it, instead of pulling up at the last moment? And what does it
take: compute, game settings, data, hardware?

The operator set the order on 2026-10-19: the cheapest step first, each step
judged before the next one is paid for.

## Background

### Why: the crashes are the chase flying into ground it cannot see

Measured on six logs of 2026-10-19 (MiG-31, JA37, Su-30; 119 kill-card
crashes; Code Review 019):

- 117 (98%) came during a chase (`GAME_BATTLE_EJECT`). The chase steers at
  targets and objectives with no terrain constraint; the emergency climb only
  reacts when an impact is seconds away.
- In 23 (19%) the tracked target sat below the screen's centre: the chase was
  pointing the nose down at it.
- In 30 (25%) the jet flew level or shallow at 900 m or more into ground. HUD
  altitude is above sea level, so no altitude alarm sees that ground.
- The clear-path planner, in shadow, reported `DIRECT` (61) or `BLIND` (22) in
  the 8 s before most of them: it saw a clear path.

### What exists

Camera detectors, all in shadow except the agreement trigger, scored on the
crashes no altitude alarm or climb preceded (193 missions of four airframes):

| Detector | Warned 1 to 8 s before | A mission | A crash 1 to 8 s after |
|---|---|---|---|
| LOOM (the picture growing) | 22 of 60 | 3.6 | 14% |
| Two of LOOM, SHAPE and ESCAPE within 1 s (live since cycle 40) | about a third | 2.3 to 2.6 | 9% to 19% |
| Sky fraction in the forward box | about a sixth | 6.7 to 7.7 | 2% to 3% |

The depth view (`wingman/depth_view.py`, `wingman/depth_worker.py`, Code
Review 019 cycle 11): Depth Anything V2 Small in a worker process of its own,
252 px pictures, 95 to 200 ms each on 4 CPU threads. Off since 2026-10-16
(operator): its `mid_rank` figure was no higher before deaths than in random
windows (a maximum of 0.9 or more in 21% of both), and it slowed the steering
cycle about 6% (median 0.172 s to 0.182 s).

**The prior result to beat:** speed was not the only problem. The figure taken
from the depth did not warn. A faster model only helps with a figure that
does.

### The machine (measured 2026-10-19)

- Graphics: Intel Arrow Lake integrated graphics (`00:02.0`), no discrete
  card. The game renders on the same chip.
- CPU 20 cores, 30 GB of memory; wingman already keeps about 4 cores busy
  (411% CPU) with the OCR.
- torch 2.10.0+cu128: a CUDA build, with no CUDA device (`cuda.is_available()`
  is false).
- Intel GPU compute: not installed. Present are `intel-media-va-driver` (video
  decoding) and `ocl-icd-libopencl1` (the OpenCL loader); missing are Intel's
  compute runtime (`intel-opencl-icd`) and Level Zero (`libze-intel-gpu1`),
  which OpenVINO's GPU device needs. `/etc/OpenCL/vendors/` is empty.

## Method

Four steps, cheapest first. Each has a pass mark; a step that fails decides
whether the next one is worth its cost.

1. **The Intel chip, through OpenVINO (free).** Convert the depth model and
   time it on OpenVINO's CPU and GPU devices at 252 and 518 px, alone, then
   while the game renders a battle. Needs: the `openvino` package (a uv
   dependency) and the Intel compute runtime (a system package: the operator
   installs or approves it). **Pass:** 100 ms or less a picture at 252 px
   during a battle, the game's own average frame rate no lower, map for map,
   than in matches without the model, and the OCR no slower (telemetry OCR
   median 0.45 s or less). (The first plan asked for 50 FPS or more a match;
   without any model the game averaged 28.6 to 49.6 FPS on Stone Forest, see
   step 2's results, so a fixed mark would fail on the map alone.)
2. **Game settings (free).** Turn off motion blur and similar effects if the
   game has them. Find out whether it has a cockpit or forward camera: in the
   chase view the jet's own body covers the lower middle of the picture, where
   the ground shows in a dive. **Pass:** the forward view clear of the own
   aircraft without breaking the HUD crops (`make calibrate` re-checks them).
3. **Recorded flights and a figure that warns (run time).** Record several
   hours with `make r1 v` (session video and decision trace). The logs time
   every kill-card crash, so frames before crashes and frames of safe flight
   label themselves. Design and score depth figures offline on them. **Pass:**
   better precision than the agreement trigger (19%) at its coverage, or more
   of the unwarned crashes at its alarm rate.
4. **An NVIDIA graphics card, if step 1 fails.** RTX 4060 class, 8 GB or
   more. The installed torch is already the CUDA build. Expected (not
   measured here): the depth model fast enough to steer with, and the OCR
   several times faster, which also freshens the alarms. A second card next
   to the nested display may need desktop setup.

## Measures

- Milliseconds a picture, per device and size; the spread (median, p90).
- The game's own average frame rate for each match, with and without the
  model running: the game saves it, and wingman logs it as
  `GAME STATS: ... avg_fps=` (step 2's results). The FPS counter at the
  bottom left of the screen is the fallback.
- Wingman's own timings: telemetry OCR median (`run_*.json`), the pursuit's
  steering cycle (median 0.172 s without the depth view).
- For step 3: coverage of the crashes no altitude alarm saw, alarms a mission,
  share followed by a crash, against the table above.

## Limits

- Relative depth only: the model orders the picture near to far, it does not
  give metres. A warning has to come from change over time or from where the
  near terrain sits against the flight path.
- The flight path is not the bore sight. In a dive the path points below the
  nose; the path angle from telemetry places it on the screen only roughly.
- Night maps (Crimson Canyon Night) and cloud may defeat any picture-based
  method.

## Results

### Step 1

**The CPU half (measured 2026-10-19 14:17 to 14:25, nothing else running).**
Depth Anything V2 Small on the analysed view of
`test_screenshots/GAME_BATTLE_ENEMY_AT_NOSE_DOWN.png` (the 0.20 to 0.80 by
0.12 to 0.60 box, which the processor sizes to 252 x 504 or 518 x 1036),
milliseconds a picture, the median of 30 (p90), through a temporary
`uv run --with openvino` (no project dependency added):

| Input | torch, 4 threads | OpenVINO CPU, all cores | OpenVINO CPU, 4 threads |
|---|---|---|---|
| 252 x 504 | 114 (119) | 91 (98) | 101 (103) |
| 518 x 1036 | 748 (751) | 457 (476) | 653 (660) |

OpenVINO's output matches torch's to 0.001%. At equal threads it is about 12%
faster; most of its lead with all cores is the cores, which wingman's OCR
needs. The CPU path gives no real headroom: the question is the Intel chip.

**The GPU half: blocked.** OpenVINO 2026.4.1 lists only `CPU`: the compute
runtime is missing. Ubuntu 26.04.1 has it: `intel-opencl-icd` and
`libze-intel-gpu1` 26.05.37020.3-1, `libze1` 1.28.2-2, and `clinfo` to check.
The render device already grants the operator's account read and write
(`getfacl /dev/dri/renderD128`: `user:michael:rw-`), so no group change is
needed.

### Step 2: what the game offers (read with the game closed)

Measured 2026-10-19 16:37 to 17:04 from the game's English text file
(`loc-en-US` in `Metalstorm_Data/content-bundle`), the prefixes' registries
and the game's own log (`Player.log`). The game was not opened.

- **No cockpit view, no motion blur.** No settings label names a cockpit, a
  first-person view or motion blur. The chase camera, with the jet's own body
  in the lower middle of the picture, is the only flying view.
- **Camera settings:** Camera Distance, Field of View, Zoomed Field of View,
  Cannon Auto-Zoom, Padlock Camera Toggles, and a look-ahead: "How far the
  camera will automatically look in the direction of a turn" ("Axis Cursor:
  Look Ahead: Max Angle"; whether it applies to wingman's keyboard control is
  not known).
- **Graphics settings:** Visual Quality (Very Low to Ultra), Post Processing
  (Bloom, Color Grading), Depth of Field Quality, Use High Quality Effects,
  High Geometry Detail, Shadows, Two-Sided Shadows, Texture Quality, Scene
  resolution scale, Max FPS, vertical sync and an automatic quality
  adjustment.
- **Where the values are kept.** The registry holds the quality level only
  (`UnityGraphicsQuality` 5: Ultra, if the six levels count from 0). None of
  the camera or effects settings is in it: they are at their defaults or kept
  with the account, so their values have to be read from the settings screen.
- **The game measures its own frame rate.** It saves the last match's average
  frame rate, bad frames and ping in the registry. Account 1, wingman's last
  match (13:32): 55.4 FPS, 0.010 bad frames (units not known), 16 ms ping.
  Account 2: 58.6 FPS. The main prefix: 60.0 FPS. Account 1's figures reached
  the file 24 s after the game wrote them, before the game closed, so wingman
  can log every match. It now does (`GAME STATS:` lines,
  `wingman/game_stats.py`), and step 1's frame-rate pass mark is measured from
  them, a match at a time. **Live from 2026-10-19 18:58** (the operator's
  `make r2`, account 2, no model running): the line for each match came
  within 15 s of wingman seeing its end screen, twice slightly before it.
  Countdown 59.4 FPS; Stone Forest 47.0, 42.2,
  28.6 (4.8% bad frames) and 49.6 FPS. The game is already under 50 FPS on
  Stone Forest with wingman alone.
- **Cannon Auto-Zoom does not show in the picture (hypothesis rejected).** If
  it were on, the camera would zoom in when the gun fires and out on release,
  and LOOM's frame-to-frame zoom would show it. Over five logs of 2026-10-19
  (107 missions, 7.1 h of chase, 2,247 gun holds), 26.5% of the picture pairs
  in the 1.5 s after a press grew by more than 1%, against 27.0% elsewhere in
  battle: no zoom-in. After releases, shrinking pairs were 10.5% against 4.9%,
  but growing pairs rose too (30.6%), the mark of manoeuvring rather than of a
  zoom. LOOM warnings do start at gun presses (52 in the first 2 s of a hold,
  10.5% of chase time, against 326 outside holds, 76.8% of it: the same rate);
  the rest of a hold is quiet (1 in 12.7% of chase time), steady flight at a
  centred target.
- **What this means for the vision steps.** The jet's own body stays in the
  forward view. Camera Distance can shrink it and Field of View can widen what
  shows below the nose, but both change the picture every camera detector and
  the icon steering were tuned on, so changing either means retuning them. The
  HUD is drawn over the picture and should not move (`make calibrate` would
  confirm). The look-ahead matters to anything that takes the picture's centre
  as the flight direction.
- **For step 3: the game's log names the map.** When a map loads, `Player.log`
  lists the map's collision meshes (`stoneforest_valley_wall_west_03_col`), so
  it records the map of every match, including those that skip the
  matchmaking screen. Most of its lines carry no time, and some mesh names do
  not carry the map's name: Crimson Canyon Night's are `admin`, `bridge`,
  `outskirt`, `quonset` and `river`. On account 1's session of 11:29 to 13:33
  its 19 map loads (Stone Forest 4, Crimson Canyon Night 5, Countdown 5,
  Crucible 5) agree in order with wingman's 18 `MATCH:` reads; the
  nineteenth, the Su-30's first match, skipped the matchmaking screen. The
  maps came in blocks of about five matches (Stone Forest 11:32 to 11:55,
  Crimson Canyon Night 12:04 to 12:28, Countdown 12:35 to 12:56, Crucible
  13:02 to 13:28), so a comparison between maps is also one between times of
  the session.

### Step 3: data on hand

Eight session videos of 2026-10-04 (13:12 to 18:59, account 1, 1.5 GB) are
in `logs/` with their BT traces, but the wingman logs that time each crash only
go back to 2026-10-11 (94 files): those videos have no crash labels. Step 3
needs new `make r1 v` sessions.

**Checked 2026-10-19 18:05 to 18:10** what the frames already on disk can
give:

- **The 2026-10-04 videos hold 7 deaths in about 5.8 hours** (their traces'
  `RespawnWait` transitions): that day flew the survey mission. Too few to
  label from, whatever their causes.
- **The black box cannot be used for pictures.** It keeps the last 18 s
  before each death (`test_screenshots/path_blackbox`, three frames a second,
  about 18 deaths within its 1,000-file budget), but its frames are the live
  HUD's: the planner's free-air fill and outline cover the analysed box. The
  session video is the game's own picture.
- **The session video's timeline ran about 5% slow.** The recorder waited a
  full period after each write, so every grab stretched the period. On the
  13:12 video the respawn screen of the deaths the trace put at 559.0,
  2,193.9 and 10,050.9 s first showed at 527.0, 2,082.5 and 9,571.0 s of
  video: ratios 1.061, 1.054 and 1.050, the trace's lag after the impact
  taking a few seconds, differently each time. Taken at their nominal times,
  frames "1 to 8 s before a crash" would have come from minutes away in a
  long session. **Fixed (2026-10-19):** the recorder paces frames on an
  absolute schedule and writes each frame's wall-clock time to
  `logs/session_<id>.frames.csv`, which lines a frame up with the wingman
  log's crash times. The same slow grab gave 0.281 s between frames on the
  old recorder and 0.200 s on the new one, at 5 frames a second
  (`tests/test_session_recording.py`).

So step 3's data starts with the next `make r1 v` session.

**And the black box now keeps the game's own picture (2026-10-19 20:00).**
With `hud.black_box.raw` (on in `config.yaml`), each death writes, beside the
annotated frames, the same frames as the game drew them: 960 x 600, three a
second, the last 18 s, in `test_screenshots/path_blackbox/raw` under the
annotated frames' names, budgeted at 20,000 files (about 370 deaths). Every
session leaves them, `v` or not, and each frame is named for its moment, so
nothing has to be lined up. The log's `BLACK BOX:` line names the files and
the `DEATH CARD:` line a few seconds later labels them crash or shoot-down; a
shoot-down's frames are the comparison set. Crashes are about one death in
five, so a few hundred crashes take a few long sessions.

### Step 3 results (2026-10-24, wingman 1.9.3): the depth model adds to the telemetry

Operator, 2026-10-24: "update hldd1 then proceed with steps 1 and 2" (HLDD 001, "Where it stands
(2026-10-24)"). Everything below is measured offline on the black box's raw frames; nothing flew.

**The data.** The black box's raw store held 506 deaths' frames (2026-10-23 20:32 to 2026-10-24 15:00, 20,000
files). Its budget was full and the running session deleted the oldest set at each death, so the files were
hard-linked into `test_screenshots/terrain_dataset/raw` (git-ignored, outside every capture budget). Each set
was labelled from its log: 88 crashes (`DEATH CARD: no kill card`), 417 shoot-downs, 1 unread. The impact is
timed half a reading interval (0.75 s, assumed) after the last HUD reading: the respawn screen is read 6.5 to
7 s after the impact, so each set's last frames are already past it. The scripts and their outputs are beside
the frames (`tools/label_sets.py`, `measure_texture.py`, `measure_depth.py`, `score.py`, `combine.py`).

**The test.** A warning counts when it is up 2 to 6 s before the impact. Crashes give the hit rate;
shoot-downs, combat flight from the same sessions that did not end in the ground, give the false-alarm rate.
The live pull-out trigger (its logged alarms) is scored on the same windows: it caught 26.4% of the 87 crashes
at 5.3% false alarms. Both measures look at a corridor around the flight path: x 330 to 630, y 110 to 300 of
the 960 x 600 frame, between the HUD's ladders and above the jet's tail fins.

- **Measure 1, terrain share by texture** (the share of the corridor whose 15 x 15 mean Sobel gradient
  exceeds 8; 12,901 frames in 27 s). Area under the curve 0.70. At 5.3% false alarms it catches 27.6% (the
  corridor 99.9% textured), on a par with the pull-out. Its growth over 1 s does not separate (0.46 to 0.59).
- **Measure 2, proximity from the depth model** (Depth Anything V2 Small on the whole frame at 252 px, as
  `depth_view` loads it; 8,853 frames in 35 min at 2 threads and nice 19, 272 ms a frame). Each pixel's
  proximity is its output between the sky (the frame's 1st percentile) and the jet's own body (the 95th
  percentile of x 400 to 560, y 400 to 560): about the camera's distance to the jet over its distance to the
  terrain (inferred; it assumes the output is affine in inverse depth). The corridor's median: area under the
  curve 0.79, its growth 0.75, the share over 0.3 0.80; at 5.3% false alarms it catches 24.1%. It fails when
  no sky is in the frame: on the 2026-10-24 05:21:21 mesa crash it read 0.57 to 0.77 from 4.1 s out, then 0.09
  at 1.0 s with rock filling the picture.

No single picture measure beats the pull-out trigger at its false-alarm rate. Combined with the telemetry they
do. An L2-regularised logistic regression, scored out of fold (5 stratified folds, 20 shuffles), on the
highest value of each feature in the window; telemetry is the live pull-out alarm and, from the logged
readings, the lowest predicted pull-out point, the steepest path, the top speed and the lowest altitude:

| Features | Area under the curve | Caught at 5.3% false alarms | At 10.6% |
|---|---|---|---|
| Live pull-out alarm alone | — | 26.4% | — |
| Telemetry | 0.810 | 34.8% (28.7 to 39.1% over the shuffles) | 47.2% |
| Pictures (both measures) | 0.792 | 31.1% | 47.1% |
| Telemetry and texture | 0.818 | 33.2% | 45.9% |
| Telemetry and depth | 0.855 | 42.4% (39.1 to 47.1%) | 63.4% |
| Telemetry and both | 0.853 | 41.8% | 62.6% |

All of the pictures' gain comes from the depth model: about 8 points more crashes caught at 5.3% false alarms
and 16 at 10.6%, against one standard error of about 5 points on 87 crashes. The texture share adds nothing
once the telemetry is in. The depth model was switched off live on 2026-10-16 because its `mid_rank` was no
higher before deaths than in random windows; the proximity here is a different figure, scored on crashes
against shoot-downs.

**Limits.** These are death windows only: a shoot-down's last seconds are not ordinary flight, so the false-
alarm rate over whole battles is not known, and neither is the precision. The live pull-out flew during these
sessions, and its climbs changed what followed. The crashes are mostly from one day (73 of 87 on
2026-10-24). The telemetry model already beats the hand-written pull-out rule on these windows (34.8% against
26.4%); read on its own, that says more about the windows than about the rule.

### Ordinary flight (2026-10-25): the gain does not carry over

The operator's `make r1 v` session of 2026-10-24 18:18 to 21:20 (account 1, 32 missions, 13 crashes, 93
shoot-downs; `logs/session_20261024_181820_acct1.mp4`, 21,897 frames at 2 a second, every frame timed in its
`frames.csv`, no gap over 0.67 s). The depth model ran on every second frame offline (10,949 frames, 4 threads,
95 ms a frame, `tools/measure_video.py`). The telemetry-and-depth score was fitted on the 503 death windows,
its thresholds set there, and applied to every battle second of the session with the same 4 s features
(`tools/score_video.py`; 6,117 seconds in battle flight, outside respawns).

| Warning, alarm episodes 5 s apart | A mission | Followed by a crash in 2 to 10 s | Crashes warned 2 to 10 s ahead |
|---|---|---|---|
| Live pull-out alarms | 2.44 | 6.4% | 5 of 13 |
| Telemetry and depth, threshold at 5.3% | 2.59 | 3.6% | 3 of 13 |
| Telemetry and depth, threshold at 10.6% | 4.09 | 3.1% | 4 of 13 |

Ranked over the battle seconds (104 of them 2 to 10 s before a crash): the score 0.708, the telemetry's lowest
predicted pull-out point alone 0.709, the depth proximity 0.668, its growth 0.666, its share over 0.3 0.680.
The depth model sees something, but in ordinary flight nothing the telemetry does not already carry; its gain
on the death windows came from how shoot-down windows differ from crash windows, not from flight in general.
13 crashes is a small sample (the precision figures move by several points with one crash); the ranking over
6,117 seconds is the firmer of the two results. **The camera path, as measured here, does not beat the
telemetry: the depth model does not go to shadow, and the GPU runtime is not needed for it.**

## Open Questions

- Does the game render more slowly when a model shares its chip? (The
  `GAME STATS` lines now measure it.)
- What are the current Camera Distance, Field of View and look-ahead values?
  Does a longer Camera Distance uncover enough of the ground below the nose to
  be worth retuning the camera detectors?
- Is depth the right representation at all, or would a sky-and-terrain
  segmentation along the flight path warn better?
- The game's files hold every map's collision meshes (step 2). Ground heights
  built from them would be a terrain database: exact, and needing no picture.
  Unpacking the game's files is the operator's decision.

## Next Steps

Since step 3 (2026-10-24), in order (2026-10-25: step 1 done, "Ordinary flight" above; the combination
failed it, so step 2 is not needed):

1. **The false-alarm rate in ordinary flight.** Score the telemetry-and-depth combination on whole battles,
   not death windows: a `make r1 v` session's video, lined up with its log through
   `logs/session_<id>.frames.csv`, run offline as above. It is the number that decides whether the
   combination is worth flying in shadow. Nothing needs the GPU for this.
2. **The cost live.** The depth model costs 95 to 272 ms a picture on the CPU (step 1), and in its 2026-10-16
   shadow it cost the steering cycle about 6%. The Intel GPU runtime below becomes the way to run it, if step 1
   of this list holds up.

Before step 3, unchanged:

1. The operator runs
   `sudo apt install intel-opencl-icd libze-intel-gpu1 libze1 clinfo`;
   then `clinfo -l` should list the Intel graphics.
2. Re-run the timing with the `GPU` device, alone and then during a battle,
   against step 1's pass mark: the frame rate from the `GAME STATS` lines,
   matches with the model running against matches without.
3. Step 2's last check, with the game open and no match running: Settings,
   Camera. Note Camera Distance, Field of View, the look-ahead and Cannon
   Auto-Zoom.

## References

- [HLDD 001 — Terrain Avoidance](../hldd/001-terrain-avoidance-hldd.md)
- [Research 016 — Manual Flight vs Tracker Shadow Comparison](016-manual-flight-vs-tracker-shadow-comparison.md)
- Code Review 019 (`docs/code-review/019-2026-10.md`): the crash, detector and
  depth-view measurements quoted here.
- `wingman/depth_view.py`, `wingman/depth_worker.py`: the existing depth view.
