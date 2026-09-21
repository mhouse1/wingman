# Design 005 — Screen-Space Target Tracking and Nose Centering

| Status | Date       | Wingman Version |
|--------|------------|-----------------|
| Draft  | 2026-06-26 | 1.6.22          |

## Overview

This HLDD defines a closed-loop target tracking capability for J-20 attack behavior:

- detect a moving target marker in the HUD,
- estimate its position relative to screen center,
- apply roll input (`ROLL_LEFT_KEY` / `ROLL_RIGHT_KEY`) until the target is centered.

Unlike quadrant-only logic, this design uses continuous screen-space error and a feedback controller.

---

## Problem Statement

Current behavior can detect enemy presence and perform coarse directional responses, but it does not maintain continuous alignment with a moving target on-screen.

Desired behavior:

- if target drifts left of screen center, roll left,
- if target drifts right of screen center, roll right,
- stop rolling inside a center deadband,
- keep tracking as target moves frame-to-frame.

---

## Goals

1. Track one target marker continuously in screen space.
2. Convert target position into a normalized horizontal error signal.
3. Apply stable, non-jittery roll correction using existing controller key helpers.
4. Respect existing safety and manual-takeover constraints.
5. Provide low-overhead single-window visual telemetry via periodic annotated screenshots.

## Non-Goals

1. Full 3D interception guidance.
2. Pitch/yaw control loops. Still true for *this* design — the roll
   controller stays roll-only. Where a consumer needs pitch alongside it
   (ADR 136's dive-and-track mode, HLDD-011's future `BoresightEngage`),
   pitch is driven by a separate, already-existing primitive
   (`eject_and_dive`'s `NOSE_DOWN` impulse rotation, ADR 069) called
   alongside this tracker's roll controller, not folded into it.
3. Multi-target tactical prioritization beyond single-target lock persistence.

---

## Functional Design

### 1. Target Sensing

Source regions (two-phase):

1. **Global acquisition region**: `GAME_BATTLE`-anchored scan area using ADR023-style percentage coordinates and relative text offsets.
2. **Local tracking ROI**: a smaller dynamic window around the locked target, updated every cycle.

Acquisition to tracking flow:

1. Start in global acquisition mode to find an initial target marker/text anchor.
2. After lock, initialize local ROI centered on detected target location.
3. On each scan, compute new position from updated relative text location inside local ROI.
4. Expand ROI or fall back to global acquisition if confidence drops or target exits ROI bounds.

Marker extraction:

- HSV filter for target marker colors.
- Prefer locked-target color if available (red), fallback to non-locked target color (green).
- Extract contour centroids.

#### Target marker visual characteristics

From live gameplay frames (`P2_050_RESPAWN_CLEAR_HEALTH_ALIVE_MISSILES_4.png` and related):

- **Non-locked enemy marker**: tall, narrow, bright-green vertical bar. Typically 3–6% of frame height,
  width 1–4 px. One bar per visible enemy. Multiple bars present simultaneously at varying
  distances (near bars are taller; far bars are shorter and may resolve to near-point blobs).
- **Locked-target marker**: same vertical-bar shape, color shifts toward red/orange.
  When locked the dashed-circle reticle (see below) also appears.
- **Lock-on reticle**: a dashed white/green circle that tracks the locked target independently
  of the bar. The reticle is a separate HUD element — it is **not** the target marker and should
  not be used as the centroid source. Track the bar, not the reticle.
- **Yellow proximity dot**: a small solid yellow marker appears below/near the reticle when an
  enemy is very close (< ~500m). Can be used as a high-confidence "close target" signal.
- **Blue edge indicators**: thin blue horizontal lines at screen edges indicate enemies outside
  the FOV. Not useful for screen-space tracking but signal that reacquire should be attempted.

**Reference frame for the centrality principle** (`test_screenshots/ALTITUDE_SPEED.png`, 1920x1200):
captured at match end (`MATCH OVER` banner), so it shows the post-match nametag/health-bar overlay,
not the tall-bar marker system described above — it is not itself a frame the live tracker consumes.
It is kept here because it states this design's own ranking goal in the clearest available terms: of
the two visible contacts, `[TIG] ManuTheRed`'s Su-25 (0.25km, rendered roughly 250px left and 45px
above frame center) sits far closer to center than `[SD] demonslayer`'s F-14 (1.3km, rendered at the
right-edge indicator rail, 330-770px off-center depending on which of its two rendered elements is
measured). A tracker faithful to this design's selection goal should prefer the Su-25. See Selection
Hardening (below) for a code-level gap between that goal and the current implementation that this
frame motivated flagging.

