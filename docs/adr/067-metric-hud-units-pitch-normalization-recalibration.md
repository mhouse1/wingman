# ADR 067 — Metric HUD Units: Corrected Pitch Normalization and Band Recalibration

| Status   | Date       | Wingman Version |
|----------|------------|-----------------|
| Accepted | 2026-08-09 | 1.7.1           |

*Accepted 2026-08-09 — every validation-plan item met: 50-session archive
replay (decision 2), `make tp` fully green including the ADR 044/045 runtime
gates, live sine-band eject confirmations (2026-08-08 19:30 session, 2 of 2
via steep band), and `steep_dive_min_sin` settled at 0.8 from the replay
corpus.*

Extends [ADR 038](038-game-battle-altitude-speed-signals-for-phase3-and-eject-dive.md)
(Draft) and [ADR 058](058-eject-dive-confirmation-via-raw-descent-rate.md)
(Accepted). Neither is modified. This ADR supersedes ADR 038 on one point only —
the stated HUD units — and takes up the calibration decision ADR 058 explicitly
deferred: *"If later flight data shows the ratio is systematically compressed by
a units mismatch, that is a separate calibration decision and warrants its own
ADR."* This is that ADR.

## Context

### The HUD is metric

ADR 038 states the telemetry HUD renders *"speed on top in MPH, altitude below
in feet"* and that *"units are fixed by the HUD"*. Visual inspection of the
archived integration screenshots (2026-08-08) shows the labels actually read
**KPH** and **m**:

- `test_screenshots/integration_test/P1_030_BATTLE_HUD_MISSILES_4.png` — "1022 KPH / 554 m"
- `test_screenshots/integration_test/P1_060_BATTLE_HUD_HEALTH_ALIVE_MISSILES_4.png` — "1067 KPH / 1969 m"

The OCR pipeline discards the unit labels positionally (each row's largest
numeric token is kept), so no code path ever read them. Every downstream
consumer inherited the imperial assumption: variable names (`alt_rate_fps`,
`speed_mph`), config keys (`max_speed_mph`, `max_altitude_ft`), and — the only
place it matters behaviorally — the sine-ratio normalization.

### The compression factor is 5.3x

`pitch_band()` computes `sin(flight path angle) = alt_rate / (speed * MPH_TO_FPS)`.
With metric readings the correct conversion is `speed / 3.6` (KPH to m/s), not
`speed * 1.4667`. The denominator is therefore `3.6 * 1.4667 = 5.28x` too
large, and every computed ratio is compressed by that factor.

Reinterpreting the flight evidence already recorded in ADR 058 (session
2026-07-30 15:47, commanded full nose-down at speed):

```
15:47:49  alt 4579  speed 1264   descending -232 ft/s
15:47:52  alt 3411  speed 1782   descending -389 ft/s   diving hard
15:48:07  alt 3353  speed 2101   descending -465 ft/s   dives once released
```

| Reading            | Legacy ratio (as logged) | Legacy angle | Metric ratio | Metric angle |
|--------------------|--------------------------|--------------|--------------|--------------|
| -389 at 1782       | 0.149                    | -8.6 deg     | 0.786        | -51.8 deg    |
| -465 at 2101       | 0.151                    | -8.7 deg     | 0.797        | -52.8 deg    |
| -232 at 1264       | 0.125                    | -7.2 deg     | 0.661        | -41.4 deg    |

A hard commanded dive that the legacy normalization scored as an 8-degree
descent is a 52-degree dive under the correct units — physically consistent
with what the aircraft was visibly doing.

### The compression explains ADR 058's standing caveat

ADR 058 was accepted with the caveat that the raw descent-rate path, not the
sine band, does essentially all the confirming. The compression is why:

