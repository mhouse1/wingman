# ADR 038 - Integrate Altitude and Speed Signals in GAME_BATTLE

| Status   | Date       | Wingman Version |
|----------|------------|-----------------|
| Draft    | 2026-07-27 | 1.6.24          |

## Context

GAME_BATTLE decision logic currently relies on OCR and event signals that do not include
explicit altitude and speed telemetry. This limits tactical quality in two areas:

- Phase 3 behavior policies that depend on flight envelope awareness
- the eject-and-dive sequence (`eject_and_dive()`, ADR 056), which deliberately
  crashes the aircraft when missiles are exhausted so it respawns with fresh
  ammunition — the dive must be as fast as possible to minimise dead time
  before the respawn

The eject sequence is currently open-loop: NOSE_DOWN and AFTERBURNER are held
on timers with no feedback that either input took effect. Observed failure
modes: the aircraft sometimes flies straight instead of diving, and the
afterburner sometimes never engages — either one stretches the sequence toward
its 120 s safety timeout instead of producing a prompt crash. A third failure
mode is directional: because the hard-coded nose-down input has no feedback on
the resulting pitch, it can under- or over-rotate, and the aircraft has been
observed flying out of the arena boundary instead of into the ground.

Adding altitude and speed as first-class runtime signals enables closed-loop
verification of the eject sequence and more adaptive Phase 3 behavior while
preserving current FSM ownership.

## Decision

Integrate altitude and speed extraction into GAME_BATTLE analysis and expose both values
as normalized signals for controller and Phase 3 policy consumption.

The integration will:

- Add stable per-cycle altitude and speed readings with confidence and freshness metadata
- Store smoothed values and short history windows for trend-aware decisions
- Balance processing speed against read accuracy in the ADR 030 style: the raw
  OCR layer is tuned for cycle time, and occasional bogus readings are
  acceptable because a plausibility filter rejects them before decision logic
  ever sees them (see Plausibility Filter)
- Derive the altitude rate of change and combine it with current speed to
  estimate nose direction, replacing eject_and_dive's hard-coded open-loop
  nose-down with closed-loop pitch verification (see Nose-Direction
  Estimation)
- Keep legacy behavior as fallback when telemetry confidence is below threshold
- Ship with automated tests and processing-time performance history from the
  first release: telemetry OCR timings join the PerformanceTracker per-crop
  session history and the two-tier regression gates (ADR 034)

## Scope

In scope:

- Extraction pipeline for altitude and speed, active on every GAME_BATTLE
  cycle — the signal serves eject_and_dive, Phase 3 behavior trees, and
  future GAME_BATTLE consumers, not the eject sequence alone
- Runtime signal model with value, confidence, timestamp, and staleness
- Controller closed-loop input verification for eject_and_dive using altitude
  and speed trends
- Telemetry and tests for transition-safe, non-blocking integration

Out of scope for this ADR:

- Full HUD parsing of additional flight indicators
- Replacing existing incoming or respawn detection flows
- Reworking FSM state definitions

## Data Model

Altitude and speed signals expose:

- value: numeric reading in configured unit
- confidence: OCR confidence score for current reading
- ts: sample timestamp
- age_s: derived freshness in seconds
- stable_value: smoothed value used by decision logic
- trend: rising, falling, or flat based on recent window
- rate: signed rate of change per second (feet per second for altitude, MPH
  per second for speed) — the input to nose-direction estimation. Rate is
  derived from post-filter accepted readings using actual sample timestamp
  deltas, deliberately not from stable_value: smoothing then differencing adds
  several seconds of lag on a 1.5 s tick, too slow for dive verification. OCR
  completions are asynchronous, so assumed tick intervals are also wrong — at
  roughly 880 feet per second a 200 ms timing error is about 180 feet.
  stable_value and trend remain the inputs for slower Phase 3 decisions.

## Crop and Extraction Design

Speed and altitude render as two left-aligned numeric lines stacked in a single
HUD block (speed on top in MPH, altitude below in feet). Because the two values
sit within one strip roughly 56px tall, they are read from a **single combined
crop** (`ALTITUDE_SPEED`) in one OCR pass rather than two separate per-value
crops.

Rationale — each crop in `analyzer.py` is OCR'd via `reader.readtext()`, and the
numeric workers run it on two preprocessed variants (gray and Otsu binary). Two
separate crops therefore cost roughly four `readtext()` invocations per tick
against one combined crop's two. For crops this small the fixed detection and
recognition dispatch dominates over pixel count, so a single crop roughly halves
telemetry OCR time — directly serving the non-blocking requirement above. The
second line adds only one extra recognition box to a detection pass that already
scans the whole strip.

Extraction:

- One `readtext(img, detail=1)` pass over the combined crop. The `MPH` and
  `feet` labels are read as ordinary text and discarded by taking each row's
  leading digit run, so they need not be cropped out. (A digit allowlist was
  tried and rejected: it does not suppress the label glyphs, it forces them
  into junk digits — `530 MPH` read as `530011`.)
- Split the returned boxes into rows by bounding-box vertical centre: the upper
  row is speed, the lower row is altitude. A single visible row is assigned by
  which half of the crop it occupies.

Calibration — the numbers are left-aligned, so the leading digit sits at a fixed
x while the value grows rightward. Altitude ranges across three to five digits;
the crop must hold five digits plus margin or the trailing digit is clipped (the
prior 54px `ALTITUDE` crop read `27681` as `2768`). The combined crop supersedes
the earlier overlapping `ALTITUDE` and `ALTITUDE_SPEED` definitions:

```yaml
ALTITUDE_SPEED:
  coords:
  - [0.309, 0.626]
  - [0.362, 0.673]   # 102px by 56px at 1920x1200
```

Validated at design time against two archived telemetry frames (530 MPH and
27681 feet, and 957 MPH and 27123 feet): both numbers captured in full with
margin. Those working frames have since been superseded by the labeled corpus
described below.

### Day and Night Map Tuning

Maps run in both day and night lighting. The HUD telemetry text is the same
green in both, but the background behind the strip swings from bright terrain
(day) to near-black sky (night), which changes grayscale contrast and can flip
Otsu thresholding behaviour. The current gray-plus-Otsu preprocessing variants
are therefore provisional.

Final preprocessing is tuned offline against an operator-labeled corpus of 17
archived screenshots spanning both lighting regimes, stored under
`test_screenshots/telemetry/`. Ground truth is filename-encoded — the operator
read each value directly off the frame and wrote it into the name:

```
telemetry<ID>_<YYYYMMDD>_<HHMMSS>_spd<value>_alt<value>_<day|night>.png
```

- `<ID>` is a single letter (A through R; K unused after duplicate removal)
  serving as a short stable handle for discussing individual frames
  ("telemetryC misreads altitude").
- The original capture timestamp is retained so each labeled frame traces
  back to its raw capture.
- `spd` is in MPH and `alt` in feet; units are fixed by the HUD and not
  encoded in the name.
- A value the operator cannot read off the frame is written as `na` rather
  than omitted — such frames exercise the no-read path (last accepted value
  retained, staleness growing) rather than counting against read accuracy.

Corpus coverage as labeled: 5 day frames and 12 night frames; speed spans
214 to 1355 MPH (3 to 4 digits), altitude spans 1076 to 27164 feet (4 to 5
digits). Known gap: no frame with a 3-digit altitude (below 1000 feet) — one
should be captured and added rather than treating sub-1000-foot reads as
covered.

`manifest.yaml` is the normalized machine-readable form, built by parsing the
filenames and independently verifying each frame visually. Disagreements
between the filename label, visual inspection, and pipeline OCR are flagged for
operator review, never silently resolved — each one is either a labeling slip
or a real OCR failure mode worth keeping. Candidate preprocessing variants, in
tuning order:

- gray and Otsu-binary (current)
- HSV green-isolation mask — already verified to isolate the HUD text cleanly
  on both the existing day and night frames

Acceptance gate: tuning balances read accuracy against per-tick processing
time rather than chasing perfect reads — the ADR 030 lesson is that a cheap
reader plus a rejection filter beats an expensive reader pursuing zero errors.
Both values must read exactly on at least 90 percent of corpus frames (at the
current 17 frames this permits one failing frame). A wrong number on the
remaining frames is acceptable at the raw layer: the plausibility filter, not
the OCR stage, is responsible for keeping bogus values away from decision
logic. The corpus check records both accuracy and per-frame processing time
and joins the real-OCR lane (`make ocr`).

## Plausibility Filter — Tolerating Bogus Reads (ADR 030 Pattern)

ADR 030 solved the same problem for health OCR: stray digits produce
wrong-but-plausible-looking numbers (`224` read as `4224`), and the fix was
not a better reader but a self-calibrating rejection filter in front of
decision logic. Telemetry adopts the same pattern, with the bound derived from
physics rather than a rolling ceiling:

- **Altitude**: vertical speed can never exceed total speed. A new reading
  implying a climb or descent rate greater than the current speed (converted
  to feet per second, plus margin) is rejected. At 600 MPH (roughly 880 feet
  per second), even a vertical dive moves at most about 1300 feet per 1.5 s
  tick; a stray-digit misread such as `2768` becoming `27681` implies a jump
  of tens of thousands of feet in one tick and is rejected outright.