Contour selection implications:
- Contour aspect ratio filter (tall/thin) eliminates the dashed circle and HUD noise.
  A contour with height >= 3x width and minimum area of 12 px² is characteristic of the bar.
- The nearest (tallest) bar is usually the highest-priority target. Nearest-to-last-centroid
  policy handles the case where multiple bars are present.

#### Respawn recovery

After a `respawn_detected` event (from `MissionStatsTracker`), the tracking state machine
must return to `Acquiring` regardless of prior state. The respawn blank-screen interval
(typically 2–4 s) means the local ROI will see no markers; this must not trigger a permanent
lost-target state. The `lost_timeout_sec` grace window should be long enough to survive the
respawn blanking without requiring a tuning change. The `Searching → Acquiring` transition
is gated on `GAME_BATTLE` being active, which handles respawn automatically as long as the
FSM is still in `GAME_BATTLE` throughout.

Relative-anchor method (ADR023-aligned):

- define crop bounds as percentages of `GAME_BATTLE` frame.
- define target text/marker expected location relative to the crop origin.
- track movement by comparing previous and current relative anchor coordinates.
- preserve screen-size independence by keeping all config in normalized coordinates.

Output per frame:

- `visible: bool`
- `centroid_x_px: float | None`
- `confidence: float` (optional heuristic from contour size/shape)
- `scan_mode: global | local`
- `roi_rect_px: [x, y, w, h] | None`

### 2. Target Selection and Persistence

When multiple centroids are present:

1. Prefer red target marker set over green marker set.
2. Pick centroid closest to previous tracked centroid (`last_x`) for temporal consistency.
3. If no previous target, pick centroid closest to crop center.

**Known gap (2026-09-21, see Selection Hardening below):** rule 1 is implemented as a mask-level
switch (`TargetTracker._detect_targets`, `wingman/tracker.py:317`), not a per-candidate tie-break —
any red pixel anywhere in the acquisition crop discards the *entire* green candidate set before rules
2/3 ever run. A locked contact far from center, or even at the frame edge, currently always outranks
an unlocked contact dead-center. Rules 2/3 only ever choose among candidates of whichever single color
survived rule 1, never across colors. Also worth stating plainly: "closest" in rules 2/3 is horizontal
pixel distance only (`abs(cx - ref)`, `wingman/tracker.py:339`), never full 2D distance to center —
consistent with this design's roll-only Non-Goal 2, but easy to misjudge from a screenshot where the
eye reads 2D closeness. Not yet changed — see the phased validation plan below for why.

Lost-target behavior:

- keep last target for `lost_timeout_sec` (short grace window),
- if not reacquired within timeout, clear tracking state.

### 3. Error Signal

Horizontal error is measured relative to active scan center:

- `error_px = target_x - center_x`
- `error_norm = error_px / (active_width / 2)`

Where:

- `error_norm < 0` means target is left of center,
- `error_norm > 0` means target is right of center,
- `error_norm = 0` means centered.

### 4. Roll Controller

Use proportional control with deadband and clamped hold duration:

- if `abs(error_norm) <= deadband`: no roll,
- else compute `hold = clamp(kp * abs(error_norm), min_hold, max_hold)`.

Actuation mapping:

- `error_norm < -deadband` -> `roll_left(hold_seconds=hold)`
- `error_norm > +deadband` -> `roll_right(hold_seconds=hold)`

Rate limiting:

- enforce `command_cooldown_sec` between roll commands.

---

## Runtime Architecture

```mermaid
flowchart TD
    CAP[Frame Capture] --> MODE{scan_mode}
    MODE -->|global| GBL[Global acquisition scan]
    MODE -->|local| LCL[Local ROI scan]
    GBL --> AN[Analyzer: detect_target_marker]
    LCL --> AN
    AN --> SEL[Select/Persist Target]
    SEL --> ROI[Update/expand local ROI]
    SEL --> ERR[Compute error_norm]
    ERR --> DEC{error outside deadband}
    DEC -->|No| HOLD[No roll command]
    DEC -->|Yes| CTRL[Compute hold_seconds]
    CTRL --> DIR{error direction}
    DIR -->|Negative| RL[Controller.roll_left]
    DIR -->|Positive| RR[Controller.roll_right]
    CAP --> HUD[HUD Renderer: draw telemetry]
    HUD --> OUT[Atomic write to static screenshot path]
```

