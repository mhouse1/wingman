# ADR 028 — Minimap Enemy Bearing and Overhead Attack Positioning

| Status   | Date       | Wingman Version |
|----------|------------|-----------------|
| Draft    | 2026-08-08 | 1.7.1           |

> **Revision note (2026-08-08):** this Draft originally specified "Enemy Quadrant
> Detection and Nose Orientation" — a 3×3 quadrant split of the `ENEMY_CLOSE_BY`
> crop driving a fixed roll-decision table. It is revised in place (permitted:
> the ADR never left `Draft` and no part of the quadrant design was implemented).
> The filename keeps its original slug. Drivers for the revision: the measured
> single-combined-crop result in ADR 038, the Design 005 screen-space tracker
> (which already implements continuous roll correction), and direct inspection
> of the minimap in archived battle frames. The companion HLDD
> ([Design 003](../hldd/003-enemy-quadrant-detection-hldd.md)) was revised
> together with this ADR.

## Context

The J20 tactic this feature supports: climb to high altitude, then position the
aircraft directly above clustered enemy fighters and hold missile lock. The
near-vertical engagement geometry forces defenders into high-AOA climbs that
most airframes cannot sustain, raising their stall probability, while
maximising target-painting buff uptime (ADR 027). **The effectiveness of the
overhead position is a gameplay hypothesis, not a measured fact** — this ADR
therefore includes instrumentation as part of the decision (§6), and cannot go
`Accepted` without live measurements, per the performance-ADR evidence rule.

Why the quadrant revision was replaced:

1. **Range.** `ENEMY_CLOSE_BY` (coords `0.8764–0.9521 × 0.0822–0.1856`) is a
   small box at the *centre of the minimap*. Quadrant-splitting it yields
   bearing only for enemies already near own position — nothing guides the
   aircraft toward a cluster elsewhere on the map. The capability the tactic
   actually needs is *navigation to the cluster*, which requires the whole
   minimap.
2. **Crop-consolidation precedent (ADR 038).** Replacing two overlapping
   OCR crops with one combined `ALTITUDE_SPEED` crop roughly halved effective
   OCR cost and eliminated an overlap misread (`27681` read as `2768`). The
   precedent transfers in structure, not mechanism: OCR crops save per-call
   dispatch, while HSV mask cost scales with area and stays sub-millisecond
   either way. The single-region win here is one calibration, one detection
   pass serving every consumer, and centroid post-processing that sub-crops
   cannot do — an icon straddling a sub-crop boundary splits its pixel mass
   across regions, whereas a centroid bins cleanly.
3. **Duplication.** The quadrant→roll table coarsely duplicated what
   `Controller.orient_nose_to_target(error_norm)` (Design 005, implemented in
   `controller.py`) already does with continuous proportional control,
   deadband, hold clamping, and cooldown. This revision reuses it as the
   actuator instead.
4. **The altitude section was stale.** The quadrant draft specified a new
   `ALTITUDE` OCR crop read via `_process_health_region`. ADR 038 has since
   replaced altitude/speed reading with the combined `ALTITUDE_SPEED` crop and
   the atomic `get_telemetry()` snapshot. This revision consumes that.

Verified minimap properties (frames `P1_040_BATTLE_HUD_MISSILES_0_HEALTH_ALIVE.png`
and `P1_060_BATTLE_HUD_HEALTH_ALIVE_MISSILES_4.png`, 1920×1200):

- Circular map in the top-right HUD corner (the top-left holds the kill feed).
  Position must be confirmed per device/HUD layout before calibration.
- **Heading-up rotation**: the compass letters sit at different rim positions
  in the two frames while the own-ship marker and view-cone wedge stay fixed
  at centre pointing up. Therefore an icon's angle from the up-axis *is* its
  relative bearing, own position is the map centre, and icons converging on
  centre means directly overhead. No compass math is needed.
- Red icons are enemies; blue icons are friendlies. Other artifacts share the
  region: rim compass letters, capture-point ring badges (red or blue), wreck
  markers, green pickup crosses, and route lines — these must be filtered.

## Decision

### 1. Single `MINIMAP` crop

A new calibrated crop `MINIMAP` bounds the full minimap circle. In step 1,
`ENEMY_CLOSE_BY` and `detect_enemy_red` remain untouched — both crops are numpy
slices of the same captured frame, so the second region adds no capture cost.
In step 2, once live logs show equivalence, the 30-second disengage boolean is
derived from the minimap scan (`radius_frac <= minimap.close_radius_frac`) and
the `ENEMY_CLOSE_BY` crop is retired, leaving one region serving both
consumers.

### 2. `detect_enemy_map_bearing(frame)` (analyzer)

