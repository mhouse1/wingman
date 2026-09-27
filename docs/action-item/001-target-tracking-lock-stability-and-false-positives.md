# Action Item 001 — Target Tracking Lock Stability and False-Positive Elimination

| Status   | Date       | Wingman Version |
|----------|------------|-----------------|
| Draft    | 2026-09-23 | 1.8.11          |

## Purpose of this document

This is a self-contained task brief for an autonomous `/iterate` loop (see
`.claude/skills/iterate`), not a summary for a human reader. Whoever (or
whichever session) picks this up should be able to start from this document
alone, without re-reading the conversation that produced it. Read this whole
document before touching code — the Open Questions section names the exact
first diagnostic step, and it is not "start tuning thresholds."

**A note on evidence durability, for whoever extends this document further:**
`wingman.log` is the live session and is overwritten (`mode="w"`) on every
run — a citation by filename/timestamp into it will silently go stale the
next time wingman starts, unless that session was archived to
`logs/wingman_<end-stamp>.log` first. Archived frames under
`tests/test-output/target_tracking/` are not subject to this — nothing in
this codebase prunes them (checked 2026-09-23). Prefer citing frames by
filename; when a log line matters, quote it inline rather than pointing at
`wingman.log`.

## Goal

`TargetTracker` (`wingman/tracker.py`) should identify a real enemy aircraft,
hold that lock stably, and let `Controller.pursue_and_engage`
(`wingman/controller.py`, HLDD 015) steer toward it and fire the secondary
missile — end to end, not just "stop locking onto the wrong thing." A false
lock that never converges into a shot and a real lock that keeps dropping are
both failures of this same goal.

## Current problem (operator report, 2026-09-23)

Two distinct complaints, likely related:

1. **Lock retention is weak.** Tracking acquires a target but frequently
   loses it — `LOST_GRACE` churn and, in the session measured below, one gap
   of roughly 21 seconds in `ACQUIRING` mode with no lock at all, same live
   trial as the false positives below. This was measured from `wingman.log`
   before it was overwritten by a later run (the live log is not durable —
   see "A note on evidence durability" below), so the bracketing lines are
   quoted here directly rather than cited by filename:

   ```
   2026-09-23 21:12:20,261 [DEBUG] TargetTracker: scanned roi rect=(1099, 550, 422, 264) hits=0
   2026-09-23 21:12:20,262 [DEBUG] HEATDIVE[roll]: err=+0.365 (was +0.365 after roll_right, diverging) mode=LOST_GRACE
   2026-09-23 21:12:20,944 [DEBUG] TargetTracker: scanned acq rect=(384, 216, 1152, 600) hits=0
   ... (repeats, "scanned acq ... hits=0", roughly every 0.3-0.7s, for ~21s) ...
   2026-09-23 21:12:41,526 [DEBUG] TargetTracker: scanned acq rect=(384, 216, 1152, 600) hits=0
   2026-09-23 21:12:41,862 [DEBUG] TargetTracker: scanned acq rect=(384, 216, 1152, 600) hits=1
   2026-09-23 21:12:41,868 [DEBUG] HEATDIVE[roll]: err=+0.021 (was +0.365 after roll_none, converging) mode=TRACKING
   ```

   I.e. the wide acquisition-region scan (`scanned acq rect=(384, 216, 1152,
   600)`) ran roughly 40+ times over ~21 seconds finding zero tall-bar
   contours before one materialized. Whether that means the target was
   genuinely out of the acquisition region that whole time, or on-screen but
   unmatched, is exactly Open Question 4 below — this excerpt is evidence of
   the gap's existence and duration, not yet of its cause.
2. **False positives on the player's own HUD/effects, not enemy aircraft.**
   Confirmed categories, each with frame evidence in
   `tests/test-output/target_tracking/`:
   - **Own engine exhaust (afterburner flame).** The most persistent
     category — recurred even after the fix in "What's already been tried"
     below was live. See `pursuit_mode_20260923_211425_64.png` and
     `pursuit_mode_20260923_211427_65.png`: the `PURSUING` marker sits
     directly on the player's own right engine nozzle, no enemy nameplate
     within hundreds of pixels, `HP` dropping (45 to 5) while never engaging
     a real target.
   - **Enemy flare/countermeasure effect.** See
     `pursuit_mode_20260923_195450_83.png` — the marker locks onto a small
     floating red-orange flame-shaped effect, not the aircraft that (likely)
     released it.
   - **The game's own "INCOMING" missile-warning banner.** New this session,
     not previously catalogued. See `pursuit_mode_20260923_211420_60.png` —
     a large, solid, bright-red full-width HUD banner reading "INCOMING" is
     directly under the `PURSUING` marker. This matches the operator's own
     hypothesis ("it may be interpreting incoming detection as a target") —
     confirmed by direct pixel inspection, not yet root-caused in code.

All three false-lock frames above (`60`, `64`, `65`) show `det=1` in the HUD
debug line (`Track:TRACKING VIS err=... det=1`) — i.e. the tall-bar contour
path (`TargetTracker._detect_targets`, unaffected by `red_mass_steering` or
the nameplate gate below) found exactly one qualifying contour every time.
That repetition across three otherwise-different false locks is a lead, not
yet a conclusion — see Open Questions.

## What's already been tried

All of this lives in `wingman/tracker.py`'s `_red_mass_centroid` and the
`tracking.*` block of `wingman/config.yaml` (search both for `red_mass_` and
`2026-09-23` to find every dated comment with its measurement). In order:

1. `red_mass_hue_max: 6` — separates the afterburner's bulk orange flame
   (measured hue 7-10) from a real target's pure red icon (hue 3-5).
2. `red_mass_value_min: 245` — separates the afterburner's fading outer rim
   (same hue band as a real target, but dimmer) from a real target's flat,
   fully-bright icon.
3. `red_mass_exclude_pct` — masks out the fixed-position "NO LOCK" HUD text,
   which is bright red and was winning the centroid outright.
4. `red_mass_nameplate_gate_enabled` (currently **`true`** in shipped
   config, thresholds **unvalidated**) — added `_count_nameplate_glyphs`,
   which rejects a red-mass candidate unless the same scanned crop also
   contains at least `red_mass_nameplate_min_glyphs` (default 20) small,
   glyph-shaped connected components — the letter/digit fragments of a real
   enemy's HUD nameplate (name/distance/type), which no flare or exhaust
   ever has nearby. Measured 33 such components on a real target vs. 8-9 on
   each of two false positives, from 3 frames in one engagement — see
   `tests/test_target_tracking.py::TestNameplateGate` for the unit tests
   (all synthetic, all passing) and `docs/hldd/005-target-tracking-hldd.md`
   for the design writeup.

   **This did not fix the problem.** It was enabled for a live trial
   (`wingman/config.yaml`, `red_mass_nameplate_gate_enabled: true`), and the
   very next session reproduced the own-exhaust lock (frames `64`/`65`
   above) and found a brand new false-positive category (the "INCOMING"
   banner, frame `60`) that the gate was never evaluated against.

## Open questions — start here, in this order