Integration points:

- Analyzer: new method returns tracking observation and normalized error.
- Main loop: calls tracking update in `GAME_BATTLE` only.
- Controller: add a thin `orient_nose_to_target(error_norm)` helper that translates error to `roll_left` / `roll_right` calls.
- Debug HUD: render an annotated frame every `hud.interval_sec` and overwrite one static file watched by VS Code/image preview.
- Tracking mode manager: switches between global acquisition and local ROI scan.

---

## Visualization Strategy (Primary)

To avoid split attention across game + terminal + secondary debug windows, this design
uses a **single static filename screenshot HUD** as the primary runtime visualization.

### Rationale

1. No in-game overlay injection and no anti-cheat-sensitive drawing path.
2. No extra live debug window that steals focus.
3. Persistent telemetry frame that does not scroll away like terminal logs.
4. Predictable resource usage at fixed update intervals (default 1.0s).

### Rendering model

1. Capture frame as usual.
2. Draw telemetry text/graphics directly onto a copy of that frame.
3. Write to a temp file, then atomically replace the static output filename.

Atomic write sequence:

- `live_hud.tmp.png` write complete
- `os.replace(live_hud.tmp.png, live_hud.png)`

This prevents preview tools from showing partially-written images.

### Minimum telemetry payload

1. Timestamp and loop FPS/interval.
2. FSM state (`GAME_LOBBY`, `GAME_BATTLE`, etc.).
3. Tracking values: `visible`, `centroid_x`, `error_norm`, `last_roll_cmd`.
4. Combat counters: health, missiles, flares (when available).
5. OCR timings/cycle metrics relevant to tracking responsiveness.

### Logging policy

The screenshot HUD becomes the primary high-frequency telemetry surface.

- Keep terminal logs for warnings/errors and significant state transitions.
- Reduce repetitive per-cycle info logs that are now visible on the HUD frame.

---

## State Model

```mermaid
stateDiagram-v2
    [*] --> Searching
  Searching --> Acquiring : game battle active
  Acquiring --> Tracking : target detected
  Acquiring --> Acquiring : no target
    Tracking --> Tracking : target detected
    Tracking --> LostGrace : target missing
  LostGrace --> Acquiring : roi expand/reacquire
    LostGrace --> Tracking : target reacquired
  LostGrace --> Searching : timeout exceeded
```

State meanings:

- `Searching`: no active target.
- `Acquiring`: scanning global battle region for first lock.
- `Tracking`: active target and feedback control enabled.
- `LostGrace`: short persistence window to prevent oscillation on brief occlusion.

---

## Safety and Gating Rules

This section originally described one combined gate on the whole tracking
capability. As of 2026-09-09 the implementation (already live in
`wingman/tick_handlers.py`'s `TrackingHudHandler`, ahead of this document —
see the note at the top of Implementation Plan) splits **sensing** from
**actuation**, and the two are gated differently:

- **Sensing** (`TargetTracker.update(frame)`, HUD rendering — no key press)
  runs in `GAME_BATTLE` **and** `GAME_BATTLE_MANUAL`. This is deliberate:
  sensing quality can be validated live while an operator flies manually and
  deliberately points at targets, with zero actuation risk — exactly the
  "Live dry-run logging mode" this document's own Validation Strategy step 2
  already called for, now generalized to run continuously rather than only
  during automated flight. A config flag, `tracking.actuate` (default
  `false`), gates whether detections are ever turned into roll commands at
  all — even in `GAME_BATTLE` — so enabling `tracking.enabled` for the first
  time can never silently start rolling the aircraft.
- **Actuation** (`Controller.orient_nose_to_target`) is suppressed unless
  *all* of the following hold:
  1. `tracking.actuate` is `true`.
  2. game state is `GAME_BATTLE` (never `GAME_BATTLE_MANUAL`).
  3. mission is running (`Controller.is_mission_running()`).
  4. target visible and lost timeout not exceeded.
  5. optional altitude guard passes (if altitude source is available).