- **Speed**: readings implying acceleration beyond a configured maximum MPH
  change per tick are rejected.
- **Filter ordering**: speed is filtered first against its own envelope bound
  (last accepted speed plus the configured per-tick change limit); the
  altitude bound then uses the last accepted speed, never the raw speed read
  from the same tick — otherwise one bogus speed read (`1355` as `13550`)
  would inflate the altitude gate tenfold and admit exactly the spikes being
  filtered. If speed is stale, the altitude bound falls back to the aircraft's
  global maximum speed: conservative, but still tight enough to reject
  multi-thousand-foot jumps.
- **Rejection behavior**: a rejected reading returns the last accepted value
  and lets `age_s` grow, exactly as ADR 030 returns the last accepted health
  value. Sustained rejection escalates to the stale-data fallback rules.
- **Legitimate fast changes must pass**: a maximum-rate dive during
  eject_and_dive is the very signal being measured, so the bound comes from
  current speed and physics, never from recent statistical variance — the
  analogue of ADR 030 accepting instant health-icon restores.
- **Seeding and re-seed**: the first reading seeds the window unconditionally
  (there is no prior state to check against). If several consecutive readings
  are then rejected, the seed itself is suspect — clear the window and
  recalibrate from the next reading, so one bogus seed cannot lock out all
  subsequent real values. Rejection counts are logged and tracked as a
  performance counter.

## Nose-Direction Estimation

The end goal of this telemetry is closed-loop nose-direction control. Pitch
relates the two measured values: altitude rate is approximately speed times
the sine of the pitch angle. With speed read from the same crop, the observed
altitude rate bounds the current nose direction without any additional HUD
parsing:

- level flight: altitude rate near zero regardless of speed
- 20 degrees nose down at 600 MPH: descent around 300 feet per second
- vertical dive at 600 MPH: descent around 880 feet per second

The same relation holds nose up: commanding 90 degrees nose up must produce a
much higher climb rate than 20 degrees nose up, so the rate distinguishes
steep from shallow attitudes, not merely rising from falling.

One limitation shapes the correction logic: sine is symmetric about vertical,
so a shallow descent rate cannot distinguish under-rotation (20 degrees nose
down) from over-rotation past vertical (160 degrees). A blind nose-down
re-issue fixes the first case and worsens the second — potentially driving
the aircraft toward the inverted attitude behind the arena-exit failure.
Corrections are therefore measure-correct-measure rather than fixed
re-issues: after NOSE_DOWN is issued, the descent rate must reach the
steep-dive band for the current speed within a verification window; if it
does not, issue a short corrective input, re-check whether the descent rate
improved, and reverse the correction direction if it worsened. A near-zero
rate (flying straight) is unambiguous and simply re-issues nose-down.

Corrections require contrary evidence, never absence of data: a maximum-rate
dive is the worst OCR environment (motion blur, terrain filling the crop at
low altitude), so if readings stop arriving mid-dive the sequence falls back
to the existing timer-driven behavior instead of correcting against stale
data — correcting on staleness invites oscillation. Phase 3 policies consume
the same estimate read-only for envelope awareness.

## Module Placement and Signal API

The filter, smoothing, rate derivation, and pitch-band estimation are pure
computation — no OCR, no threads, no game state. They live in a standalone
`wingman/telemetry.py` module following the `crop_region.py` precedent (no
internal imports, safe to import anywhere), so every rule in this ADR is
unit-testable with plain numbers and no OCR or threading fixtures:

- `analyzer.py` feeds it raw readings with timestamps as OCR completes and
  stores the returned state under `_telemetry_lock`.
- Consumers read one atomic snapshot: a single `get_telemetry()` accessor
  returns an immutable snapshot (both values, rates, confidence, timestamps)
  captured under one lock acquisition. It replaces the separate
  `get_speed()`/`get_altitude()` accessors — two separate calls can return a
  torn pair from different cycles, and nose-direction estimation divides
  altitude rate by speed, so the pair must come from the same cycle.
- All tuning thresholds — spike margin, maximum MPH change per tick,
  steep-dive band fraction, verification window, stale age — live in a
  `telemetry:` block in `config.yaml` so they are calibratable without code
  changes.

## Implementation Plan

1. Replace the two overlapping `ALTITUDE`/`ALTITUDE_SPEED` crops with the single
   combined `ALTITUDE_SPEED` crop, and add a telemetry OCR worker in
   `analyzer.py` that reads both values in one pass (see Crop and Extraction
   Design).
2. Tune OCR preprocessing against the day/night screenshot corpus, balancing
   accuracy against processing time, and add the corpus accuracy and timing
   test to the `make ocr` lane (see Day and Night Map Tuning).