- With `steep_dive_min_sin: 0.8` evaluated against a 5.28x-compressed ratio,
  confirmation requires a *true* ratio of 4.2 — physically impossible for an
  aircraft whose displayed speed reflects its motion. The band could only fire
  during stall transients, where displayed forward speed collapses below the
  actual fall rate (exactly the 2026-07-28 case: "-376 ft/s at 294 MPH,
  ratio 0.87").
- ADR 058's replay of 255 telemetry samples found a maximum compressed ratio of
  0.346 *including a terminal ground-impact dive*. Corrected, that maximum is
  1.83 — past vertical saturation, i.e. a falling aircraft descending faster
  than its displayed forward speed. The sine band was structurally unreachable
  at speed; the raw-rate fallback (ADR 058 decision 1) was compensating for a
  units bug, not for OCR noise.
- The legacy "level" band (`|ratio| <= 0.15` compressed) actually spans true
  flight-path angles up to 52 degrees. Eject corrections gated on `band=level`
  were flying far steeper than the label implied.

### Early corrected-pipeline evidence (live session 2026-08-08 18:40)

The first battle of the first live session on the corrected display path
produced a physically coherent zoom-climb-and-stall arc — the kind of
self-consistent sequence the compressed normalization could never show:

```
18:40:35  Altitude: 1485  | Speed: 959   | Nose: +41° (climb)
18:40:44  Altitude: 3945  | Speed: 1861  | Nose: +62° (steep_climb)
18:40:47  Altitude: 5280  | Speed: 2061  | Nose: +66° (steep_climb)
18:41:05  Altitude: 11278 | Speed: 903   | Nose: +44° (climb)
18:41:14  Altitude: 12222 | Speed: 166   | Nose: +22° (climb)
18:41:17  Altitude: 12231 | Speed: 59    | Nose: +3° (level)
18:41:20  Altitude: 12167 | Speed: 135   | Nose: -40° (dive)
18:41:23  Altitude: 11982 | Speed: 279   | Nose: -90° (steep_dive)
18:41:32  Altitude: 10393 | Speed: 967   | Nose: -90° (steep_dive)
```

Afterburner launch climbing at 41-66 degrees; the climb angle decays as speed
bleeds from 2061 to 166 KPH (energy trade); apex at 12,231 m nearly stationary
(level at 59 KPH); then a stall drop where the ratio saturates at -90 degrees
because the aircraft falls faster than its displayed forward speed — the
documented saturation case, carrying no angular information but correctly
labeled steep. The saturated samples (e.g. -178 m/s at 486 KPH = 135 m/s,
ratio 1.32) also demonstrate that corrected ratios past 0.8 occur readily in
real descents, supporting threshold reachability under decision 2.

### Implementation state (v1.7.1 working tree)

Both the display path and the decision path now use the metric normalization:

- `pitch_angle_deg()` — nose-angle log display, `KPH_TO_MPS`, band label from
  the corrected angle (`pitch_band_from_angle_deg()`).
- `pitch_band()` — the eject decision path, migrated per decision 2 below
  after the archive replay validated threshold reachability.
- The plausibility-filter envelopes are untouched (see decision 2a).

## Decision

**1. `pitch_angle_deg()` uses the metric normalization (implemented).**

Display and logging only. Parameters and docstring state the metric units
explicitly; the screenshot evidence is cited in the docstring.

**2. Migrate `pitch_band()` to the corrected normalization; keep
`steep_dive_min_sin` at 0.8 (implemented, validated by archive replay).**

Validated by replaying all 50 archived session logs (`logs/wingman_*.log`)
through the real `TelemetryProcessor`: 15,099 accepted rate samples, 498 eject
windows (`FSM: entering GAME_BATTLE_EJECT` to `eject_and_dive complete`).
Corrected-ratio results:

| Population                | n     | p25   | p50   | p75   | p95   |
|---------------------------|-------|-------|-------|-------|-------|
| Normal flight             | 9341  | -0.05 | +0.44 | +0.80 | +0.94 |
| Eject windows, descending | 4867  | -1.25 | -0.95 | -0.49 | -0.09 |

Per-eject sustained descent (best pair of consecutive samples, matching
`confirm_consecutive: 2`), sine-band confirmations by threshold:

| Threshold | Ejects confirming (of 495 with rate data) |
|-----------|-------------------------------------------|
| 0.60      | 437 (88%)                                 |
| 0.70      | 424 (86%)                                 |
| 0.75      | 416 (84%)                                 |
| 0.80      | 410 (83%)                                 |

Decisions the data settles:

- **`steep_dive_min_sin` stays 0.8.** Sustained eject descents cluster at
  ratios of -1.2 to -2.9 — far past the threshold — so lowering it buys
  almost nothing (0.75 adds 6 of 495 ejects) while widening steep
  classification of ordinary combat descents. The earlier candidate of 0.75,
  inferred from two mid-maneuver samples, is refuted by the full corpus.
- **The corrected ratio is an ordinal steepness signal, not an exact sine.**
  It routinely exceeds |1| (normal-flight p95 is +0.94; eject median -0.95):
  the displayed speed under-represents actual motion during hard maneuvers.
  The `pitch_band()` clamp at ±1.5 and the display saturation at ±90 degrees
  in `pitch_angle_deg()` are therefore load-bearing, and the band thresholds
  discriminate on ordering, which the replay shows they do well.
- **Test migration**: eject closed-loop stub rates were rescaled to metric so
  each scenario keeps its band intent (e.g. the shallow-dive reversal case
  moved from -200 to -60, ratio -0.36, still the dive band).

**2a. The altitude plausibility gate keeps the legacy loose envelope.**

The gate's premise — vertical speed cannot exceed total speed — is *false*
for displayed speed: the replay shows sustained descents at 1.2 to 2.9 times
displayed speed. The legacy `MPH_TO_FPS` factor made the gate 5.28x looser
than metric physics would dictate, which is *accidentally correct*: a
metric-tight gate would reject genuine stall and dive telemetry wholesale
(re-creating the ADR 058 clamp defect by another route). The gate keeps the
legacy factor deliberately; it is an empirically tuned envelope, not physics.
`_altitude_bound_fps` is not changed.

**3. The ADR 058 raw descent-rate path is retained unchanged.**

It confirmed correctly throughout and is the proven fallback for stall cases
where the ratio saturates past 1.0 and carries no angular information.

**4. Config keys and variable names are not renamed in this ADR.**

`max_speed_mph`, `max_altitude_ft`, and the `_fps`/`_mph` naming are misnomers,
but every envelope value was tuned in raw display units and is numerically
correct as-is. Renaming is a cosmetic sweep with regression risk across config,
tests, and replay corpora; if done at all it belongs in its own change with no
behavioral component.

## Validation plan (gate to Accepted)

1. ~~Replay archived telemetry through the corrected normalization.~~ **Done,
   exceeded scope**: all 50 archived sessions (15,099 samples, 498 eject
   windows) rather than the single 255-sample session; results in decision 2.
2. ~~`make tp` green, including the ADR 044/045 runtime gates.~~ **Done
   2026-08-09** — after pinning the ADR 045 lane's capture to the config
   region (the presenter's canvas), removing the lane's hidden dependency on
   the game window's position.
3. ~~At least one live flight session on the corrected eject pipeline showing
   sine-band eject confirmations.~~ **Done** — session 2026-08-08 19:30
   (11m09s, 2 missions, 2 ejects): **both ejects confirmed via the steep
   band**, versus 1 of 64 across the entire archived-log era. Second eject,
   end to end in 17 seconds:

   ```
   19:34:11,757  MISSILES EMPTY — cancelling mission and ejecting
   19:34:13,262  Altitude: 11890 | Speed: 692 | Nose: +41° (climb)
   19:34:19,263  Altitude: 12333 | Speed: 214 | Nose: +17° (climb)
   19:34:22,263  Altitude: 12310 | Speed: 127 | Nose: -6° (level)
   19:34:25,265  Altitude: 12200 | Speed: 154 | Nose: -53° (steep_dive)
   19:34:28,264  Altitude: 11993 | Speed: 335 | Nose: -90° (steep_dive)
   19:34:28,498  eject_and_dive — dive confirmed post-release via steep band
                 (alt rate -69 ft/s, 2 consecutive, 2 re-entry available)
   ```

   The trajectory is physically coherent throughout: momentum climb after the
   eject command, apex, nose-over, steep dive. The over-rotation guard (ADR
   058 d12) fired correctly twice during the nose-down holds, and the
   plausibility filter rejected 5 OCR-garbage readings (e.g. altitude
   2,583,416) without disturbing the confirmations. Note the controller log
   labels rates "ft/s" — the value is m/s; stale label, tracked as cleanup.
4. ~~Set `steep_dive_min_sin` from that data.~~ **Done**: stays 0.8, settled
   by the archive replay (410 of 495 ejects confirm; lowering refuted).

## Consequences

- The eject closed loop gains a working sine-band confirmation path at speed,
  instead of relying solely on the raw-rate fallback.
- Corrected band labels make eject correction logs meaningful: `band=level`
  will once again mean approximately level.
- Risk: threshold recalibration changes when corrective nose-down re-issues
  stop; a wrong threshold either re-introduces ADR 058's blind re-issue loop
  (too high) or confirms shallow dives as steep (too low). Mitigated by the
  validation plan and by the retained raw-rate path.
- ADR 038's design analysis (sections quoting ft/s and MPH) remains readable
  as written — its numbers are correct in raw display units; only the unit
  names and the sine normalization constant were wrong.