Manual takeover always wins over autonomous roll correction — rule 2 above
is absolute, not merely a default.

**This split exists for a second consumer, not only for this document's own
validation.** ADR 136 (`docs/adr/136-heatseeker-dive-invokable-mode.md`)
calls `TargetTracker.update()` and `Controller.orient_nose_to_target()`
directly from inside `eject_and_dive`'s existing closed-loop thread, gated
by its own new flag (`eject.heatdive_enabled`, default `false`) rather than
a separate mode or mission. That caller does **not** go through
`tracking.enabled` / `tracking.actuate` at all — those flags gate only the
ambient, tick-driven path described in this document. Manual-takeover
cancellation is inherited for free: `eject_and_dive` is already covered by
`release_for_manual_takeover()`/`cancel_mission()`, and ADR 136's addition
shares `eject_and_dive`'s own `self._eject_stop` event as its stop signal
rather than adding a second one.

---

## Configuration Additions

```yaml
tracking:
  enabled: false
  crop_name: ENEMY_CLOSE_BY
  acquisition_region_basis: GAME_BATTLE
  acquisition_region_pct: [0.20, 0.18, 0.60, 0.50]  # x, y, w, h normalized
  use_relative_anchor: true
  anchor_text_offset_pct: [0.50, 0.50]
  deadband: 0.05
  kp: 0.30
  min_hold_sec: 0.08
  max_hold_sec: 0.35
  command_cooldown_sec: 0.15
  lost_timeout_sec: 0.70
  prefer_red_lock: true
  local_roi_enabled: true
  local_roi_scale: 0.22
  local_roi_min_px: [140, 90]
  local_roi_expand_factor: 1.25
  local_roi_max_scale: 0.45
  local_roi_reacquire_cycles: 3

hud:
  enabled: true
  output_path: tests/test-output/live_hud.png
  interval_sec: 1.0
  show_crops: true
  jpeg_quality: 90
```

Optional HSV tuning block (if separated from existing enemy HSV keys):

```yaml
tracking_hsv:
  # Locked target bar (red/orange).  Wrap-around hue: also check [170,150,150]-[179,255,255].
  red_lower: [0, 150, 150]
  red_upper: [10, 255, 255]
  # Non-locked enemy bar: saturated bright green observed in live frames.
  green_lower: [45, 150, 150]
  green_upper: [75, 255, 255]
  # Yellow close-proximity dot (bonus signal, not required for tracking).
  yellow_lower: [20, 150, 150]
  yellow_upper: [35, 255, 255]
  # Contour aspect ratio filter: reject blobs that aren't taller than wide.
  # Prevents reticle circle and HUD noise from matching as target markers.
  min_contour_area: 12
  min_aspect_ratio: 2.5   # height / width >= 2.5 for a valid target bar
```

Notes on HSV values:
- OpenCV HSV uses H: 0–179, S: 0–255, V: 0–255.
- The bright green bars in live frames land around H: 50–70, S: 180–255, V: 180–255.
- The `green_lower/upper` range above tightens the original draft (was `[40,120,120]`–`[80,255,255]`)
  to reduce false positives from green HUD text and foliage in game backgrounds.
- The `min_aspect_ratio` filter is new: contour height/width >= 2.5 accepts the tall bar,
  rejects the roughly-circular lock-on reticle and small background blobs.

---

## Implementation Plan

**Implementation status (2026-09-09):** this plan is implemented, not
speculative — `wingman/tracker.py`'s `TargetTracker` and
`TrackingHudHandler` in `wingman/tick_handlers.py` exist and are wired into
the main loop today, gated by `tracking.enabled` (sensing) and
`tracking.actuate` (actuation, see Safety and Gating Rules above). The
Status table above still says `Draft` because the live-validation steps
under Validation Strategy have not been run against a real match yet —
"implemented" and "validated" are tracked separately in this document.

1. Analyzer
- add `detect_enemy_target_x(frame)` returning centroid and error.
- add short-lived tracking state (`last_x`, `last_seen_ts`) with lock protection.
- add `scan_mode` and dynamic ROI state (`roi_rect`, `miss_count`).
- implement ADR023-style relative anchor translation from `GAME_BATTLE` basis.