3. Implement `wingman/telemetry.py` as a pure module (no internal imports)
   holding normalization, the plausibility filter (ADR 030 pattern),
   smoothing, rate derivation from accepted-reading timestamps, and
   pitch-band estimation, with all thresholds in the `config.yaml`
   `telemetry:` block (see Module Placement and Signal API).
4. Publish signals for every GAME_BATTLE cycle via a single atomic
   `get_telemetry()` snapshot accessor, replacing the separate
   `get_speed()`/`get_altitude()` getters.
5. Update controller eject_and_dive with closed-loop nose-direction
   verification — the goal is the fastest possible crash, not terrain
   avoidance:
   - descent rate not reaching the steep-dive band for the current speed while
     NOSE_DOWN is held → measure-correct-measure adjustment: short corrective
     input, re-check the rate, reverse if it worsened (see Nose-Direction
     Estimation); corrections only on confident contrary readings, never on
     stale data
   - speed trend not rising after AFTERBURNER is pressed → re-press afterburner
     (activation was missed)
6. Record telemetry OCR processing time in the PerformanceTracker per-crop
   session history (`run_*.json`) with regression thresholds in `config.yaml`
   per ADR 034, plus counters for signal confidence, filter rejections, and
   stale-read frequency.
7. Add integration tests with screenshot sequences for high and low altitude-speed cases.

## Safety and Fallback Rules

- If confidence is below threshold, keep the previous stable value for a short grace window.
- Readings rejected by the plausibility filter return the last accepted value
  with growing age; sustained rejection escalates to the stale-data rule below
  and triggers a window re-seed.
- If data is stale beyond maximum age, disable altitude-speed dependent optimizations.
- Never block the main loop on altitude-speed OCR; extraction remains asynchronous.
- On extraction failure or loss of readings mid-sequence, retain the existing
  timer-driven eject_and_dive baseline behavior; corrective inputs are issued
  only on confident readings that contradict the expected band, never on
  absence of data.

## Consequences

Positive:

- Better tactical context for Phase 3 behavior policies
- Faster missiles-out respawn cycle: failed nose-down or afterburner inputs
  during eject are detected and re-issued instead of the aircraft flying
  straight until the 120 s timeout
- Fixes the arena-exit failure: a nose-down input that under- or over-rotates
  is detected from the altitude rate and corrected instead of flying the
  aircraft out of bounds
- More explainable controller actions via explicit flight-envelope signals

Trade-offs:

- Additional OCR workload and tuning overhead
- Need for screenshot fixtures across HUD variants and resolutions
- Threshold calibration effort to avoid over-conservative behavior
- Accepted risk, revisit after first implementation: rate derived from raw
  accepted readings may prove noisy during rapid climbs and descents, where
  the numbers change quickly between samples. Implement as specified first;
  if dive verification proves unstable, revisit the rate derivation (for
  example a short two-point regression or median window) in a follow-up.

## Test Strategy

Required tests:

- Unit tests for parsing and normalization edge cases
- Plausibility-filter unit tests: injected bogus readings rejected, maximum-rate
  legitimate dives accepted, re-seed after consecutive rejections
- Day/night corpus accuracy tests: exact-match reads across the 17 labeled
  frames spanning both lighting regimes (see Day and Night Map Tuning)
- Processing-time performance history: telemetry OCR timings recorded per
  session in PerformanceTracker and gated by the two-tier regression check
  (ADR 034)
- Lifecycle tests confirming non-blocking behavior and clean shutdown
- Timed screenshot replay integration tests for altitude-speed driven decisions
- Replay-lane sequence test (ADR 037): inject a known-bogus frame into a
  replay sequence and assert the decision layer never observes the implausible
  jump — the end-to-end filter property the per-frame corpus test cannot check
- Regression checks for false confidence and stale-data fallbacks

## Alternatives Considered

1. Defer altitude and speed to Phase 3 only.
   - Rejected because eject_and_dive benefits are immediate: the open-loop
     sequence observably misfires (straight flight, missed afterburner) and
     telemetry feedback is the direct fix.

2. Use fixed dive heuristics without telemetry.
   - Rejected because static rules underperform across variable battle conditions.

3. Add full avionics telemetry parsing in one release.
   - Rejected due to scope and validation risk; phased signal integration is safer.

## References

- ADR 024 - Phase 3 behavior tree architecture
- ADR 030 - Health ceiling from repeated OCR readings (the
  cheap-reader-plus-rejection-filter pattern this ADR adopts)
- ADR 033 - Phase 3 architecture recommendations
- ADR 034 - Two-tier performance regression detection
- ADR 037 - Timed screenshot replay integration testing
