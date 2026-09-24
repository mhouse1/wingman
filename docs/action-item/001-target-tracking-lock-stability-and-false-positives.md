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