2. Controller
- add `orient_nose_to_target(error_norm: float)` helper.
- reuse `roll_left` / `roll_right` and enforce cooldown.

3. Main loop
- invoke tracking logic in `GAME_BATTLE` path.
- guard with manual mode and mission-running checks.
- start with global acquisition, then switch to local ROI scan after lock.
- fall back to global acquisition when local ROI confidence drops.

4. HUD renderer
- add periodic annotated screenshot writer (default 1.0s cadence).
- implement temp-write + atomic replace for static path output.
- include tracking/controller/ocr metrics overlays.

5. Tests
- unit tests for centroid selection and error normalization.
- unit tests for deadband and hold clamping.
- integration test with synthetic moving target across frames.
- unit test for atomic HUD writer path and filename replace behavior.
- unit tests for normalized acquisition-region to pixel-rect conversion.
- unit tests for local ROI expansion/fallback thresholds.
- regression test on `P2_050_RESPAWN_CLEAR_HEALTH_ALIVE_MISSILES_4.png`: verify marker detected,
  reticle circle rejected by aspect-ratio filter, centroid outside reticle region.

---

## Validation Strategy

0. Reference frame regression (static, fast):
- use `test_screenshots/integration_test/P2_050_RESPAWN_CLEAR_HEALTH_ALIVE_MISSILES_4.png`
  as a known-good GAME_BATTLE respawn-cleared frame.
- verify `detect_enemy_target_x()` returns at least one centroid with confidence >= threshold.
- verify no centroid falls inside the lock-on reticle region (approx center-left of frame).
- verify aspect-ratio filter rejects the reticle circle and accepts the tall green bars.

1. Synthetic test frames:
- move marker from far-left -> center -> far-right,
- verify roll direction changes at zero crossing,
- verify no commands inside deadband.

1b. Acquisition/local-scan switching:
- detect target in global region and confirm mode switches to local ROI.
- move target gradually and verify local ROI follows without full-frame OCR.
- force target outside local ROI and confirm bounded expansion then global fallback.

2. Live dry-run logging mode:
- compute and log commands without sending key presses,
- tune `deadband`, `kp`, and hold bounds.

2b. Live HUD mode (recommended default):
- open `tests/test-output/live_hud.png` in VS Code/image preview,
- verify frame updates at configured interval,
- validate that telemetry values match flight behavior.

3. Controlled live flight:
- enable tracking with conservative bounds,
- verify reduced oscillation and improved center hold.

---

## Risks and Mitigations

| Risk | Impact | Mitigation |
|------|--------|------------|
| Marker jitter/noise | Roll thrash | EMA smoothing + deadband + cooldown |
| Wrong target selected | Misalignment | lock-priority + nearest-to-last-target policy |
| Lost target during clutter | Unstable switching | lost-grace timeout before reset |
| Over-aggressive gains | Oscillation | clamp hold and tune `kp` incrementally |
| Conflict with manual input | Bad UX | hard gate on `GAME_BATTLE_MANUAL` |
| Local ROI too small | Target escape, reacquire churn | min ROI size + expansion ladder + periodic global fallback |

---

## Selection Hardening — Color-Class Priority vs. Centrality (2026-09-21)

### Finding

`TargetTracker._detect_targets` (`wingman/tracker.py:295-329`) does not rank red and green candidates
against each other — it excludes one color class entirely before ranking begins:

```python
mask = mask_red if (self._prefer_red and bool(np.any(mask_red))) else mask_green
```

If any red pixel exists anywhere in the acquisition crop, every green contour is dropped before
`_select_target` (`tracker.py:331-340`) ever runs. `_select_target` then ranks the survivors by
horizontal distance only — `abs(cx - ref)`, where `ref` is `self._last_x` once a target has ever been
locked, or the frame's horizontal center on first acquisition (`tracker.py:338-339`). Both of these
are correct in isolation and match this design's own stated rules (Target Selection and Persistence,
above) — the gap is that color (rule 1) is evaluated as a hard pre-filter, never as one input alongside
distance (rules 2/3). A locked contact anywhere in frame, including at the extreme edge, always
outranks every unlocked contact, however close to center, as long as it produces even one qualifying
red pixel.