**Do not start by re-tuning `red_mass_nameplate_min_glyphs` or adding more
exclusion regions.** Two prior attempts to reason about this from screenshots
and log lines alone (this session's own `/check` runs) produced numbers that
**contradicted each other** — a hand-reconstructed crop of the exact scanned
region logged 0 tall-bar hits where the live log said `hits=1` for the same
tick. That is the `/iterate` skill's own "Standing trap" — *"Reproduce with
the real function, not a re-implementation"* — biting on this exact feature.
Static reconstruction has already been shown unreliable here; do not repeat
it as the primary diagnostic.

1. **Add direct instrumentation before changing behavior.** In
   `_red_mass_centroid` (and/or around the `selected = ...` line in
   `TargetTracker.update`), log — at minimum — the glyph count
   `_count_nameplate_glyphs` computed, whether the gate passed or rejected,
   and which path (`red_mass` vs. the tall-bar `_select_target` pick)
   actually supplied `selected` that tick. Run a live trial with this
   logging in place before touching any threshold. This turns "did the gate
   fire" from a forensic reconstruction into a one-line grep.
2. **Check whether the tall-bar path is the real culprit, independent of
   red_mass entirely.** All three new false-lock frames show `det=1`. If the
   tall-bar contour path is independently finding a spurious tall/narrow
   shape on the INCOMING banner's border or the exhaust ring's highlight,
   then `red_mass_nameplate_gate_enabled` was never going to fix this — it
   only gates the `red_mass_centroid` override, not `_select_target`'s own
   pick, and the override only applies `if red_mass_local is not None`
   (i.e., a rejected gate falls back to the tall-bar pick, which may itself
   already be wrong). Instrument both paths, not just the one added most
   recently.
3. **Characterize the "INCOMING" banner specifically.** It is large, solid,
   bright red, and (based on three frames) appears to render at a
   consistent-ish position (upper-center, spanning much of the screen
   width) whenever the game is warning of an incoming missile — closer in
   character to "NO LOCK" (fixed HUD chrome) than to the afterburner
   (attached to a moving airframe). Measure its actual position variance
   across several occurrences before deciding whether a `red_mass_exclude_pct`-
   style fixed-region exclusion is viable, or whether it should simply be
   caught by the nameplate gate once (1) and (2) above establish why it
   currently isn't.
4. **Quantify lock-retention separately from false positives.** The ~21s
   `ACQUIRING` gap measured 2026-09-23 21:12:20–21:12:41 is one data point.
   Before changing `lost_timeout_sec` / `local_roi_reacquire_cycles` /
   `local_roi_max_scale`, count how often gaps like this happen per mission
   and whether they correlate with the target leaving the acquisition
   region entirely (expected, not a bug) vs. the target being on-screen but
   unmatched (a real detection gap). Per the `iterate` skill's own
   discipline: label each finding **measured**, **inferred**, or
   **assumed** — don't tune a timeout against an assumed cause.

## Relevant files

- `wingman/tracker.py` — `TargetTracker`: `_detect_targets` (tall-bar
  contour path), `_red_mass_centroid` / `_count_nameplate_glyphs` (red-mass
  override + nameplate gate), `_select_target`, `_compute_roi`,
  `_handle_miss`, the `TrackMode` state machine.
- `wingman/controller.py` — `pursue_and_engage` (HLDD 015), the
  `_eject_heatdive_loop` roll axis (ADR 136), the sustained-hold actuation
  mechanism (`_sustained_roll_hold`/`_sustained_pitch_hold`,
  `engage_roll_search`, HLDD 005 2026-09-23 addendum).
- `wingman/config.yaml` / `wingman/config_schema.py` — the `tracking:` block
  (every `red_mass_*` key, `local_roi_*`, `sustained_hold_enabled`) and
  `pursuit_mode:` block.
- `docs/hldd/005-target-tracking-hldd.md` — target tracking design,
  including the 2026-09-23 "Sustained-Hold Actuation" section.
- `docs/hldd/015-target-tracking-pursuit-mode-hldd.md` — pursuit mode design
  and its stated preconditions (check these are still accurate before
  assuming pursuit mode's rollout phase).
- `tests/test_target_tracking.py` — `TestRedMassSteering`,
  `TestNameplateGate`; `tests/test_pursuit_mode.py`;
  `tests/test_sustained_hold.py`.
- `tests/test-output/target_tracking/` — archived frames. Read them with
  the `Read` tool (it displays PNGs directly) rather than inferring from
  filenames or logs; multiple findings in this document (the INCOMING
  banner, the exhaust lock persisting) were only visible by looking at
  actual pixels.

## How to work this

Follow `.claude/skills/iterate`'s cycle — review, diagnose, fix, gate, run,
watch, record — and its loop mode if running unattended. In particular:

- Measure before fixing; never infer a rate from a mechanism you haven't
  counted (`iterate`'s own "Standing traps" section has three examples of
  this exact project getting burned by skipping that step).
- One change at a time. If a live session's MetalStorm build updates
  mid-investigation, record that as a variable, not a footnote.
- Write the test that would have caught each fix, in the same pass as the
  fix, using real frames/synthetic frames matching this codebase's existing
  `_tracker()`/`_draw_circle()`/`_bgr_from_hue()` helpers in
  `tests/test_target_tracking.py` — don't hand-roll a parallel detection
  reimplementation to validate against (see Open Question 1's own warning
  about exactly that mistake).
- Gate (`make lint && make test`) before every live run.
- After a live run, use `/check`-style rigor: confirmed / contradicted / no
  evidence / inconclusive, not "looked fine."
- Record findings in `docs/hldd/005-target-tracking-hldd.md` (amend, since
  it is still `Draft`) or a new dated ADR if a decision needs to survive
  independently — before the next run's log rotation erases the evidence.
- This repo's git rule applies regardless of how this task is invoked:
  **do not commit, push, or tag without the operator explicitly asking in
  that specific request.** Leave finished work in the working tree and
  report what changed.

## Definition of done

Not "the three named frames stop reproducing" — that can happen by
coincidence. Done looks like: a live trial through at least one full
missiles-exhausted pursuit engagement where `pursue_and_engage` holds lock on
a real enemy contact from acquisition through a fired secondary missile,
with no false lock on own-exhaust, flares, or HUD chrome anywhere in that
engagement's archived frames, and the lock-retention gaps measured are
attributable (with evidence, not assumption) to the target genuinely leaving
the scan region rather than a detection failure.

## Progress log

**2026-09-23, cycle 1** — full write-up in `docs/hldd/005-target-tracking-hldd.md`,
section "Nameplate Gate Authority — Fallback Suppression". Summary:

- Open Question 2 answered (inferred, 66 archived frames): the gate could never
  reject a lock, because a rejection fell back to the tall-bar pick. Gate-accepted
  locks were on real nameplates 22 of 22; gate-rejected fallback locks were about
  90% false. Fixed behind `tracking.red_mass_tallbar_fallback: false`.
- Open Question 1 done: `TRACKPICK` per-tick log line and `save_raw_scan` raw-crop
  archive are in. The saved PNGs are annotated and the marker overwrites the
  lock pixels — that is why static reconstruction contradicted the live log.
- Open Question 3 answered (measured): the INCOMING banner is fixed chrome at
  (710-1210, 318-358) of 1920x1200, and its shards qualify as tall bars.
- Open Question 4: one gap measured, mostly the target genuinely absent; one
  stale-ROI tick and two acquisition-edge near-misses. Not yet a rate.
- **Not done:** the live trial. `make r1` is blocked by a hung `Xwayland :3`
  (pid 1837131). Next: kill it (SIGKILL needed; it ignores SIGTERM), rerun
  `make r1`, then check `grep TRACKPICK wingman.log` against the verdict criteria
  in the HLDD section. Consider lowering `red_mass_nameplate_min_glyphs` from
  20 once live `glyphs=` values are in.

**2026-09-24, cycles 2 and 3 and live trial 1** — full write-up in
`docs/hldd/005-target-tracking-hldd.md`, sections "Search-Resume Delay" and its
"Live trial 1"; the su30 part is in `docs/adr/144-mission-su30-scripted-sequence.md`
(D4 and its live-trial paragraph). Summary:

- **Cycle 2, done and unit-tested, unverified live.** After a valid lock a miss now
  holds the roll axis neutral for `pursuit_mode.search_resume_delay_s` (2.0 s)
  instead of re-pressing ROLL_LEFT. `HOLD[roll]` logs every roll-hold state change,
  so the roll axis is no longer invisible in the log (confirmed live).
- **Cycle 3 (operator request), confirmed live for step 2.** `mission_su30` no
  longer presses SWITCH_WEAPON at the start of a life. The deferred switch in
  `pursue_and_engage(defer_switch_until_empty=True)` was never exercised live (the
  spawn weapon never emptied). The dive still switches at the 20 s pursuit cap.
- **Live trial 1 could not test cycle 2:** 0 locks in 5 pursuits, no nameplate in
  view in any of them. Do not treat the delay as validated.
- **Next target, measured twice:** locks are dropped almost immediately. 11 of 18
  acquisitions were lost on the very next scan (06:01-06:08 log; 6 of those had 7-18
  glyphs, i.e. a clipped real nameplate), and all 13 dive-loop target holds in the
  06:51-07:06 log ended by the lock being dropped, median 0.36 s. Suspects, all
  inferred: the selected point is the mean of every red pixel so it can land on none
  of them; the next ROI is centred on it and clips the nameplate; the glyph gate's
  threshold of 20 is too high for a clipped crop (real nameplates at 12-19 glyphs
  were already seen). Fix the ROI geometry or the gate's crop, not only the number.
- **Also open:** the search rarely sees a target at all (roll-only search covers a
  cone), and 2 of 5 pursuits ended in death with 3-4 incoming-missile warnings each
  (the 3 timed-out pursuits had none). Neither is explained yet.

**2026-09-24, later — operator-run session 07:34-08:24, and the fixes it asked for** —
write-up in `docs/hldd/005-target-tracking-hldd.md` ("Search-Resume Delay", "Live
trial 2") and `docs/adr/144-mission-su30-scripted-sequence.md` (D4).

- **The search-resume delay is confirmed live** in pursuit and in the dive loop
  (0 of 51 resumes came sooner than 2.0 s after a target hold). That session had the
  first pursuit locks: 8 of 22 pursuits, 26 acquisitions. Trial 1's "0 locks" was not
  representative.
- **The operator's "still over rotating" was real, and specific:** with the delay in
  place the search still resumed with the target last seen within +-0.15 of centre in
  6 of 10 pursuit and 23 of 41 dive cases, so the aircraft turned away from a target it
  was already pointing at. Fixed by a near-centre extension (6.0 s hold), and by
  applying the delay to the dive loop too. Unit-tested; not yet run live.
- **The early weapon switch was the dive, not the mission:** 16 of 16 presses came within
  1 s after a pursuit cap, when `eject_and_dive` switched away from a loaded rack (the
  operator's `v` screenshot 08:20:14 showed the 2/2 secondary selected with the 6/6
  primary untouched). The dive now defers the switch too. Unit-tested; not yet run live.
- **[Overturned at 09:19, see the Cycle 5 entry below] A hypothesis this cycle disproved (keep it disproved):** that dropped locks come from
  the ROI clipping the nameplate as it moves. Replaying the exact raw crops of the 8
  gate-pass acquisitions through the real tracker methods predicted 97% next-tick
  survival at today's settings, against 39% observed. Those 8 crops are length-biased
  (the archive samples about 1 frame per second, so it mostly keeps locks that lasted),
  and the 32 acquisitions in the two full logs split 30 dive / 2 pursuit, with survival
  43% in the dive. What is measured: lost locks show an empty ROI (median 1 red pixel)
  where survivors keep theirs (median 960), and one lost lock's frame showed the aircraft
  pitched about 90 degrees within a second. So the dive's violent pitching, not the
  detector, is the likely cause of most dropped locks, and pursuit has too few
  acquisitions to say. Not acted on.
- **Next, if wanted:** save the raw crop at every acquisition tick and the tick after it
  (not only at the 1 s archive cadence), so pursuit-phase drops can be replayed exactly.

**2026-09-24 08:41-08:56 — live trial 3 of the operator-requested fixes** (details in
`docs/hldd/005-target-tracking-hldd.md`, "Live trial 3", and ADR 144 D4):

- **Near-centre hold: confirmed on a small sample.** 3 search resumes after a lock lost
  near centre all waited at least 6.0 s (min 6.01); 8 after a far lock waited at least 2.0 s.
- **No weapon switch at the pursuit cap: confirmed.** 0 `switch_weapon` presses across 6
  capped pursuits (was 16 presses at 22 caps). The switch-when-empty step still has no live
  evidence because the primary never emptied.
- **Stopping a run:** synthetic `z` and `v` presses to `:3` were not acknowledged this time;
  SIGTERM plus `close_game` and `close_nested_display` left a clean machine.

**2026-09-24 09:0x-09:3x — Cycle 5: dropped locks are mostly a cut-off nameplate, and the
"disproved" verdict above was wrong** (full write-up in `docs/hldd/005-target-tracking-hldd.md`,
"Clipped-Nameplate ROI Follow"):

- **The 5-second interval search is not supported by the data, so it was not built.**
  Next-tick lock survival by `|err|` (538 lock ticks, three logs) is smooth across the
  deadband edge: 77% at up to 0.05, 72% at 0.05-0.10, 65% at 0.10-0.20, then 36% at 0.20-0.40.
  Overshoot while locked and rolling: 14 of 151 tick pairs (9%). The 20% vs 1.5% reacquisition
  rate between the grace window and the search is confounded (grace follows a target just seen).
- **Retraction of my own first result.** A first survival script printed 100% in every band
  because its parser skipped every scan with `sel=-`, so only survivors were counted. Discarded;
  the numbers above come from the corrected parser.
- **The overturned verdict.** 182 locks dropped after a lock in pursuit and dive windows; 125
  had red pixels present and a nameplate-gate rejection (78% of drops within 0.10 of centre).
  Four drop ticks had an archived raw crop that reproduced the logged glyph count (14, 19, 19,
  6) through the real tracker: each shows a real nameplate cut by the crop edge, the red mean
  82-173 px off the crop centre toward it. The earlier "disproved" rested on 8 length-biased
  lock crops, which cannot show a drop.
- **Fix, one change:** on a missed tick in the local ROI, when the gate rejected a red mass of
  at least 150 px that touches a crop border, the ROI is re-centred on that mass for the next
  scan (`tracking.local_roi_follow_on_clip`, on). Lock decisions, steering and the gate's
  threshold are untouched. 15 unit tests in `tests/test_roi_follow.py`; live status unverified.
- **Verdict criteria for the next run:** `grep ROIFOLLOW wingman.log`; measure *time to
  relock after an eligible drop* (gate reject, at least 150 red px). Baseline over 112 drops in
  the old logs: +1 tick 9%, +2 ticks 46%, +3-4 ticks 3%, not within four 42%. Confirmed if the
  +1-tick share rises well above 9% and the not-within-four share falls below 42%; falsified
  if +1 stays near 9%. Per-tick survival is not the metric (the follow acts after a drop) and
  should stay near 66%. **Correction, 09:38:** an earlier draft of this bullet used a 15%
  baseline from a stricter definition (within 120 px, four ticks); it is withdrawn, the
  comparison was not like-for-like.
- **Live result, 09:26-10:13 run (analysed on a 10:08 snapshot; 62 eligible drops, 44 follows,
  27 independent episodes):** time to relock, baseline (n=112) against live (n=62): +1 tick 9%
  against 34%, +2 ticks 46% against 26%, not within four 42% against 37%. So the follow brings
  the relock **one scan sooner** (strong), and does **not** show fewer drops that never come
  back (37% against 42% is inside the noise; the n=15 interim read of 27% regressed with more
  data). Gate intact: 388 `redmass` ticks, none under 20 glyphs. 0 errors; 21 pursuit caps, 0
  weapon-switch presses. Details, retractions and the by-edge split are in the HLDD "Live trial
  4".
- **What the failures are:** of the 15 follows that never relocked, 12 saw red with almost no
  label on any following tick, so most failures are not a cut nameplate, and a failed follow
  costs nothing against baseline (the wide scan takes over on the same tick).
- **Retracted while analysing (do not repeat):** a crop that looked like a fully visible label
  with 4 glyphs was another tick (real gate: 30 glyphs, pass); a crop that looked like a HUD bar
  chased by the follow did not reproduce the logged pixel count. Only crops whose replayed glyph
  and pixel counts both equal the logged tick are evidence. A frame-top explanation for the
  top-edge failures (12 of 23 relocked, against 22 of 26 elsewhere) was tested and not supported.
- **Still open:** 56 of 182 drops had no red at all (no crop archived, cause inferred to be the
  dive's pitching); the wide acquisition scan steers on the mean of every red pixel in a
  1152 x 600 region (inferred to explain relocks landing far from the old lock); the search
  rarely sees a target; a capped pursuit still dives with unused missiles.

**2026-09-24 10:40-11:1x — Cycle 6: "red present, no label" is mostly one constant-size icon**
(details in `docs/hldd/005-target-tracking-hldd.md`, "Unlabelled Red Icons"):

- **Measured.** 129 of the 200 raw crops from the 09:26-10:13 session are gate-rejected with at
  least 150 red px and under 8 glyphs; 117 of them hold exactly one red component, median 433 px,
  about 39 x 35 px bounding box, fill 0.34. A contact sheet shows red jet-silhouette icons and one
  red arrowhead, no nameplate. Such an icon-like mass sits on 76% of non-locked pursuit ticks
  (61% in the dive). Only 34% of acquisitions had one on the tick before the lock (median lead
  0.0 s), so they do not foreshadow the labelled locks.
- **Inferred, not established.** They look like distant enemy contacts drawn without a nameplate,
  which would make the nameplate gate's premise false for them and explain why the search
  "rarely sees a target". Not known: that all are enemy aircraft, their distance, whether one
  is the aircraft that later gets a label, whether steering to one brings a label into range.
  The Cycle 5 reading of the 12 failed follows as "the target left" is therefore unproven.
- **Change, instrumentation only:** `TRACKPICK` gains `blob=(x,y,aAREA,WxH)`, the largest red
  component's absolute centre, DEBUG only. Six tests; a full gate and a live run are pending.
- **Decision for the operator, not taken:** whether the tracker should steer toward an unlabelled
  icon at all. It is a design change that runs against the earlier "prefer the real, labelled
  aircraft" direction and the false positives the gate exists for. The operator can also say
  directly what the icons are and at what range a nameplate appears.

**2026-09-24 11:07 — the switch-when-empty step is now verified live** (10:51 run; recorded in
ADR 144 D4 and HLDD 005 "Deferred weapon switch, verified live"): one complete life on the
6-rack read 6, then 5, 4, 3, 2 in pursuit; the pursuit cap pressed nothing; the dive fired 2 to
1 to 0; `selected weapon empty (3 consecutive zero reads)` then one `switch_weapon` press, and
the secondary read 2. All six primary missiles were used before the only switch of the life
(the operator's requirement). 7 pursuit caps in the run so far, 1 press in total. This closes
the last "unverified live" item from the su30 weapon-switch work; one life is a small sample.

**2026-09-24 11:20 — Cycle 6 live result (10:51 run, snapshot 11:18)** (full tables in the HLDD
"Unlabelled Red Icons" and "Replication in a second session"):

- **The `blob=` field works and answered part of the question.** The icon-like blob is a
  persistent object (1,150 consecutive tick pairs: centre moved a median 12 px, 92% within 60
  px). 26 of 32 acquisitions had one in the preceding 3 s, and the lock landed within 200 px of
  it in 46% (shuffled null 16%) and within 300 px in 65% (null 29%). That is consistent with the
  icon being the same aircraft before its nameplate renders for a good share of acquisitions,
  not proof: the lock point includes the label's offset, and 35% landed more than 300 px away.
  The icon-like share of search ticks reproduced (pursuit 81%, dive 68%).
- **The ROI follow replicated in an independent session.** Relock at +1 tick: 9% baseline, 34%
  first session, 43% second, 38% pooled (n=118). "Not within four ticks": 42% baseline, 32%
  pooled, same direction in both sessions but p about 0.12, not established. Gate intact again
  (0 of 297 lock ticks under 20 glyphs).
- **The deferred weapon switch is verified live on two lives** (11:07 and 11:18): the primary
  emptied in a steady countdown, then one press; 11 caps and 2 presses by 11:18.
- **Operational:** the X key listener died once at startup (a `BadRequest` on an X extension
  request, 0.16 s after registering keys) and reconnected itself 3 s later; `z` was acknowledged
  at 11:18, so stopping cleanly still works. First time seen today; cause not investigated.
- **Still the operator's call:** whether to steer toward an unlabelled icon. The persistence and
  proximity numbers raise the plausibility that an icon-guided approach would bring a nameplate
  into range, but that needs an experiment, not more log reading.

**2026-09-24 11:58 — Cycle 7: outcomes per pursuit, and why they cannot be compared across
sessions yet** (`_EngagementTally`, HLDD 015 "Engagement summary line"):

- **Measured, per session, from the logs** (pursuits with tracker data; a "locked scan" is a
  `TRACKPICK path=redmass` tick inside the pursuit window):

  | Session | Pursuits | With a lock | Locked scans / all | Median first lock |
  |---------|----------|-------------|--------------------|-------------------|
  | 06:53 (delay) | 5 | 0 | 0 of 280 (0%) | none |
  | 07:34 (operator) | 23 | 8 (35%) | 56 of 1390 (4%) | 8.8 s |
  | 08:42 (near-centre) | 6 | 3 (50%) | 8 of 364 (2%) | 8.5 s |
  | 09:26 (+ROI follow) | 22 | 6 (27%) | 60 of 1314 (5%) | 9.4 s |
  | 10:51 (+ROI follow) | 12 | 7 (58%) | 107 of 730 (15%) | 10.6 s |

  The 09:26 and 10:51 sessions ran the same tracker code and differ 3x in locked-scan share, so
  session-to-session variation (how many targets are around) is larger than any effect seen
  from a tracker tweak at these sample sizes. No session-level before/after claim is safe.
- **The launch counts in that analysis were discarded.** Ammo reading drops counted a rack
  switch as a launch (6 to 2), and some sessions showed launches with no lock at all. That is
  why the tally logs the switch flag beside the ammo figures.
- **Change, logging only:** one INFO line per pursuit and per dive (`PURSUIT SUMMARY:` /
  `DIVE SUMMARY:`: end reason, duration, scans, locked scans and share, time to first lock,
  first and last ammo reading, whether this loop switched). 5 unit tests plus 4 loop tests;
  the full gate is recorded below. Live: unverified until the next run.
- **Use:** from the next session, `grep "SUMMARY:" wingman.log` gives every engagement's
  outcome at INFO level, so a session can be judged on locked share and ammo per pursuit
  without DEBUG `TRACKPICK` lines.

**2026-09-24 12:50 — Cycle 7 live check of the summary lines, and a pursuit-versus-dive contrast**
(HLDD 015 "Engagement summary line"):

- **The lines work live at INFO.** 20 `PURSUIT SUMMARY` and 16 `DIVE SUMMARY` lines in the
  12:05-12:46 run; `end=` values seen: `cap`, `external:match_ended`, `dive-end`,
  `external:respawn_detected`. 0 errors other than the start-up incident below; 0 weapon presses.
- **Measured, this run:** pursuits with any lock 2 of 20 (10%), locked scans 25 of 1,046 (2%);
  dives with any lock 13 of 16 (81%), locked scans 256 of 1,968 (13%). Pooled over four sessions:
  pursuit 5.8%, dive 11.5% of scans locked, and at equal altitude the dive still locks 1.7 to 2.6
  times as often. Altitude is ruled out as the explanation; time in the life and attitude
  (nose-down and afterburner against a roll-only search) are untested and not separable here.
- **Not acted on.** It points at the pursuit's roll-only search being the weakest stage
  (90% of this run's pursuits found nothing), which is the same open finding as before, now with
  a like-for-like comparison. Changing the search pattern is a behavior change and, like the icon
  question, wants a decision; it is a candidate for the next cycle.
- **Start-up incident (12:05):** a full-screen "A-10 Thunderbolt, limited time offer" bundle window
  covered the lobby, the classifier timed out 47 times, and the program's own stuck-state recovery
  did not clear it. Closed by hand (the window's cross, on the nested display). The program only
  knows a fixed list of pop-ups, so this will recur with any new promotion; a generic dismissal
  is a possible later task.
- **Stopping:** `z` sent while the game was at GAME_STARTING stopped wingman at once and closed
  the game (ADR 094, no round in progress); clean.

**2026-09-24 13:48 — Cycle 8: a stuck start-up no longer waits for a human** (ADR 146, Draft;
`wingman/close_button.py`, `game_unknown_close` in `config.yaml`):

- **Trigger.** The 12:05 run sat in `GAME_UNKNOWN` for about 2 min 42 s under a full-screen "A-10
  Thunderbolt, limited time offer" window that no known-popup template matches. ADR 093 only warns
  ("Recovery has not worked"). It happened on 1 of the 6 sessions started that day; an unattended run
  would lose its whole session.
- **ESC ruled out by test:** one ESC on a plain lobby opens "Exit to Desktop" with **Exit** as the
  highlighted default. (Closed via Cancel; nothing else changed.) A blind key from a stuck classifier
  could quit the game.
- **What works instead:** the game draws every modal's close cross with one white sprite at 29, 38
  and 45 px. A white-mask template match scores real crosses 0.951, 0.859, 0.937 and the best
  false match on any non-gameplay frame 0.747 (3,258 archived frames; three genuine examples, so a
  thin sample). Live full-screen pages (Flight Pass, Shop, own profile) score 0.64 to 0.70: they
  use a back chevron, have no cross, and are correctly ignored.
- **Behavior:** after 25 s of `GAME_UNKNOWN`, one search per 15 s, at most 3 clicks per episode,
  reset when the state leaves `GAME_UNKNOWN`; on by default, one config line to turn off.
- **Verified end to end by replaying the incident:** the real captured frame shown full-screen over
  the nested display, wingman started against it. `GAME_UNKNOWN` 13:44:36; click at (1701,255) at
  13:45:02 (score 0.937), recorded by the viewer at the same coordinates and second; at 13:45:19 the
  retry search on the visible lobby found nothing and did not click; `GAME_LOBBY` 13:45:19.8; exactly
  one click. Tests: 24 in `tests/test_close_button.py` (one from a committed fixture of the real
  window); the full-gate result is recorded in the cycle summary.
- **Not verified:** a live positive on a fresh promotion (the window did not reappear), and cross-like
  shapes in gameplay frames misclassified as `GAME_UNKNOWN`.
- **Side effects of the test, for the record:** (1) once the state recovered, wingman went straight
  into matchmaking and a real PvP round started before it could be stopped; it was stopped by SIGTERM
  and the game closed about a minute in, so that round was abandoned. (2) `make r1
  GAME_LAUNCH_DEPS="nested-setup nested-focus"` closes a running game (nested-setup restarts the
  display); `make r1 GAME_LAUNCH_DEPS=` (empty) attaches to a running one.
- **Open, second sighting:** synthetic `z` and `v` presses to `:3` were not acknowledged in this replay
  run (the first sighting was trial 3; they were acknowledged in four other runs today). The listener
  logged no death. This run differed by starting wingman without the launch steps and by a Tk overlay
  window having briefly held the display. Cause not found.

**2026-09-24 14:35 — Cycle 9: the ignored `z`/`v` key presses, narrowed and instrumented** (the
open item from Cycle 8; `wingman/input_linux.py`, `_KeyTally`):

- **Count, measured from the logs:** `z` was acknowledged in 5 runs today (07:02, 10:09, 11:18,
  12:46, 14:30) and ignored in 2 (trial 3, and the ADR 146 replay run at 13:45, where `v` was
  ignored too).
- **Ruled out by reading and by test:** (1) the delivery filter: `z` and `v` are not in
  `INJECTABLE_KEYS`, so `should_deliver_hotkey` passes them on the nested display; (2) a keyboard
  layout swap: the host has one layout (`us`), per-window input sources off, and the listener
  resolves `z` to keycode 52 both at start-up and in my sender; (3) a fullscreen window on the
  display when the listener starts: 6 of 6 synthetic presses delivered with a Tk overlay present
  at start, against wingman's real `_LinuxXTestKeyboard` on a bare nested display (a game-free
  harness, no account involved).
- **Reproduced, and new:** the listener's start-up death. In that harness the `:3` (or `:0`)
  listener died 24 ms after registering its keys (`Display connection closed by server:
  Connection reset by peer`, the same family as the 10:51 `BadRequest`), and the first key pair
  lost one press, so a start-up death leaves a blind window of about 3 s (the reconnect delay).
  That does not explain a `z` ignored a minute later, and the harness display was seconds old,
  unlike a real run.
- **Instrumentation, log only:** each listener now reports, once a minute at DEBUG from its
  watcher thread, `XKey[:3]: N KeyPress events in the last 60s (M matched a registered key, K
  delivered)`, and logs a registered non-injected hotkey that the filter dropped
  (`XKey: 'z' on :3 not delivered`). On the injection display wingman's own injected keys count
  too, so a live listener shows N above zero and a deaf one shows 0. First live check (14:30): 2
  events on `:3` in the lobby minute, both matched and delivered (wingman's own echo-safe `u`),
  0 on `:0`; `z` acknowledged. 5 unit tests in `tests/test_key_tally.py`.
- **Next time `z` is ignored,** the tally says which case it is: events arriving but not matching
  or not delivered (a mapping or filter problem), or none arriving at all (a deaf record loop).
  Not yet observed with the tally on.
- **Side effect worth knowing:** stopping wingman during matchmaking does not cancel the match the
  server has already formed. The game relaunched at 14:31 straight into that live round, so the
  account was in a PvP round that nobody was flying until I closed the game.

**2026-09-24 15:15 — Cycle 10: the pursuit-versus-dive gap is smaller than Cycle 7 said, and
neither open decision looks like a large lever** (measured from the four full sessions since 08:42
that carry `TRACKPICK`, 60 pursuits and 55 dives):

- **Time inside a pursuit hardly matters.** Locked-scan share by seconds since the pursuit began:
  0-5 s 4.8%, 5-10 s 4.0%, 10-15 s 5.7%, 15-20 s 8.8%. Any lock in 18 of 60 pursuits (30%); first
  lock median 9.5 s (25th to 75th percentile 1.6 to 13.4 s). So cutting the 20 s cap would lose
  roughly what it spends; the second half is not less productive than the first.
- **The dive locks earlier and more, and is denser in lock time:** 8.0% of scans in its first 5 s,
  18.1% in 5-10 s, 12.4% in 10-20 s, 13.9% in 20-40 s, 5.8% after 40 s; any lock in 48 of 55 dives
  (87%); first lock median 11.6 s (4.3 to 19.4 s).
- **But launches per unit time are close.** By first-to-last raw ammo reading and, for the earlier
  sessions, reading drops away from a switch press: pursuit about 12 launches in roughly 1,150 s
  (1.0 per 100 s), dive about 37 in roughly 2,370 s (1.6 per 100 s). That is about 1.5x, on 12 and
  37 events, mixing two counting methods. A pursuit that locks fires fast (6 to 4 in one, 6 to 2
  in another), so the pursuit's lower locked share does not translate into a 2x launch gap.
  **This corrects the Cycle 7 line that the chase is "the weak stage"**: it is weaker on locks per
  scan, not decisively on launches per second.
- **Deaths, from the `DIED ARMED` lines of six sessions:** 6 armed deaths in 52 respawns (12%):
  3 `enemy_fire` (an incoming warning 6 to 9 s earlier), 2 `unclassified`, 1 `terrain`. The one
  `terrain` verdict rests on a telemetry frame 117 s old and no incoming warning (by elimination,
  ADR 143's rule), so it is weak evidence. No dominant, fixable death mode shows up.
- **A cosmetic bug seen while reading them, not fixed:** the `DIED ARMED` line prints an unset
  last-incoming timestamp as an age (`incoming 1790266284.2s ago`, epoch seconds). It should say
  "never". The message is in `tick_handlers.py`, a file another change set is editing.
- **Consequence for the two open decisions (icon steering, search pattern):** the measured upside
  of either is bounded by the pursuit's share of a life (about 20 s of roughly 2 to 5 minutes) and
  a launch-rate gap of about 1.5x, so neither justifies a behavior change on this evidence
  without the operator's say-so. More runs now log `SUMMARY:` lines, which is what will tighten
  these numbers.

**2026-09-24 15:20 — Review and commit proposal (nothing staged or committed; CLAUDE.md: the
operator reviews every change first).** The working tree holds two independent change sets. Full
gate on the combined tree at 14:41: 1,992 passed, 35 skipped, 6 failed (the known order-dependent
`tests/test_input_linux.py` failures, which pass alone: 47 passed with `tests/test_key_tally.py`).

*Not mine (the capture-budget change set, to commit separately by whoever owns it):*
`wingman/capture_budget.py`, `tests/test_capture_budget.py`, `wingman/hud.py`,
`wingman/tick_handlers.py`, the whole diff of `tests/test_target_tracking.py`, and inside shared files
only these hunks: `main.py` (`from . import capture_budget`, the `capture_budget.configure` and
`prune` block near line 470, the `session_video` prune near line 736), `config.yaml` (the
`capture_budget:` block and `min_interval_s` / `max_per_encounter` under `hud.target_tracking_archive`),
`config_schema.py` (`_CAPTURE_BUDGET`, the two archive keys, the `capture_budget` Section).

*Mine, in five proposed commits* (shared files need `git add -p`; `controller.py` holds commit 1's
three features and can be split the same way if wanted). Each message ends with the trailer
`Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>`.

1. `pursuit: hold roll neutral after a lock, defer the weapon switch until the rack is empty, log an
   engagement summary` — `wingman/controller.py`; `tests/test_pursuit_mode.py`,
   `tests/test_eject_heatdive.py`, `tests/test_sustained_hold.py`, `tests/test_engagement_summary.py`;
   `config.yaml` and `config_schema.py`: the new `pursuit_mode.search_resume_centre_err` and
   `search_resume_centre_delay_s` keys and updated comments on the existing ones (`search_resume_delay_s`,
   `empty_confirm_reads` and `sustained_hold_enabled` are already in HEAD);
   `docs/adr/144-mission-su30-scripted-sequence.md`, `docs/hldd/015-...`.
2. `tracker: move the local ROI toward a nameplate the scan window cut; TRACKPICK gains edges and blob`
   — `wingman/tracker.py`; `tests/test_roi_follow.py`; `tracking.local_roi_follow_*` in `config.yaml` and
   `config_schema.py`.
   Cycle 12 adds to `wingman/tracker.py`, `config.yaml` and `config_schema.py` (widened acquisition
   region, `red_mass_exclude_zones_pct`, `red_mass_cluster_select`, `TRACKPICK` `clu=`) and
   `tests/test_acquisition_clusters.py`; it can be its own commit (`tracker: widen the acquisition region,
   mask HUD zones, steer on one nameplate cluster`) split with `git add -p`, since it shares
   `tracker.py`, `config.yaml` and `config_schema.py` hunks with commit 2.
3. `ADR 146: click a window's close cross when GAME_UNKNOWN outlasts every known popup` —
   `wingman/close_button.py`; `tests/test_close_button.py`, `tests/fixtures/a10_promo_close_region.png`;
   `main.py` (the `GenericCloseRecovery` import, instantiation and tick hunks); `game_unknown_close` in
   `config.yaml` and `config_schema.py`; `docs/adr/146-generic-close-button-recovery-for-stuck-game-unknown.md`.
4. `input_linux: log a per-display key tally once a minute` — `wingman/input_linux.py`;
   `tests/test_key_tally.py`.
5. `docs: target-tracking HLDD 005 and action item 001 progress log` —
   `docs/hldd/005-target-tracking-hldd.md`, `docs/action-item/001-...md`.
6. `tests: replay validator accepts the pursuit eject flow` (added in Cycle 11) —
   `tests/runtime_replay_validate.py`, `tests/test_runtime_replay_validate.py`.

*What a reviewer should look at first:* (a) `tracker._follow_clipped_nameplate` and the probe change
(behavior: only moves the next scan window, never a lock or the gate); (b) `controller.pursue_and_engage`
and `_eject_heatdive_loop` deferral logic (verified live on two lives, 11:07 and 11:18); (c)
`GenericCloseRecovery` is on by default and clicks after 25 s of `GAME_UNKNOWN`, at most 3 per episode
(one config line, `game_unknown_close.enabled`, turns it off); (d) the untested-live parts listed in
the entries above (a fresh real promotion; the `z` key miss).

**2026-09-24 16:25 — Cycle 11: the release gates, and one that was already red at HEAD.** I had only
ever run `make lint && make test`; `make tp`'s other gates were never run against this work.
On the current tree (each run separately; the live-screen lane, `rr-live-path1-gate`, takes over the
real display and was left to the operator):

| Gate | Result |
|------|--------|
| `make reqs-gate` (requirements and source traceability) | PASS |
| `make leak-check-gate` | PASS (latest qualifying log 2026-09-23, 1.75 h, wingman -22 MB/h; my sessions are under the 1 h minimum) |
| `make rr-path1-gate` (real main loop over replayed screenshots, then the validator) | **FAILED, and fails identically at a clean HEAD** (checked in a temporary worktree, since removed); now PASS |
| `make rr-live-path1-gate` | not run (needs the operator's display) |

*Why the replay gate was red.* Since commit `eabaa28` (2026-09-23 21:16) `pursuit_mode.enabled`
is true. The replay injects a missiles-empty screen at 52 s and a respawn screen at 65 s; with pursuit
mode on, the missiles-empty screen starts the pursuit (`MISSILES EMPTY — pursuing with secondary
weapons`, then `pursue_and_engage — tracking engaged`) and the respawn stops it
(`stopped externally (reason=respawn_detected)`), well before the 20 s cap would hand over to
`eject_and_dive`. `tests/runtime_replay_validate.py` only knew the `eject_and_dive` markers, so it
reported three missing patterns. So `make tp` has failed at HEAD since that commit, independent of
any change of mine.

*Fix (test infrastructure only, no product code):* the validator now defines both eject flows and
accepts a flow only if **all** its markers are present, including how it ended; the summary JSON
reports which completed (this replay: `pursuit`). 10 new unit tests in
`tests/test_runtime_replay_validate.py` (there were none): each flow passes; a pursuit without its
start marker, without an end, or stopped for an unknown reason fails; neither flow present fails and
names both; the flows do not borrow each other's markers; the Good Luck markers and forbidden
patterns are still enforced. Trade-off to know: this replay scenario no longer exercises the legacy
`eject_and_dive` flow end to end (its unit tests still do).

*Added to the commit proposal as item 6:* `tests: replay validator accepts the pursuit eject flow` —
`tests/runtime_replay_validate.py`, `tests/test_runtime_replay_validate.py`. It is independent of
everything else and fixes a gate that was red before this work began.

**2026-09-24 16:58 — Cycle 12 (operator question): should the acquisition region be the full screen?**
Trigger: `pursuit_mode_20260924_164821_48.png` (16:48:21, `Track:ACQUIRING det=0`): two complete enemy
nameplates ("[LiWo] happylife 10.3km F-5", "[BoP] sillyfella 10.5km A-4") are on screen at y about 940
to 1050 of 1200, below the acquisition box (`acquisition_region_pct: [0.2, 0.18, 0.8, 0.68]`, y 216 to
816). The tracker saw only a red arrowhead icon (glyphs 0).

- **Measured, 300 archived full-screen frames (11:00 to 16:51 sessions), tracker red mask, glyph-sized
  components clustered in 300 x 170 px windows:** 57 frames carry a nameplate-like cluster (at least
  20 glyph components). Of the 71 clusters, 40 are inside the acquisition box, **28 outside it in open
  screen** (11 below, 7 above, 3 left, 2 right, 5 in corners), 3 in the top HUD strip. So about 41% of
  the nameplates shown lie where the tracker does not look (25 of 57 frames have one). A contact sheet
  of 12 outside clusters showed real nameplates in all 12 (ranges 1.6 to 9.3 km; 10.3 and 10.5 km in
  the trigger frame, so labels render at 10.5 km or more).
- **The real tracker probe on those frames, three ways** (gate = the nameplate glyph gate):
  current box passes the gate on 36 of 300; full screen 88 of 300 but only 59 once HUD zones (top strip
  under y 110, minimap, weapons panel, squad logo) are masked, so about 29 of the 88 are HUD text, not
  targets; the masked full screen passes **23 frames the current box misses and loses none**.
- **Cost of growing the box under today's steering rule (mean of every red pixel):** on the 36 frames
  the current box already locks, the steering point moves a median 331 px (max 686) at full screen with
  the HUD unmasked, and still a median 134 px (75th percentile 200, max 393) with the HUD masked,
  because a wider box lets other nameplates into the mean.
- **Answer given to the operator:** agree with widening, not with full screen as-is. It needs (1) HUD
  exclusion zones (`red_mass_exclude_pct` is a single rectangle today), and (2) steering on ONE
  nameplate cluster (for example the one nearest the previous lock, else the screen centre) instead of
  the mean of everything, which overrides the 2026-09-23 "centroid of every red pixel" instruction and
  so needs the operator's go-ahead.
- **Correction to Cycle 6:** I inferred the unlabelled red icons were contacts "too far to render a
  nameplate" (labels seen to 5.8 km). The trigger frame shows a label at 10.5 km, drawn about 170 px
  **below** its marker, so at least some "icon, glyphs 0" ticks are an icon inside the box whose label
  fell below it. Small sample: in frames showing both, 4 icon and label pairs had the label 162 to 287 px
  below (dx scattered, -5 to +205). Not an established rule.

**2026-09-24 17:29 — Cycle 12, done: widened region, HUD zones, cluster steering** (operator go-ahead
via `/proceed`; write-up and numbers in HLDD 005, "Wider Acquisition Region, HUD Zones and Cluster
Steering"):

- **Change:** `tracking.acquisition_region_pct` `[0.2, 0.18, 0.8, 0.68]` to `[0.0, 0.09, 1.0, 0.95]`;
  new `red_mass_exclude_zones_pct` (scoreboard and rosters, minimap, weapons panel, squad logo);
  new `red_mass_cluster_select` (gate counted per nameplate cluster; steering on the cluster nearest
  the previous lock, else the screen centre, from the red within +-150 sideways, 300 up, 100 down);
  `TRACKPICK` gains `clu=`. 13 new tests; one Cycle 6 test's over-narrow `endswith("blob=-")`
  assertion fixed.
- **Validated offline on 300 real frames** with the real tracker and real config: locks 36 to 54,
  none lost; on the 36 the old box already locked, steering point median 1 px (64% within 40 px);
  54 of 54 within 320 px of an independently found nameplate; update() 3.7 to 9.5 ms.
- **Live (6.5 min, ended by the operator's Backspace):** 10 of 14 acquisitions outside the old box, 0
  locks in an HUD zone, 0 errors, 3 of 3 pursuits locked (38% of scans) and fired; dive share 8.1%
  (no rise). Small sample; mechanism confirmed, outcome not yet measured.
- **Side effects to know:** raw scan crops in `tests/test-output/target_tracking/` are now up to
  full width, so each archived frame is larger (the capture budget's 600 files / 1 GB caps the folder).
  The operator started their own session at 17:26 on this code; its archive is the next evidence.

**2026-09-24 18:17 — Cycle 14: a correction to Cycle 10, the DIED ARMED line fixed, and what the icons
do over time.**

- **Correction to Cycle 10.** I wrote that the one `cause=terrain` verdict "rests on a telemetry frame
  117 s old and no incoming warning (by elimination, ADR 143's rule), so it is weak evidence". That was
  wrong. `RespawnHandler._classify_died_armed` checks terrain **first**, from the behavior tree's
  hard-emergency timestamp (`climb_last_hard_emergency_ts`: "hitting the ground is certain"), and by
  design does not use the pre-crash frame's altitude or rate at all. The 12:11 verdict therefore
  reflects an emergency actually being active, which is direct evidence, not inference. The two odd
  numbers in that line were unrelated to the verdict.
- **Fixed:** the line printed an unset last-incoming timestamp as an age (`incoming 1790266284.2s
  ago`, epoch seconds). `_classify_died_armed` now returns an infinite age when no alert ever
  fired and the log says `no incoming alert this session`; classification is unchanged (an infinite
  age is beyond every lookback). `tests/test_died_armed_incoming_age.py`, 7 tests.
- **Icons over time (measured; three full sessions, 10:51, 12:05 and 17:26, icon-like unlabelled
  blobs linked across ticks, tracks of at least about 1 s):** 229 tracks. Pursuit: 70 tracks, 1 (1%)
  became a labelled lock within 350 px in the next 1.2 s (lead 3.9 s), 69 never did (median 4.0 s,
  90th percentile 14.2 s). Dive: 159 tracks, 22 (14%) became one (median lead 4.2 s, 90th percentile
  8.4 s), 137 never did (median 3.3 s). 89% of all icon-visible time is in tracks that never became a
  lock. This is what passive flight gives, and the pursuit does not approach the icons, so it cannot
  say whether approaching would convert them. It supports an experiment, not a conclusion, and
  whether to run one (icon steering) is still the operator's decision.

**2026-09-24 18:20 — Cycle 14: `make session-report` (alias `make sr`).** Judging a session used to mean
hand-grepping or asking for a one-off script, and each comparison recounted slightly differently.
`scripts/session-report.py` reads a log only (no game, no display, nothing imported from wingman) and
prints one page: time span, errors (startup classification timeouts counted separately), pursuit and
dive engagements from the `SUMMARY:` lines (any lock, locked-scan share against the pooled pre-widening
baseline, fired, end reasons), the tracking block from `TRACKPICK` (lock ticks, acquisitions and how many
were outside the old acquisition box and on which side, lock ticks in an excluded HUD zone which must
be 0, `clu=` counts, gate rejections, ROI follows), weapon presses and empty-switches, pursuit caps,
respawns, `DIED ARMED` by cause, generic close clicks, and the log's own session summary. A log
without `TRACKPICK` lines (an INFO log) says so instead of printing zeros. Usage: `make sr` for
`wingman.log`, or `make sr LOG=logs/wingman_<stamp>.log`.

Checked against the 24-minute 17:26 session, whose numbers I had counted by hand with a different
script: identical on every figure (2,193 ticks, 252 lock ticks, 33 acquisitions of which 24 outside the
old box with the same sides, `clu` 233/18/1, 111/570 pursuit and 128/1275 dive locked scans, 8
respawns). `tests/test_session_report.py`, 10 tests, from a synthetic log with known answers; one test
pins the report's copies of the old box and HUD zones to `config.yaml` so they cannot drift silently.

**2026-09-24 18:45 — Cycle 15: the operator's three decisions, and no time cap on the pursuit.**
The operator answered the open questions:

1. **Icons: ignore them.** "the icons are showing the general direction of the enemy aircraft, we can
   ignore it." No icon steering; closed (recorded in HLDD 005).
2. **Stuck-popup clicks: leave them.** `game_unknown_close.enabled` stays true (ADR 146).
3. **No dive after the 20 s chase.** "after a 20 second chase do not dive, continue searching and
   pursuing."

*Change for 3.* `pursuit_mode.pursuit_max_duration_s` ships as `0`, meaning no cap (a positive value
restores it; the code default stays 20.0 for a config that never sets the key). The loop's cap check is
`cap > 0 and elapsed >= cap`. The ammo-exhausted fall-through to `eject_and_dive` is unchanged: an
empty airframe still dives to rearm; only the timer is gone. Consequences handled:

- **Anomaly 003 would have ended recording sessions.** `EjectStuckDetector` fires when GAME_BATTLE_EJECT
  has had no descent running for 40 s, and a pursuit has no descent, so any pursuit over 40 s tripped it
  in a `make rd v` run. Added `Controller.eject_flight_active()` (descent or pursuit) and `main.py`
  passes that. Nothing else in the code time-limits the eject state (a health-alive event ends it only
  after an observed death).
- **Tests:** 6 new in `tests/test_pursuit_mode.py`: still pursuing past where a 0.5 s cap fires, no
  fall-through into the dive, fires and ends on a respawn (`end=external:respawn_detected`), still dives
  when the ammo is exhausted, the deferred weapon stays untouched however long it runs, a positive cap
  still works, and `eject_flight_active` is true only while a pursuit flies. 165 related tests pass.
- **Docs:** HLDD 015 (D3 table, "No time cap", open questions 2 and 3), ADR 144 (Draft, amended in place,
  the dated live-trial paragraphs left as the record).

*Exposure the operator should know about (measured, not fixed).* No behavior-tree tactic acts inside
GAME_BATTLE_EJECT except Climb's terrain emergency, so a long pursuit is guarded against terrain but not
against the arena edge, and the search only rolls, so it flies straight. Over 50 pursuits with boundary
readings: start distance from the boundary median 0.37 minimap radii (minimum 0.06); 13 of 50 showed the
boundary ahead and under 0.3 away; the worst closing rate (-0.010 per second) reaches a boundary 0.5
away in about 50 s. No out-of-bounds warning has been seen during a pursuit, but pursuits were at most
20 s. The one warning on record (17:32) was at a life's start in normal battle. If lives start ending
out of bounds, the follow-up is a boundary guard for the pursuit (steer away, or let the existing
boundary turn act in this state) and, related, a search that turns the nose instead of only rolling.
`make sr` will show the effect (`DIED ARMED`, respawns, pursuit lengths).

**2026-09-24 19:44 — Check of the no-cap change, then the operator's chase-altitude request (ADR 147).**
*Check of Cycle 15 against the 18:57 to 19:18 session (measured).* 6 chases, no `PURSUIT CAP`, every end
`external:*`, lengths 28 to 181 s (median 157 s); 4 ended when the match ended, 2 in death by enemy fire
(157 s and 99 s in); the deferred weapon switch worked on a long chase (19:01:17, 90 s in). Locked share 14%,
inside the session-to-session spread. Two limits: the Anomaly 003 fix was not exercised (the run was not
`--record-session`), and the boundary reader was blind in chases (1 reading in 486 ticks, against about 90% in the
two earlier sessions, with the minimap plainly on screen in the archived chase frame), so the boundary exposure I
quoted last cycle came from readings taken before or early in earlier, capped chases and says nothing about how
close these chases got to the edge. No crossing was confirmed (`RTB w/ missiles 0`, no `RTB: red_frac` lines).
The tracker in HEAD (`3cb467d`, another session) is newer than what this run used, so the next session changes two
things at once.

*The operator's request:* "first fix the issue where it's continuously rotating at 4000 altitude, modify it so it
rotates at 3000."

*Diagnosis (measured, mechanism from the code).* The chase does not choose its altitude. The tree's hard floor
(4000 m) and armed sustain band (4000 to 5000 m) are above su30's 3000 m level-off. At 18:59:27 the script stopped
its climb at 3192 m and the tree started a new one to 5000 m 1.3 s later; the -10 degree step waited behind it for its
full 20 s and gave up (24 of 26 su30 hand-offs across the day's four sessions), so the chase began at 4750 m, sank to
the floor and rolled there (median chase altitude 3544, 4609, 4365 and 3923 m in the four sessions; all 31
`ALTITUDE FLOOR` events cite 4000 m). ADR 144 had recorded this as its open question 2.

*Change (ADR 147, Draft).* New `su30_mission.alt_floor_m: 3000`. While su30 is the mission in play, the hard floor is
that value and the sustain band stands aside; J20 and JAS39 keep 4000 m. `ClimbCondition` takes an override function,
`make_sustain_climb_condition` a suppression function, `build_tree` threads both, `Controller.altitude_floor_override_m()`
and `sustain_climb_suppressed()` answer from "the last launched mission is su30". The dive-recovery and terrain-ahead
triggers are untouched. Removing the config key restores the old behavior.

*Tests (21 new).* Floor override 6, sustain suppression 3, the 18:59:27 altitude replayed through the real tree and a
real snapshot 5 (the tree's own doctrine restarts the climb, as measured; the su30 doctrine does not; the floor alone is
not enough, the band must stand aside too), handler wiring to a real Controller 1, Controller predicates 4, shipped
config 2 (`alt_floor_m` at or below `climb_alt_m` and below the tree's floor). Mutation checks: ignoring the override
fails 5, dropping the suppression 4, dropping the wiring 1. `make sr` gained an ALTITUDE block (5 tests): chase altitude
median and 10th to 90th percentile, `ALTITUDE FLOOR` events by floor, su30 hand-offs and how many ended "nose angle not
confirmed". Baseline for the after-check: pursuit altitude median 3923 m (10th to 90th percentile 1674 to 4255 m), floor 4000
on 13 of 13 events, 6 of 6 hand-offs not confirmed.

*Not verified.* No session has run with this change. Success would read: `ALTITUDE FLOOR ... below 3000m`, no
`target alt 5000` climb after `step 3/4`, chase altitude centred near 3000 m, fewer unconfirmed nose angles.
Open and named: a floor climb in `GAME_BATTLE` (steps 1 to 3 only) still aims at the sustain exit altitude (5000 m), which
matters only if the aircraft sinks below 3000 m before the hand-off; and separate from this, the telemetry filter let a
handful of 1 to 41 m altitude reads through in chases (35 m at 19:00:45 while the HUD read 3060 m started an emergency
airbrake climb), which is worth its own look.

**2026-09-24 20:22 — Cycle 16 (`/iterate improve the target tracking`): the committed tracker aimed about
325 px above the aircraft; fixed (write-up and numbers in HLDD 005, "Aim Point").**
*Review.* No session has run since `3cb467d` (another session's tracker rewrite: aim at "the marker above the
label", keep locks on a looser test near the previous cluster, whole-region scan every tick). The newest log (18:57
to 19:18) predates it, so the review replayed the rewrite's real functions on archived frames.

*Diagnosis (measured).* The game draws the aircraft's marker (a diamond bracket, or a small red jet icon on far
contacts) BELOW its nameplate, about 100 px below the label's glyph centre: 90, 95, 98, 101, 102, 102, 106 and 110 px
on 8 archived frames read with a ruler (targets 0.36 to 6.8 km, mean 100.5). The rewrite looked for a marker 162 to 287
px above the label and otherwise moved the label up 225 px, so its steering point was 327, 326, 331 and 335 px above the
marker on four real frames; it found a "marker" on 2 of 54 nameplates in the 300-frame archive and took the fallback on 52.
The marker is also darker than the strict red mask (hue 0 to 2, value 217 to 230 against a floor of 245), so it was never
in the mask to be found. The wrong geometry came from my Cycle 12 note ("label 162 to 287 px below the icon"), which paired
labels with icons of other contacts; corrected in HLDD 005.

*Change (one).* `tracking.red_mass_aim_offset_px: 100` replaces `red_mass_marker_band_px` and `red_mass_marker_offset_px`; the
steering point is the chosen nameplate's glyph centre moved 100 px down. `_aim_point` and the `aim=` field of `TRACKPICK`
are removed. Nothing else in the tracker moved.

*Tests (13 new, 4 replaced).* `tests/test_nameplate_aim_geometry.py` runs the real probe on four real crops
(`tests/fixtures/aim_*.png`, 580 KB, lossless) and requires the point within 15 px of the hand-read marker, requires the
marker 85 to 115 px below the label, and requires the old rule to miss by more than 75 px. The synthetic frames in
`tests/test_acquisition_clusters.py` drew the marker above the label, agreeing with the code and disagreeing with the game,
so they now draw it below, and four marker-rule tests became five offset tests. Flipping the offset's sign fails 8.
Result on the fixtures: (+7, -2), (+6, -1), (+2, -6), (+7, -10) px against -327 to -335 px in y before.
Out of sample: 12 further frames rendered with the new point; on the five with a visible bracket it sits within about 12 px.

*Also added to `make sr` (5 tests).* Lock runs (consecutive lock ticks) and how close to centre the steering point sits.
Baseline from the last log (old tracker, old aim): 116 runs over 305 lock ticks, median 2 ticks, longest 22, 24 runs of 4
or more; |dx| median 47 px (50% inside the roll deadband), |dy| median 115 px (20% inside the pitch deadband); over the 24
runs of 4 or more the first tick's median |dy| was 74 px and the last 106 px, so pitch was not converging inside a lock.
Locks are too short to settle, which is the larger limit and what the rewrite's keep test is for.

*Gates.* `make lint`, `make test` (2085 passed, 35 skipped, before the report addition; the 5 report tests and lint were
rerun after), `make reqs-gate`, `make rr-path1-gate`: all PASS.

*Not verified.* No live session with this change or with `3cb467d`. Open: whether roll and pitch now converge and the game
reports a lock more often; the gains were tuned on the old aim; a partly drawn label shifts the glyph centre and the aim with
it; labels near the screen edge may be clamped by the game.

**2026-09-25 00:18 - Cycle 16, live result (the operator's own session, 00:03 to 00:08 on 2026-09-25, two lives, stopped with `z`).**
Code under test: `3cb467d` plus the Cycle 16 aim change and the ADR 147 altitude change (working-tree diff hash 9218f916dd10d64d).
The session started after my reply and I did not start it: under the pre-flight I added to the iterate skill this turn (the
operator pointed out that it says to run wingman), an existing operator session is Watched, not duplicated.

*Aim fix: mechanism confirmed, pitch centring not.* No `aim=` in `TRACKPICK`; one lock run of 65 ticks (23 s) against a longest of
22 before; 64 of those ticks passed the strict gate, 1 the keep test. |dx| median 46 px (57% inside the roll deadband), |dy| median 77 px
(17% inside the pitch deadband) after the approach, against 115 px and 20% before. Details in HLDD 005, "Aim Point", live check 1.

*ADR 147: floor wired, goal not met.* The floor cites 3000 m; step 3 began at 3019 and 3191 m; the -10 degree step was not confirmed on
either life and the chases started at about 4160 and 3730 m. Two causes found, neither the floor:

1. **Stale climb latch (measured mechanism, reproduced with the real `ClimbCondition`).** The floor emergency at 721 m sets the band's
   `_active` latch. BoundaryTurn then held the selector for 33 s (00:03:59 to 00:04:32), and the band's release needs two evaluations of
   `__call__`, which py-trees never makes while a higher-priority sibling wins. When BoundaryTurn ended at 3651 m the first evaluation
   returned True and started a climb toward 5000 m; the running hold then kept the leaf selected. Repro: latch at 721 m, 33 s of
   `update_emergency` only, first evaluation at 3651 m is True, the second False. Anomaly 007 fixed this staleness for the emergency
   verdict but not for the band. Pre-existing and independent of su30; it defeats the su30 level-off whenever BoundaryTurn follows a
   floor emergency (at spawn, near the edge, that is usual).
2. **BoundaryTurn at the spawn (life 2).** Three "banking and pulling away" turns during step 3 pulled the nose up against the nose-down pulses.

*New, serious: both chases ended by flying into the ground.* Measured from the log:
- Life 1 (00:04:42 to 00:05:36): locked from 00:05:01, the aircraft followed the target down from 4555 m at nose -22 to -42 degrees,
  speed 488 to 1159 KPH, altitude at 3 s steps 4372, 4172, 3886, 3566, 3205, 2840, 2429, 1974, 1431, respawn at 00:05:36. The steering
  point stayed within about 80 px of the screen centre on most ticks (the loop was doing its job, into the ground).
- Life 2 (pursuit from 00:06:25): no lock at all (`path=none`); a steep climb to 4583 m at 234 KPH, then a dive from 00:06:40 at -34 to
  -46 degrees to 1102 KPH and altitude 2 m at 00:07:07 (search roll spiral after a near-stall; inferred, the log has no attitude trace).
- In life 2 the tree's emergency selected Climb in time and did nothing useful: the `BT[active]` lines read ttg 25 s (Idle), then 19,
  15, 11 and 5 s with Climb selected, and each `CLIMB - holding nose up + airbrake (EMERGENCY ...)` ended `climb complete (state_exit,
  0.3s)` and restarted 1.5 s later. Life 1 shows the same airbrake holds from 00:05:20 (altitude about 3200 m).
  Cause (code, `_run_climb_hold`): the SAF-001 backstop exits a climb hold on any game state other than `GAME_BATTLE`, and pursuit lives
  in `GAME_BATTLE_EJECT`, so an emergency recovery in a chase is 0.3 s of nose-up in every 1.5 s (a 20% duty cycle) while the pursuit
  loop and the search roll keep writing the same axes. ADR 144 recorded the two-writer half; the state exit makes it fatal.
- Side observations: the crash to the respawn screen took about 26 s, longer than the 10 s terrain lookback, so life 2's death was
  classified `unclassified`; pitch key holds are not logged, so pitch behaviour cannot be read from a log.

*Proposed next cycle (not started).* (1) Let a hard-emergency climb hold run through `GAME_BATTLE_EJECT` while `is_pursuing()` (still
ending on eject, evade, respawn or any other state); (2) make the pursuit loop yield pitch and roll, including the search roll, while
such a hold runs; (3) refresh the Climb band's hysteresis every tick in `update_emergency`; (4) log pitch key holds. Tests for each,
gate, then run. Not a preventive dive guard: that is a policy choice about how low the chase may go and is the operator's call.

**2026-09-25 00:44 - Cycle 17 (operator: "after it switched to secondary weapons it crashed to the ground, it should have continued pursuit sequence"): a dive recovery flies through a pursuit (ADR 148).**
*What the log and frames say about the switch (measured).* At 00:05:24.495 the first rack read empty three times and the switch to
the secondary was pressed; the frame archived at 00:05:26 shows `+100 DESTROYED Gabagool`, so the lock that ended right then was a kill. No
`eject_and_dive` and no fall-through followed, and the summary is `end=external:respawn_detected dur=54.5s`: the pursuit ran to the
crash. The dive that crashed it began at 00:05:06 (nose -22 degrees, 4372 m), 18 s before the switch, chasing that target down.

*Cause (code, measured in the log).* `_run_climb_hold` releases a hold on any game state other than `GAME_BATTLE`; a pursuit lives in
`GAME_BATTLE_EJECT`. The tree's `DIVE RECOVERY` fired at 29 s to ground and its airbrake holds started 24 times, but 38 holds ended
`state_exit` (33 after 0.3 s), while the chase kept holding roll and pitch. Hence the Cycle 16 finding that both lives flew into the ground.

*Change (one mechanism, three parts; ADR 148, Draft).* (1) A hard-emergency hold (time to ground or terrain, not the floor) keeps flying
through `GAME_BATTLE_EJECT` while `_pursuing`, latched, capped by the new `pursuit_mode.recovery_max_s` (30 s; 0 restores the old
behaviour); takeover, respawn, any other state and eject or evade pre-emption still release it, so SAF-001 stands. (2) The chase releases
its sustained holds (the search roll included) and steers nothing while it flies, then resumes. (3) `HOLD[pitch]` debug lines, because pitch
holds were not logged at all. Not touched: the stale climb latch and the boundary-turn pull-ups (the su30 level-off, Cycle 18), and any
preventive dive guard (the operator's policy call).

*Tests and gates.* `tests/test_pursuit_recovery.py`, 19 tests on a real Controller, real hold thread and real `GameState`. Mutation checks:
a state check that never exempts fails 8, a chase that never yields fails 4, a flag never cleared fails 1. `make lint`, `make test`
(2112 passed, 35 skipped), `make reqs-gate`, `make rr-path1-gate`: all PASS.

*Run.* `make r1` at 00:40:27, python pid 3080120, log `wingman.log` (the 00:03 session is preserved as `logs/wingman_20260925_000825.log`),
code state HEAD 3f89442 plus working tree (`git diff HEAD -- wingman` hash b058ced5e8bb4447). The Monitor is armed and proven (its first
event was `Configuration loaded`). The Monitor on the 00:03 session had delivered nothing because its last stage, `cut`, block-buffers; fixed in the skill.

*First live result, 00:41 to 00:43 (measured, the run's first life).* The chase began at 3597 m and dived at 814 to 1079 KPH, nose -9 to -26
degrees. `DIVE RECOVERY - 25s to ground (alt=3297m rate=-130m/s)` fired at 00:41:52.0, the hold logged `hard emergency inside a pursuit: flying
through GAME_BATTLE_EJECT and the chase yields` at 00:41:52.3, and the chase logged `yielding pitch and roll` at 00:41:52.4. The aircraft
bottomed at about 1930 m (00:41:56), climbed to 3425 m at the cap (00:42:22, `recovery_cap`, 30 s), reached 4110 m at 00:42:32 and the chase
resumed at 00:42:28. A second recovery began at 00:43:37 from 2372 m and the aircraft was climbing again at 00:43:41. Two dives, two recoveries, no crash so
far, against two crashes in the two lives of the 00:03 session. Cost noticed: the pull-out ran through a nose +90 degrees at 191 KPH (near a
stall, at 00:41:59) with the airbrake held, and the cap ended the hold with the aircraft still climbing at nose +63 degrees.
To read next: hold durations in `climb complete (...)`, `HOLD[pitch]` lines, deaths by cause in `make sr`, how often the chase dives again after a recovery.

*Watch update 2026-09-25 00:45 (the 00:40 run, first life, still alive after 4.5 minutes; measured from a copy of the log).* Three recoveries so far
(00:41:52, 00:43:37, 00:44:43), each ending at the 30 s cap with the chase resuming a few seconds later. Altitude: 3704 m twelve seconds before the
first, minimum 1928 m, 4094 m at +43 s; 2926 m, minimum 2304 m, 4380 m; 3900 m, minimum 2778 m for the third. **All three dives followed a locked
chase** (18 of 27, 51 of 67 and 59 of 68 ticks in the preceding 25 s were lock ticks), so the aircraft is following targets down, not spiralling in a
search. The recoveries took about a third of the pursuit so far (36 s + 32 s + the third), and 136 of 564 ticks were lock ticks (24%). No death yet,
against a death within about 1.5 minutes on both lives of the 00:03 session. The open question this raises is the operator's: whether a chase
should be allowed to follow a target that low at all (a preventive dive guard), or whether break-off at 30 s to ground is the intended limit.

*Watch update 2026-09-25 00:45.* The first life ended with the match, not a crash: `PURSUIT SUMMARY: end=external:match_ended dur=231.7s scans=634
locked=132 (21%) first_lock=0.3s ammo=4->3 switched=yes`. 232 s with three dive recoveries and no death, against 54 s and about 85 s (both ending in an impact)
in the 00:03 session. One life is one sample: the next lives are what will say whether it holds.

*Result of the 00:40 run (2026-09-25 00:50 entry; 00:40:27 to 00:49:15, 8 min 45 s, five lives; log `logs/wingman_20260925_004915.log`).* It ended when the
nested display `:3` was closed under it (`XIO: fatal IO error on X server ":3"`, exit 2): the operator started their own session at 00:49:38, and a
run target closes a running game and its display. Not a wingman fault, and not a fault of the change. Measured with `make sr` on that log:
- **Dive recovery, the target of the cycle: worked in the one long chase.** Three `recovery_cap` holds (30 s each) in a 232 s life that ended with the
  match (`end=external:match_ended`), with altitude minima 1928, 2304 and 2778 m, against two impacts in the two lives of the 00:03 session. `HOLD[pitch]`
  lines: 55, so the instrumentation works. All three dives followed a locked chase.
- **Not shown by this run:** whether it holds over more chases. The other four lives never dived: three died to `enemy_fire` (two before the hand-off, at about
  37 s and 40 s after spawn, one 21.6 s into a chase with no lock), each about 6 s after the incoming warning with 4 missiles unused. Prologue deaths in
  today's earlier logs: 9 of 153 su30 starts (6%); here 2 of 5. Small sample; not attributed to any change.
- Tracking numbers, for the record: 7 lock runs, longest 63 ticks, median 1; |dx| median 56 px (46% inside the roll deadband), |dy| median 122 px (17% inside
  the pitch deadband); 8 `ALTITUDE FLOOR` events all citing 3000 m; the -10 degree step unconfirmed on both hand-offs.

*Watch update 2026-09-25 00:56: where the recoveries actually began (measured; the last BT read before each `hard emergency inside a pursuit` line, and the lowest 3 s altitude read in the following 30 s).*

| Start | Altitude, rate, time to ground at the trigger | Dive before it (3 s reads: altitude, nose) | Lowest read after |
|-------|-----------------------------------------------|---------------------------------------------|-------------------|
| 00:41:52 (my run) | 3297 m, -130 m/s, 25 s | 3597 (-9), 3345 (-18), 2948 (-26) | 1928 m |
| 00:43:37 (my run) | 2598 m, -87 m/s, 30 s | 2795 (-9), 2628 (-12), 2372 (-21) | 2304 m |
| 00:44:43 (my run) | 3406 m, -115 m/s, 30 s | 3701 (-17), 3432 (-20), 3085 (-24) | 2778 m |
| 00:52:56 (operator's session) | 2331 m, -133 m/s, 17 s | 2651 (-21), 2371 (-22), 1971 (-28) | 1488 m |
| 00:54:56 (operator's session) | 3171 m, -110 m/s, 29 s | 3445 (-11), 3200 (-18), 2869 (-23) | 2495 m |
| 00:55:53 (operator's session) | 3912 m, -191 m/s, 20 s | 4352 (-35), 3981 (-35), 3404 (-40) | 2054 m at nose -67 degrees, still diving when the match ended |

Both lives so far ran to the end of the match (232 s and 242 s), with 3 and 4 recoveries. The margin depends on the dive: the triggers came at 17 to 30 s to
ground and cost 300 to 1400 m before the aircraft turned, which is enough at 100 to 130 m/s of descent; the last dive was -35 to -67 degrees at up to 1280 KPH,
lost 1858 m or more in about 5 s, and the outcome is unknown because the match ended. Options if that proves fatal, all the operator's call: a larger
`recover_below_time_s` for a pursuit, a descent-rate trigger, or the preventive dive guard in the chase's pitch loop. The chase resumed at 4795 m after the
previous recovery and was at -35 degrees within 13 s, so the chase itself dives immediately after every recovery.

*Watch update 2026-09-25 01:00: the first crash with the recovery in place (the operator's 00:49 session, life 2, PURSUIT SUMMARY `end=external:respawn_detected dur=76.8s`; measured from the log).*
Not enemy fire (no incoming warning, no `DIED ARMED` line). The chase resumed at 4081 m at 00:58:18 after the first recovery and dived at nose -40 to -43 degrees, firing
2 to 3 missiles on the way (`Ammo missiles: 2, 1`). `BT` reads: 3946 m at -124 m/s, ttg 32 s (00:58:21); a telemetry gap (alt None, 00:58:25); 2883 m, ttg 31 s (00:58:30),
when the altitude floor (not a hard emergency) started a soft climb WITH the afterburner at 770 KPH in the dive, released after 0.25 s by `state_exit` and followed by a 6.3 s exit
push (`pitch budget (3 pulses) exhausted`); 2740 m, ttg 29 s (00:58:33, a stale repeat); then a garbage read (`Altitude: 218 | Speed: 7`, 00:58:36) gave `alt=1805 alt_rate=-743m/s
ttg=2s` and started the hard emergency at 00:58:37.8 with the real altitude near 1800 m and descending fast (inferred: the ttg=2 s came from the garbage read, so the
emergency started by luck, and later than the readings alone would have started it). In the recovery: the next read was another garbage sample (`alt=1448 rate=+486m/s angle=90`), and the
hold's two-sided rate authority pulsed NOSE DOWN at 00:58:39.8 and 00:58:41.5 in a -49 degree dive; then nose-up pulses from 00:58:43 (rate -132 to -163 m/s) brought the nose from
-90 degrees at 388 m to +12 degrees at 303 m (00:58:54); at 00:58:57 the ttg read 49 s, the emergency cleared mid-hold (`resuming normal fuel-floor logic`, afterburner re-engaged) at
264 m, and at 00:59:00 the aircraft was at nose -44 degrees at altitude 0. Impact about 00:59:02, respawn detected 00:59:04.

Three defects visible in one dive, none of them the mechanism ADR 148 added: (1) the trigger is late for a dive that accelerates faster than the 3 s telemetry, and can be set off by a
garbage read; (2) a soft floor climb with the afterburner starts in a dive; (3) the hold trusts an implausible positive rate and pulses nose down. Plus the policy one: the chase
resumes and dives again straight after each recovery. Recoveries so far (both runs): 9 started, this is the first crash; the last life of the run before it survived 4.
Recommendation, the operator's call on the parameters: a preventive guard in the chase (no nose-down command below a time-to-ground, using the estimate the tree already keeps, with roll
tracking continuing), and rejecting implausible rates and altitude jumps in the recovery hold.

*Watch update 2026-09-25 01:03: a prologue terrain crash (the operator's session, 01:02:02 to 01:02:41, `DIED ARMED - 4 missile(s), cause=terrain`; measured).* Before the chase, in `GAME_BATTLE`, so
neither ADR 148 nor the altitude doctrine's floor was the mechanism. The step 1 climb went vertical near the map edge (nose +56 degrees at 1263 m, then +90 degrees at 1836 m and 2303 m with
speed falling 746 to 491 KPH), BoundaryTurn took the selector twice (01:02:09 and 01:02:20) and pulled at the top, and the aircraft fell into a dive: 2545 m at nose +37 (01:02:18), 2298 m at -24, 1521 m at
-61 and 1060 KPH, 523 m at -57 and 1425 KPH (01:02:27), 176 m (01:02:33). The climb hold's exit routine logged `overshot into a dive (-24deg)` and `pitch budget (3 pulses) exhausted, releasing anyway`, and the tree
returned to Climb at about 1000 m (01:02:26), too late. Prologue deaths in today's logs: 12 of about 165 su30 starts (7%): 3 enemy_fire, 2 terrain, 4 unclassified, 3 with no `DIED ARMED` line (not armed at death).
Last hour: 3 of 10 (two enemy fire, this one). The vertical climb near the spawn edge is a known hazard of the script's step 1 (the operator's "nose up on battle starting or respawn"), not a new one; noted for a later cycle.

*Watch update 2026-09-25 01:06: the second crash with the recovery in place (the operator's session, `PURSUIT SUMMARY: end=external:respawn_detected dur=162.9s scans=449 locked=178 (40%)`; measured).*
The chase resumed at 4153 m at 01:05:24 after its second recovery and dived at nose -19 to -37 degrees, speed rising to 1271 KPH: 3453 m (01:05:32), 2878 (01:05:38), 2582 (01:05:41, floor climb started, WITH
the afterburner, released by `state_exit` after 6.3 s), 2177 (01:05:44), 1692 (01:05:47). The hard emergency started at 01:05:48.9 from 2150 m at -161 m/s (`ttg=13s`), 1091 m at 01:05:50, 896 m at 01:05:53 with
`Speed: 38` (an impact or a misread; the respawn was detected 3 s later). The tree's estimate still read `ttg=19s` at 1226 m: it assumes the ground is at altitude 0, and this map has terrain near 900 m and higher, so
the aircraft hit the ground with the estimate saying about 19 s remained (inferred from the altitude and speed at the last read; no frame of the impact was checked). Recoveries with the change so far: 14 across
the two sessions, 2 crashes, both from dives at 1200+ KPH begun at 3000 to 4000 m (the other: 00:58:38). In this life the recoveries took about half the chase (3 of them: 01:03:43, 01:04:52, 01:05:49) before the crash.
Proposed for the next cycle, pending the operator's parameter: the chase does not command nose-down below a minimum altitude (default: the doctrine floor plus 500 m, so 3500 m for su30) or while the time-to-ground
estimate is under 60 s; roll tracking, firing and the search roll are unchanged; a config key, off by setting it to 0.

*Watch update 2026-09-25 01:28: the 01:20 death was a missile kill, not a crash, and the death classifier is blind after the weapon switch (the operator's session, `PURSUIT SUMMARY: end=external:respawn_detected dur=95.0s scans=261 locked=106 (41%) ammo=4->2 switched=yes`; measured unless labelled).*
The recovery worked and the aircraft was shot down while climbing. Sequence: hard emergency at 01:20:18.4 from 1391 m; altitude 1645 m at 01:20:19.8 (nose +31 degrees), 1695 m at 01:20:22.8 (nose +10, 331 KPH, +17 m/s; the emergency
climb holds the airbrake and suppresses the afterburner, so speed fell from 649 to 331 KPH in four seconds); `INCOMING MISSILE DETECTED (source=ocr_fallback ... text=INCOMING)` at 01:20:21.557 with flares deployed; the HUD digits (health, ammo, fuel)
gone by the first OCR pass that finished after it (no digits for flares, missiles, fuel and health at 01:20:22.95 to 01:20:23.13); a red blob of 35639 px, 398 by 379, at the frame centre at 01:20:24.195; `RESPAWN` text at 01:20:26.27. No terrain explanation fits at
1.6 km and rising a moment before the HUD vanished (inferred: the spawn altitude on this map is about 1500 m and the highest ground-impact altitude read so far is about 900 m, 01:05:53), so the cause is inferred to be the missile. No HUD frame of it exists: the
encounter archive cap (12) had been reached. The recovery had run about 4 s and had gained about 300 m, so ADR 148 did not cause it.
*How late the recovery started.* The chase had descended from 4016 m at 01:19:32 to 1297 m at 01:20:14 (nose -14 to -23 degrees, 980 to 1180 KPH) before this hold began at 1391 m. The floor was crossed at 01:19:49 and five soft floor climbs (01:19:49, :57, 01:20:04, :09, :10) lasted
0.25 to 6.3 s each and released by `state_exit`; two of them ended with `climb exit - no telemetry, single 1.0s nose-down pulse applied` (01:20:08.1 and 01:20:10.6, at about 1600 m with the aircraft in a dive), the open item (d) again. Same pattern as the two crashes, one step
short of one: the soft floor is a nudge in a chase, so the aircraft went 1600 m below it before the hard trigger.
*Why the log has no cause line for it.* `tick_handlers.py:690` skips the `DIED ARMED` line when `had_secondary_weapon_active` is set (the missile counter then reads the heatseeker rack). In this session the three deaths with the secondary active (00:59:04,
01:05:56, 01:20:27) have no `DIED ARMED` line and the two with it off (01:02:41 `terrain`, 01:10:15 `unclassified`) have one: three of three. So the deaths that follow a switch, which is the situation the operator's Cycle 17 report was about, are the ones
the classifier cannot label at all, and `make sr` counts them as not armed.
*MissileEvade cannot run in a chase (measured from the code and the logs).* `is_idle` (behavior_tree.py:194) selects Idle whenever the state is not `GAME_BATTLE`, and a pursuit lives in `GAME_BATTLE_EJECT`; the one exception (`make_idle_condition`) yields only to Climb's
emergency; and `make_missile_evade_condition` yields to that same emergency (the operator's Phase 1 directive). So in a chase without an emergency the tree selects Idle, with one it selects Climb, and MissileEvade is unreachable either way; the chase gets flares and the ADR 128
afterburner hold only. Logs with pursuit windows (from the 13:46 log on 2026-09-24; earlier logs carry no `PURSUIT SUMMARY` line): 16 incoming alerts, 8 inside pursuits with 0 MissileEvade starts and 4 deaths within 12 s, 8 outside pursuits with 3 MissileEvade starts and 2 deaths
within 12 s. Four of 8 against 2 of 8 is not a difference n=8 can show, and "a death within 12 s of an alert" is a proxy that also counts deaths from other causes. The tactic at the alert inside pursuits was Climb 6 times and Idle twice (01:16:36, survived).
Neither HLDD 015 nor ADR 070 mentions it. Whether it matters is not measurable from this sample (ADR 070's 82% against 50% survival is from `GAME_BATTLE`, not from chases; assumed to transfer, not shown).
*Recoveries in the operator's session, 00:49 to 01:28:* 13 started, 9 ran to the 30 s cap, 3 ended in a death (two ground impacts, 00:59:04 and 01:05:56, and this missile kill), 1 ended another way. Five deaths in the session: those three, a prologue terrain crash (01:02:41) and an
unwarned kill in a zero-lock chase (01:10:15, 107 s, `locked=0%`, no incoming alert).
Proposed, not built, the operator's call: (1) instrumentation only: give the death line a cause when the secondary is active (same classifier, a `DIED (secondary active)` tag), so the next report can separate crash from shot-down; (2) a design question, not a tweak: whether a chase
should be able to evade (Idle yields to an alert in a pursuit, and the chase yields its axes as ADR 148 does). Note this death would not have changed under (2) as the tree stands, because the altitude-floor emergency pre-empts MissileEvade by the operator's own directive.

*Watch update 2026-09-25 01:38: a false emergency from one garbage altitude read, then a missile at 128 KPH (the operator's session, `PURSUIT SUMMARY: end=external:respawn_detected dur=69.6s`, `DIED ARMED - cause=enemy_fire (incoming 6.0s ago)`; measured unless labelled).*
The first recovery of this life was sound: hard emergency at 01:31:02 from 1551 m at 1289 KPH, 3026 m at 01:31:30, cap at 01:31:32.4. One garbage read then undid it. `PADLOCK ... Altitude: 314` at 01:31:33.5, between real reads of 3026 m (01:31:30.5) and 3485 m (01:31:36.5).
The plausibility filter accepted it: the implied -904 m/s is under `max_alt_rate_mps: 1000` (ADR 097 D2, calibrated on real dives that reach 919 m/s), so the smoothed value became the mean of 2704, 3026 and 314 = 2014.7 m, with `alt_rate=-906m/s ttg=2s`. The filter then rejected the good 3485
read against that anchor (`rejected reading (speed_raw=368 altitude_raw=3485, total_rejected=19)`, an implied +1057 m/s; ADR 097 D3's reseed needs three rejections and got one). The tree read `alt=2014.7 alt_rate=-906m/s ttg=2s` unchanged at 01:31:33.6, 35.1 and 36.6, and `alt=None` at 38.1 when the snapshot went stale.
Consequences: (1) the recovery cap's exit routine ran on it (`climb exit - overshot into a dive (-90deg) - correcting with nose-up` at 01:31:34.4, `pitch budget (3 pulses) exhausted`, `climb complete (recovery_cap, 36.3s)` at 01:31:38.4) while the aircraft was climbing at +64 degrees;
(2) a second EMERGENCY hold (airbrake held, afterburner suppressed) started at 01:31:39.6 at 3668 m with `ttg=n/a` and no rate, 668 m above the su30 floor and climbing (inferred: the latch from the poisoned reading was still on), and ADR 148 made the chase yield to it again. The tree went back to Idle at 01:31:42.6 (`alt_rate=+81m/s`) but the hold
thread kept pulsing (`down` with rate 80.6 at 01:31:43.9 and 45.6, `up` at 47.4, 49.1 and 50.9). Speed: 430 KPH at 01:31:30.5, 222 at 42.5, 128 at 45.5 (nose +37; `STALL PREVENTION - speed 222 KPH below 300 KPH` at 44.1). `INCOMING MISSILE DETECTED` at 01:31:33.8 and 35.3 (flares, afterburner held 5.4 s) and again at 01:31:45.8; the health digits
were gone from about 01:31:45.8 and the respawn text came at 01:31:50.6. Inferred: a near-stalled aircraft is the easiest missile target, and the second hold should not have run. Not shown: that it would have survived at speed. Both missile deaths in this session (01:20 at 331 KPH, this one at 128 KPH) came during or just after a recovery, with the alert about 1.4 s before the HUD went (n=2).
*Cost of a false emergency under ADR 148.* Before it, a false hard emergency in a chase was a 0.25 s nudge; now the latch makes it a hold of up to 30 s with the chase yielding, as ADR 148's Consequences said it would. The latch exists to stop a chase re-diving a low aircraft; at 3668 m it protected nothing.
*How common the spike is (measured, with a caveat).* Single-read low outliers (a read more than 1000 m under both neighbours, the neighbours within 700 m of each other) in the PADLOCK series of the 22 logs from 2026-09-24 03:33 on: 29, six followed within 8 s by an emergency line, one of those inside the ADR 148 period (this one). The series prints raw reads,
including ones the filter rejected (a one-step drop over about 3000 m is rejected by the 1000 m/s ceiling), so it overcounts the cases that poison the anchor. Recoveries in the operator's session so far: 16 started, 11 reached the cap, 4 ended in a death (00:59:04 and 01:05:56 ground impacts, 01:20:24 and 01:31:47 missile kills), 1 ended another way.
Proposals, not built, the operator's call, cheapest first: (1) on the ADR 148 side: the recovery exemption applies only below a height (the doctrine floor plus a margin, for example 500 m, so 3500 m for su30), so a hold that starts or continues above it ends by the old state exit and a false emergency at altitude costs the nudge it used to;
(2) perception: one accepted outlier should not become the anchor (a median of the last three for the smoothed value and the rate, or a reseed on the next read that agrees with the read before the outlier); ADR 097's calibration set has to be re-checked, so this is the larger change; (3) the cap's exit routine should not pulse nose-up on a nose estimate that contradicts the read before it (+64 to -90 degrees in one step).
Not runnable while the operator's session is up: a fix cycle ends in `make r1`, which relaunches the game.

*Watch update 2026-09-25 01:43: two more deaths (01:37 an impact with no recovery, 01:40 a missile kill during one) and where the session's deaths come from (the operator's session, 00:49 to 01:41; measured unless labelled).*
*01:37:27, ground impact (`PURSUIT SUMMARY: end=external:respawn_detected dur=47.1s scans=131 locked=34 (26%)`, no `DIED ARMED` line: the switch to the secondary was at 01:37:25.7).* The chase held level near 3570 m from 01:36:51, then `HOLD[pitch]: None -> down (target err_y=+0.601)` at 01:37:20.8 and no release before the
impact: 3045 m at 01:37:21.5, 2539 m at 24.5 (1045 KPH, nose -35), 17 m at 27.5 (1270 KPH), 2522 m in one 3 s step. The altitude floor's soft climb started at 01:37:24.6 and was released by `state_exit` 0.25 s later; its exit push (`climb exit - overshot into a dive (-35deg) - correcting with nose-up`, 3 pulses) ran until 01:37:30.9. `is_climbing()` stays
true through that push (`_run`'s `finally` clears the flag after `_run_climb_hold` returns, controller.py:4356), and the tree only starts a hold when none is running, so the hard trigger (`ttg=18s` at 01:37:24.6 and again at 26.1, two confirming reads) could not start a recovery before the impact (inferred from that code path; no log line states it, and no `hard emergency inside a pursuit`
line was written). A garbage `Speed: 5` read at 01:37:15.5 fired `STALL PREVENTION - speed 5 KPH below 300 KPH` at 01:37:17.1 (afterburner held; harmless here).
*01:40:25, missile kill in a recovery (`PURSUIT SUMMARY: end=external:respawn_detected dur=138.9s scans=391 locked=101 (26%)`, no `DIED ARMED` line, secondary active).* Dive from 3405 m at 01:39:53 to 2091 m at 01:40:08.7 (1227 KPH); floor climb at 01:40:02.8 released after 0.25 s, exit push to 01:40:09.0; `INCOMING MISSILE DETECTED` at 01:40:08.9;
hard emergency at 01:40:10.3 (`ttg=25s`), the recovery bottomed near 1690 m at 01:40:14.7, and the aircraft was at 1892 m, 271 KPH, nose +63 at 01:40:17.7 (`STALL PREVENTION` at 19.3), 371 KPH at 20.7 and 476 KPH at 23.7, climbing. Four more alerts (01:40:19.5, 20.95, 22.5, 23.9, one missile by their spacing) preceded the HUD going at about 01:40:25.5 and `RESPAWN` at 01:40:29.7.
*Where the deaths come from.* Eight unplanned deaths in 00:49 to 01:41 (two more lives ended by the designed ammo-out dive): 3 chase-dive ground impacts (00:59:04, 01:05:56, 01:37:27), 1 prologue terrain crash (01:02:41), 3 missile kills during a recovery (01:20:24, 01:31:47, 01:40:25) and 1 unwarned kill in a zero-lock chase (01:10:15). Six of the eight
followed a chase dive through the 3000 m floor. Recoveries: 18 started, 12 ran to the cap, 5 ended in a death (2 impacts, 3 missile kills), 1 ended another way. Missile alerts (grouped when under 6 s apart): 5 inside recovery windows (8.6 min of chase, 0.58 per min) against 3 in the rest of the chase (20.7 min, 0.14 per min); 3 of those 5 ended in a death, 0 of the 3 outside.
That is suggestive and no more (5 against 3 alerts; one-sided p about 0.05 for a rate that simply tracks time, before any correction for looking). A recovery flies slow by design (airbrake, afterburner suppressed, steep climb: 128, 331 and 271 to 476 KPH around the three missile deaths); slow flight as the mechanism is the thing to test, and it is not shown here.
*Recommendation, the operator's call:* the dive guard proposed on 01:06 is now the strongest option: it acts on the origin of six of the eight deaths (the descent) instead of the recovery afterwards, and it would also mean fewer recoveries, fewer chances for a false emergency, and less slow flying under fire. The cost is that the chase cannot follow a target nose-down below the guard altitude; roll tracking, firing and the search roll stay.
