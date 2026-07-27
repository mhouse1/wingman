# ADR 038 - Integrate Altitude and Speed Signals in GAME_BATTLE

| Status   | Date       | Wingman Version |
|----------|------------|-----------------|
| Draft    | 2026-07-24 | 1.6.24          |

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
its 120 s safety timeout instead of producing a prompt crash.

Adding altitude and speed as first-class runtime signals enables closed-loop
verification of the eject sequence and more adaptive Phase 3 behavior while
preserving current FSM ownership.

## Decision

Integrate altitude and speed extraction into GAME_BATTLE analysis and expose both values
as normalized signals for controller and Phase 3 policy consumption.

The integration will:

- Add stable per-cycle altitude and speed readings with confidence and freshness metadata
- Store smoothed values and short history windows for trend-aware decisions
- Keep legacy behavior as fallback when telemetry confidence is below threshold
- Use these signals immediately to close the loop on eject_and_dive: confirm
  from telemetry trends that the dive and afterburner actually engaged, and
  re-issue the inputs when they did not

## Scope

In scope:

- GAME_BATTLE-only extraction pipeline for altitude and speed
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

Validated against both archived telemetry frames (530 MPH and 27681 feet, and
957 MPH and 27123 feet): both numbers captured in full with margin.

### Day and Night Map Tuning

Maps run in both day and night lighting. The HUD telemetry text is the same
green in both, but the background behind the strip swings from bright terrain
(day) to near-black sky (night), which changes grayscale contrast and can flip
Otsu thresholding behaviour. The current gray-plus-Otsu preprocessing variants
are therefore provisional.

Final preprocessing is tuned offline against an operator-supplied labeled
corpus of 20+ archived screenshots spanning both lighting regimes and the full
altitude and speed digit range (3 to 5 digits), stored under
`test_screenshots/telemetry/`. Ground truth is filename-encoded: the operator
names each file with what they read off the frame (speed, altitude, and any
conditions such as lighting), making the filename the human reference label.
`manifest.yaml` is the normalized machine-readable form, built by parsing the
filenames and independently verifying each frame visually. Disagreements
between the filename label, visual inspection, and pipeline OCR are flagged for
operator review, never silently resolved — each one is either a labeling slip
or a real OCR failure mode worth keeping. The two existing frames (one day,
one night) seed the corpus. Candidate preprocessing variants, in tuning order:

- gray and Otsu-binary (current)
- HSV green-isolation mask — already verified to isolate the HUD text cleanly
  on both the existing day and night frames

Acceptance gate: both values read exactly on at least 95 percent of corpus
frames, and any failed read must degrade to no value rather than a wrong
number, so the Safety and Fallback Rules see a stale signal instead of a false
one. The corpus check joins the real-OCR lane (`make ocr`).

## Implementation Plan

1. Replace the two overlapping `ALTITUDE`/`ALTITUDE_SPEED` crops with the single
   combined `ALTITUDE_SPEED` crop, and add a telemetry OCR worker in
   `analyzer.py` that reads both values in one pass (see Crop and Extraction
   Design).
2. Tune OCR preprocessing against the day/night screenshot corpus and add the
   corpus accuracy test to the `make ocr` lane (see Day and Night Map Tuning).
3. Add normalization and smoothing layer with bounded history buffers.
4. Publish signals via analyzer state output for GAME_BATTLE cycles.
5. Update controller eject_and_dive with closed-loop input verification —
   the goal is the fastest possible crash, not terrain avoidance:
   - altitude trend not falling while NOSE_DOWN is held → re-issue nose-down
     (aircraft is flying straight instead of diving)
   - speed trend not rising after AFTERBURNER is pressed → re-press afterburner
     (activation was missed)
6. Add logging and performance counters for signal confidence and stale-read frequency.
7. Add integration tests with screenshot sequences for high and low altitude-speed cases.

## Safety and Fallback Rules

- If confidence is below threshold, keep the previous stable value for a short grace window.
- If data is stale beyond maximum age, disable altitude-speed dependent optimizations.
- Never block the main loop on altitude-speed OCR; extraction remains asynchronous.
- On extraction failure, retain existing eject_and_dive baseline behavior.

## Consequences

Positive:

- Better tactical context for Phase 3 behavior policies
- Faster missiles-out respawn cycle: failed nose-down or afterburner inputs
  during eject are detected and re-issued instead of the aircraft flying
  straight until the 120 s timeout
- More explainable controller actions via explicit flight-envelope signals

Trade-offs:

- Additional OCR workload and tuning overhead
- Need for screenshot fixtures across HUD variants and resolutions
- Threshold calibration effort to avoid over-conservative behavior

## Test Strategy

Required tests:

- Unit tests for parsing and normalization edge cases
- Day/night corpus accuracy tests: exact-match reads across the 20+ labeled
  frames spanning both lighting regimes (see Day and Night Map Tuning)
- Lifecycle tests confirming non-blocking behavior and clean shutdown
- Timed screenshot replay integration tests for altitude-speed driven decisions
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
- ADR 033 - Phase 3 architecture recommendations
- ADR 034 - Two-tier performance regression detection
- ADR 037 - Timed screenshot replay integration testing