`test_screenshots/ALTITUDE_SPEED.png` prompted flagging this: it isn't itself a frame the tracker
consumes (see the note under Target marker visual characteristics, above), but its layout is exactly
the shape that would trigger this gap in a live frame — a distant, edge-adjacent contact (F-14, this
document's stand-in for "reads as locked/red") next to a much closer, more central contact (Su-25,
"reads as unlocked/green"). No live capture has yet confirmed this actually firing during a real
heatdive — this is a code-reading finding, not a reproduced live bug.

### Why this matters now, not hypothetically

This design's own sensing/actuation split (Safety and Gating Rules, above) keeps the *ambient* path
inert by default (`tracking.enabled: false`, `tracking.actuate: false`, `wingman/config.yaml:749,757`).
But ADR 136's heatdive addition bypasses that gate entirely and is live today:
`telemetry.eject_closed_loop.heatdive_enabled: true` (`wingman/config.yaml:845`) means this exact
selection code already steers a real airframe on every missiles-empty eject. This is precisely the
"throwaway aircraft, already committed to crashing" moment identified as a safe place to extend
tracking — which is also, unavoidably, the one path where this selection code is not shadow-only
today. A hardening change here changes live behavior on the very next dive it ships in, unlike a
change to the ambient path (which would ship inert by default). That asymmetry is the reason for the
phased plan below rather than a direct fix.

### Phased Rollout — Shadow Session Validation

Matching this codebase's standing convention (ADR 070's missile-evade shadow trial, HLDD 013 Phase 1's
shadow-first bring-up, ADR 136 D4's own hard lesson from toggling a real key against an unverified
signal): no ranking change reaches the live heatdive path without a shadow session confirming it first.

**Phase 1 — measure, change nothing.** Add rationale logging to `_detect_targets`/`_select_target`:
whenever red pixels are present *and* at least one green contour would otherwise have qualified, log
the mask that won, the discarded green candidate(s)' position and horizontal distance from `ref`, and
the eventually-selected target's own distance from `ref` — rate-limited the same way ADR 117 D9's
`_blind_no_boundary_line_skips` logs (1st, 10th, 100th occurrence, then every 500th), not once per
qualifying tick. This is a pure logging addition — no behavior change, safe to ship immediately on the
already-live heatdive path, since it touches neither `mask` nor `_select_target`'s return value.
Purpose: learn, from real dive sessions, how often color-exclusion actually overrides a materially
more central or closer-to-last-position green candidate, and by how much — the Su-25/F-14 contrast is
a plausible shape, not yet a measured frequency.

**Phase 2 — shadow the candidate fix, still without acting on it.** Only if Phase 1's logs show the
gap firing often enough to matter: replace the hard mask exclusion with a single ranked candidate pool
(red and green contours merged, each carrying its color) and a bounded preference for red — e.g. red
wins ties within a configurable centrality tolerance, but a green candidate meaningfully closer to
`ref` still wins outright — gated behind a new flag defaulted `false` (e.g.
`tracking.ranked_lock_priority`, alongside the existing `tracking`/`tracking_hsv` blocks). While the
flag is `false`, run the new ranking function in parallel with the existing one and log wherever they
would disagree (`"SELECT[shadow]: old=... new=... would change"`), without changing `_select_target`'s
actual return value — the same "compute both, log the difference, act on neither yet" shape Phase 1
already establishes, one level up.