Returns `{bearing_deg, radius_frac, blob_count, pixel_count}`, or a fail-safe
all-`None`/zeros result if the crop is missing or an exception occurs.
Pipeline: circular mask (excludes corners and rim letters) → `enemy_hsv` red
mask including the hue wrap-around band — the identical, field-proven mask
`detect_enemy_red` already runs on this same minimap surface — → connected-
component area band (`min_blob_px`–`max_blob_px`) rejecting badges, wreck
markers, and residual rim art → pixel-mass centroid of surviving components →
polar conversion. `bearing_deg` is measured from the up-axis (positive
clockwise, range −180…180); `radius_frac` is normalised to the mask radius.

### 3. Overhead navigation phases

A pure-logic navigator (no threads, no locks) consumes `(bearing_deg,
radius_frac)` plus the `get_telemetry()` snapshot each battle tick:

| Phase    | Condition                                        | Behaviour                                   |
|----------|--------------------------------------------------|---------------------------------------------|
| Climb    | altitude below `attack_altitude`                 | no steering; existing mission profile climbs |
| Steer    | bearing outside deadzone                         | roll toward cluster via `orient_nose_to_target` |
| Approach | bearing inside deadzone, radius above threshold  | fly straight                                |
| Overhead | radius at or below `overhead_radius_frac`        | hold; exit only above `overhead_exit_frac`  |

The Overhead exit hysteresis exists because the own-ship marker occludes enemy
icons exactly at arrival — a raw threshold would flicker.

### 4. Actuation reuse

`error_norm = clamp(bearing_deg / 90, −1, 1)` is fed to the existing
`Controller.orient_nose_to_target`, whose deadband, gain, hold clamps, and
cooldown are already parameters — coarse-navigation gains come from config.
No new key-injection logic is added.

### 5. Safety gates

- `j20_mission.attack_mode` (default `false`) master switch, plus a
  `attack_mode_dry_run` log-only mode for tuning.
- Active only in `GAME_BATTLE`; suppressed in `GAME_BATTLE_MANUAL` (manual
  takeover always wins, consistent with ADR 027).
- Telemetry snapshot `None`/stale, or altitude below
  `j20_mission.min_safe_altitude` → suppress all steering (CFIT fail-safe,
  carried over unchanged from the quadrant draft).
- Terrain avoidance active (Design 001 `_terrain_avoiding`) → suppress.

### 6. Instrumentation (part of the decision)

- Per-cycle DEBUG log: bearing, radius, phase, issued command.
- Per-mission summary: percent of battle time per phase, mean `radius_frac`.
- Effectiveness A/B, `attack_mode` on vs off: deaths and outcomes per mission
  via MissionStatsTracker (ADR 055), incoming-detection rate from the existing
  PerformanceTracker histograms. These measurements are the acceptance
  evidence for this ADR.

## Consequences

**Positive**

- Whole-map 360° enemy awareness at zero additional capture cost (numpy slice
  of the already-grabbed frame) — the aircraft can navigate *to* the fight,
  not merely nudge when enemies are already close.
- One calibrated region and one detection pass; continuous bearing replaces
  five coarse buckets; no icon-straddles-a-boundary failure mode.
- Reuses the field-proven `enemy_hsv` mask (already validated on this exact
  surface) and the tested `orient_nose_to_target` controller.
- "Directly overhead" is directly observable (icons at map centre) — the
  tactic's goal state needs no inference.
- Changing bearing granularity later (sectors, per-blob tracking) is a code
  change with no recalibration.

**Negative / Trade-offs**

- `MINIMAP` must be calibrated per device/HUD layout.
- Red artifacts (enemy-held capture badges, wreck markers) can contaminate the
  centroid; v1 mitigates with the component area band and accepts residual
  noise until live data sizes the problem.
- The heading-up assumption is load-bearing: if the game offers a north-up
  minimap setting, the bearing math breaks. Verify the setting is fixed.
- Minimap zoom/range behaviour is unverified; if range changes with altitude
  or map, `radius_frac` thresholds need per-map review.
- Step 2 (retiring `ENEMY_CLOSE_BY`) changes the source of a verified
  behaviour — gated on logged equivalence, not assumed.

## Alternatives Considered

**3×3 quadrant split of `ENEMY_CLOSE_BY`** (the previous revision of this ADR)
— rejected: bearing range limited to the map centre, five-bucket coarseness,
and a roll table that duplicates the Design 005 controller.

**Screen-space tracking alone (Design 005)** — complementary, not sufficient:
world-view markers exist only inside the view frustum, so it cannot steer
toward a cluster behind the aircraft or across the map. Design 003 gets the
aircraft to the fight; Design 005 handles terminal alignment.

**Compass-heading OCR and fixed headings** — still rejected: adds OCR load and
heading-tracking state, and is far more brittle than direct icon detection.

**North-up bearing math via rim-letter OCR** — unnecessary: the map is
verified heading-up, so relative bearing is geometric.

**Health drops as an altitude proxy** — still rejected: damage as the safety
signal arrives after the crash.
