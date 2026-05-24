# ADR 038 - Integrate Altitude and Speed Signals in GAME_BATTLE

| Status   | Date       | Wingman Version |
|----------|------------|-----------------|
| Draft    | 2026-05-24 | 1.6.9           |

## Context

GAME_BATTLE decision logic currently relies on OCR and event signals that do not include
explicit altitude and speed telemetry. This limits tactical quality in two areas:

- Phase 3 behavior policies that depend on flight envelope awareness
- ejection and dive recovery flows where low altitude and low speed increase risk

Adding altitude and speed as first-class runtime signals enables safer and more adaptive
behavior while preserving current FSM ownership.

## Decision

Integrate altitude and speed extraction into GAME_BATTLE analysis and expose both values
as normalized signals for controller and Phase 3 policy consumption.

The integration will:

- Add stable per-cycle altitude and speed readings with confidence and freshness metadata
- Store smoothed values and short history windows for trend-aware decisions
- Keep legacy behavior as fallback when telemetry confidence is below threshold
- Use these signals immediately to improve eject_and_dive safety checks

## Scope

In scope:

- GAME_BATTLE-only extraction pipeline for altitude and speed
- Runtime signal model with value, confidence, timestamp, and staleness
- Controller guard updates for eject_and_dive using altitude and speed thresholds
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

## Implementation Plan

1. Add crop definitions and extraction path for altitude and speed in analyzer workers.
2. Add normalization and smoothing layer with bounded history buffers.
3. Publish signals via analyzer state output for GAME_BATTLE cycles.
4. Update controller eject_and_dive gating:
   - avoid aggressive dive when altitude is below safe floor
   - avoid delayed recovery when speed is below minimum controllable threshold
5. Add logging and performance counters for signal confidence and stale-read frequency.
6. Add integration tests with screenshot sequences for high and low altitude-speed cases.

## Safety and Fallback Rules

- If confidence is below threshold, keep the previous stable value for a short grace window.
- If data is stale beyond maximum age, disable altitude-speed dependent optimizations.
- Never block the main loop on altitude-speed OCR; extraction remains asynchronous.
- On extraction failure, retain existing eject_and_dive baseline behavior.

## Consequences

Positive:

- Better tactical context for Phase 3 behavior policies
- Reduced unsafe dive behavior near terrain or at low energy states
- More explainable controller actions via explicit flight-envelope signals

Trade-offs:

- Additional OCR workload and tuning overhead
- Need for screenshot fixtures across HUD variants and resolutions
- Threshold calibration effort to avoid over-conservative behavior

## Test Strategy

Required tests:

- Unit tests for parsing and normalization edge cases
- Lifecycle tests confirming non-blocking behavior and clean shutdown
- Timed screenshot replay integration tests for altitude-speed driven decisions
- Regression checks for false confidence and stale-data fallbacks

## Alternatives Considered

1. Defer altitude and speed to Phase 3 only.
   - Rejected because eject_and_dive benefits are immediate and safety-critical.

2. Use fixed dive heuristics without telemetry.
   - Rejected because static rules underperform across variable battle conditions.

3. Add full avionics telemetry parsing in one release.
   - Rejected due to scope and validation risk; phased signal integration is safer.

## References

- ADR 024 - Phase 3 behavior tree architecture
- ADR 033 - Phase 3 architecture recommendations
- ADR 034 - Two-tier performance regression detection
- ADR 037 - Timed screenshot replay integration testing