**Phase 3 — enable live, on the heatdive path first.** Flip `tracking.ranked_lock_priority: true` only
after a live session's Phase 2 shadow log shows the new ranking agrees with the old one whenever they
would pick the same target, and demonstrably prefers the nearer/more-central candidate in every
disagreement case observed — the same bar ADR 136 D5 already set for its own fix ("live-measured...
not a guess"). Validate specifically through the heatdive path first, since it is the only consumer
currently actuating on this code at all; only extend to the ambient `tracking.enabled`/
`tracking.actuate` path afterward, once both the sensing-quality and the ranking-quality questions have
independent live evidence. Record the outcome as a dated revision inside ADR 136 (its heatdive
consumer owns the live-trial history for this code path), cross-referenced from here — not by
rewriting this section after the fact.

### Testing plan

- Unit: once Phase 2's ranked pool replaces the hard mask switch, `_detect_targets` returns the full
  merged, color-tagged candidate list; a synthetic frame with one red contour at the edge and one
  green contour at center exercises exactly the disagreement case this section exists to catch.
- Unit: with `tracking.ranked_lock_priority: false` (default), confirm the new ranking function's
  output is computed and logged but `_select_target`'s actual return value is provably unchanged from
  today's mask-switch behavior, across both the agreement and disagreement cases above.
- Shadow live trial (required before Phase 2 ships as anything but inert logging): one full session
  with real dives, reading the rate-limited Phase 1 log to answer "how often, and by how much" before
  writing Phase 2's ranking function at all — if the gap essentially never fires in practice, Phase 2
  may not be worth building.
- Live trial (required before Phase 3): compare selected-target identity and resulting roll direction,
  Phase 2 shadow log vs. actual `_select_target` output, across a full heatdive session, before
  flipping `tracking.ranked_lock_priority` to `true`.

---

## Adaptive Optimization (Future, Non-V1)

This capability is a follow-on optimization phase and is **not required** for initial delivery.
V1 remains deterministic:

1. global acquire,
2. local ROI tracking,
3. bounded expansion and fallback ladder.

Scope for adaptive methods (contextual bandit or RL policy):

1. tune ROI size and expansion factor,
2. tune scan cadence and OCR refresh interval,
3. tune fallback thresholds (`miss_count`, confidence cutoffs).

Out of scope for adaptive methods in this document:

1. direct roll-key actuation decisions,
2. replacement of deterministic safety gates,
3. uncontrolled online exploration in live runs.

Readiness criteria before enabling adaptive policy experiments:

1. stable deterministic baseline metrics captured from live sessions,
2. replay harness with recorded frames and expected tracking outcomes,
3. objective score balancing center-hold quality vs OCR/compute cost,
4. guardrails that clamp policy outputs to safe parameter ranges.

Suggested reward/objective components for future work:

- positive: target visible persistence, lower `abs(error_norm)`, reduced reacquire events,
- negative: OCR runtime cost, command jitter, target-loss events.

---

## Open Questions

1. Should red lock be mandatory for control, or allow green fallback by default? See Selection
   Hardening (above) for a concrete related gap: today red lock isn't just preferred, it hard-excludes
   green candidates regardless of position.
2. Should tracking be active during all J-20 mission phases or only attack sub-phase?
3. Is altitude guard required in v1 or deferred behind a config flag?
4. Should target-tracking output feed future behavior-tree blackboard inputs directly?
5. Should adaptive optimization start as offline replay-only before any live tuning mode?
6. Should color priority (Selection Hardening, above) be capped to a maximum off-center/off-last-
   position distance, or dropped entirely in favor of ranking red and green candidates in one pool
   with only a soft tie-break preference for red? Deferred to that section's Phase 1 shadow log
   rather than decided here.

---

## Related Documents

- `docs/adr/027-j20-target-painting-mode.md`
- `docs/adr/028-enemy-quadrant-detection-and-nose-orientation.md`
- `docs/adr/069-eject-impulse-rotation-and-ballistic-descent.md` — the
  `NOSE_DOWN` pitch primitive a pitch-needing consumer of this design
  reuses (see Non-Goal 2).
- `docs/adr/136-heatseeker-dive-invokable-mode.md` — second, direct
  consumer of this tracker's sensing and roll controller, outside the
  `tracking.enabled`/`tracking.actuate` ambient path (see Safety and
  Gating Rules).
- `docs/hldd/003-enemy-quadrant-detection-hldd.md`
- `docs/hldd/011-acs-mode-hldd.md` — extends this design's sensing/roll
  core with a pitch channel and a lock-confirmation state for boresight
  engagement.
- `docs/hldd/013-minimap-center-seeking-navigation-hldd.md` — source of the shadow-first,
  phase-gated rollout style Selection Hardening (above) follows.
- `wingman/tracker.py` — `TargetTracker._detect_targets`/`_select_target`, the color-exclusion-
  before-centrality gap Selection Hardening documents.
- `test_screenshots/ALTITUDE_SPEED.png` — reference frame motivating Selection Hardening (see
  Target marker visual characteristics and Selection Hardening, above); a post-match nametag
  overlay, not a frame the live tracker consumes.
- `docs/architecture.md`
