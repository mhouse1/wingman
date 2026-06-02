# ADR 046 — INCOMING Template Matching Replacement

| Status   | Date       | Wingman Version |
|----------|------------|-----------------|
| Accepted | 2026-06-02 | 1.6.15          |

## Context

INCOMING missile detection currently uses OCR on the configured INCOMING crop and
detects token substrings (for example MING and ARNING). This path is effective in
some scenes but remains sensitive to OCR noise and incurs repeated per-cycle OCR cost.

Recent runtime logs show a measurable OCR cost for the INCOMING lane and repeated
no-match reads in active runs.

### Baseline Log Excerpts

```text
2026-05-31 19:25:49,807 [DEBUG] Analyzer: Parallel OCR Timings - Extract: 0.00s, Submit: 0.00s | Respawn OCR: 0.15s | Incoming OCR: 0.21s | Health OCR: 0.16s | Flares OCR: 0.12s | Missiles OCR: 0.15s | Total: 0.21s
2026-05-31 19:25:54,391 [DEBUG] Analyzer: Parallel OCR Timings - Extract: 0.00s, Submit: 0.00s | Respawn OCR: 0.14s | Incoming OCR: 0.27s | Health OCR: 0.15s | Flares OCR: 0.11s | Missiles OCR: 0.14s | Total: 0.27s
  incoming        mean 0.22s p95 0.27s    0.90s      -76%   ✅ LARGE IMPROVEMENT
2026-05-31 19:25:49,805 [DEBUG] Analyzer: No match in INCOMING region — raw OCR: binary_otsu_up_1p4='4742'
```

Source: wingman_live.log (2026-05-31 session)

## Decision

Replace OCR-based INCOMING detection with template matching as the primary detection
method.

Decision details:

- Use template matching on the INCOMING crop as the production detection source.
- Use a single canonical source template for tuning and runtime default:
    - c:/dev-tools/github/wingman/test_screenshots/INCOMING3.png
- Normalize both runtime crop and template with Otsu binary conversion before
   scoring, to stabilize matching for the semi-transparent INCOMING banner.
- Keep an optional OCR fallback path behind a config flag for controlled rollback.
- Keep threshold and short debounce rules, but do not add sustained-event dedupe;
   repeated positives are allowed while INCOMING remains visible.
- Log template score, threshold, and final detection decision in runtime telemetry.

## Scope

In scope:

- INCOMING region detection method replacement (OCR to template matching)
- Template assets and threshold tuning for INCOMING warning text
- Telemetry updates for template score and decision outcomes
- Tests for positive, negative, and threshold-boundary behavior

Out of scope:

- RESPAWN/CLICK_TO/other OCR region migrations
- FSM ownership or state-transition model changes
- Controller flare action semantics beyond detection source

## Implementation Approach

1. Add config gates and thresholds:
   - incoming_template_matching_enabled
   - incoming_template_threshold
   - incoming_template_fallback_to_ocr

2. Add template assets and scale range config, starting with a canonical runtime
   source:
   - c:/dev-tools/github/wingman/test_screenshots/INCOMING3.png

3. Implement template evaluation in the INCOMING worker path with:
   - grayscale plus Otsu binarization on both incoming crop and template
   - normalized match score
   - threshold decision
   - optional fallback to OCR when score is below threshold and fallback is enabled

4. Add detection guardrails:
   - minimum interval debounce for repeated positive triggers
   - optional second-frame confirmation when score is near threshold

5. Expand logs and metrics:
   - template score and threshold
   - detection source (template or fallback OCR)
   - per-cycle INCOMING processing time

6. Add tests:
   - known-positive template scenes
   - known-negative scenes
   - threshold-boundary regression cases

## Implementation Contract

The following contract is normative for the first implementation pass.

### Matching Method and Score Semantics

- Matching API: `cv2.matchTemplate`
- Method: `cv2.TM_CCOEFF_NORMED`
- Preprocessing: convert both incoming crop and templates to grayscale, then apply
   `cv2.threshold(..., cv2.THRESH_BINARY + cv2.THRESH_OTSU)`
- Semi-transparent handling: binarization is the normalization layer that reduces
   alpha/transparency presentation variance before template scoring
- Score meaning: higher is better, valid range is approximately -1.0 to 1.0
- Reported score: maximum response value over the evaluated search region

### Initial Thresholds and Confirmation

- Primary positive threshold: 0.82
- Near-threshold band: 0.76 to 0.81
- Near-threshold handling: require one immediate confirmation frame before positive
- Below 0.76: treat as no-template-hit in that cycle

Rationale: these defaults are intentionally conservative to reduce false positives in
flare deployment while preserving room for tuning from live telemetry.

### Runtime Region of Interest

- Source of truth: current INCOMING crop from config (`crops.incoming.coords`)
- Do not use a hardcoded absolute rectangle in code
- Apply template matching only within this crop each cycle

### Template Asset Location and Naming

- Runtime default source list is config-driven, with canonical default:
   - c:/dev-tools/github/wingman/test_screenshots/INCOMING3.png
- Runtime default scale list:
   - 1.0
- Additional sources/scales remain configurable through:
   - incoming_detection.incoming_template_sources
   - incoming_detection.incoming_template_scales

### Fallback Behavior

- Config flags:
   - incoming_template_matching_enabled (default: true)
   - incoming_template_threshold (default: 0.82)
   - incoming_template_fallback_to_ocr (default: true)
- Fallback trigger: template score is below configured threshold and fallback is enabled
- Fallback cadence: run at most once per analysis cycle for INCOMING
- Cooldown: no additional fallback cooldown beyond existing analysis loop timing

### Logging and Telemetry Contract

For each INCOMING evaluation, log these fields in a single structured debug line:

- detector: incoming_template
- template_score: <float>
- template_threshold: <float>
- near_threshold_confirmation: <true|false>
- detection_source: <template|ocr_fallback|none>
- detected: <true|false>
- debounce_suppressed: <true|false>
- incoming_processing_ms: <int>

### Debounce and Trigger Guardrails

- Minimum positive-trigger interval: 500 ms (configurable)
- If a positive result occurs inside the debounce window, mark as suppressed for that
   cycle only
- Outside the debounce window, emit positive events repeatedly while INCOMING remains
   present (no sustained-event dedupe)

### Test Fixtures and Pass Criteria

Minimum fixture set for first implementation:

- Positive: incoming visible, clean UI (at least 5 frames)
- Positive: incoming visible with mild blur/compression (at least 5 frames)
- Negative: no incoming text, cluttered combat UI (at least 10 frames)
- Negative: numerals and HUD noise in incoming crop (at least 10 frames)
- Boundary: synthetic or captured cases around 0.76 to 0.82 score

Pass criteria:

- Zero false positives in the negative fixture set
- At least 95 percent true positives in the positive fixture set
- Boundary behavior matches this ADR: confirm in near-threshold band, reject below
   lower bound unless OCR fallback returns positive

## Acceptance Criteria

Promote and keep template-primary behavior only when all criteria are met in live runs:

- No critical false positives that cause incorrect flare actions
- No regression in missed true INCOMING events versus current baseline scenarios
- Mean and p95 INCOMING detection processing time improved from OCR baseline
- Stable behavior across at least one full release cycle with fallback available

## Runtime Validation Update (2026-06-02)

Latest `wingman.log` telemetry confirms template-primary behavior in active runtime
with OCR fallback used only as a minority path.

### Observed Frequency (Latest Run)

- Total INCOMING evaluation cycles: 1925
- Positive detections (all sources): 41
- Positive via template: 36
- Positive via OCR fallback: 5

Derived rates:

- OCR fallback trigger rate vs all cycles: 5/1925 = 0.26%
- OCR fallback share of positive detections: 5/41 = 12.2%

### Observed Detection Cost When Fallback Triggers

From `incoming_processing_ms` on positive detections:

- Template positives: avg 1.5 ms (min 1, max 4, n=36)
- OCR fallback positives: avg 331.6 ms (min 262, max 370, n=5)

Estimated added latency when OCR fallback triggers:

- ~330.1 ms average versus template-positive path.

### Representative Log Excerpts

```text
2026-06-02 05:20:20,015 [INFO] Analyzer: incoming_template detector=incoming_template template_score=0.661 template_threshold=0.820 near_threshold_confirmation=False detection_source=ocr_fallback detected=True debounce_suppressed=False incoming_processing_ms=311
2026-06-02 05:20:23,083 [INFO] Analyzer: incoming_template detector=incoming_template template_score=0.819 template_threshold=0.820 near_threshold_confirmation=False detection_source=ocr_fallback detected=True debounce_suppressed=False incoming_processing_ms=370
2026-06-02 05:32:37,596 [INFO] Analyzer: incoming_template detector=incoming_template template_score=0.322 template_threshold=0.820 near_threshold_confirmation=False detection_source=ocr_fallback detected=True debounce_suppressed=False incoming_processing_ms=351
```

Interpretation:

- ADR 046 objective is being met operationally: template is the dominant detection
   source and preserves low-latency positives.
- Keeping fallback enabled remains useful for low-score true events, with a known
   latency trade-off on those cycles.

## Consequences

Positive:

- Reduced dependence on OCR for a narrow, visually stable UI signal
- Lower and more predictable INCOMING detection latency potential
- Better control over false positives via explicit score thresholds

Trade-offs:

- Template asset management and update overhead when UI visuals change
- Threshold calibration needed across screen scaling and capture quality variants
- Dual-path complexity while fallback remains available

## Alternatives Considered

1. Keep OCR-only and shrink the crop to only MING.
   - Rejected because OCR fixed overhead remains, and text-read stability still drives
     misses in noisy scenes.

2. Hybrid OCR plus template voting as permanent design.
   - Rejected for now due to complexity; fallback OCR remains available for rollback.

3. Full detector rewrite for all warning UI in one release.
   - Rejected due to higher rollout risk.

## References

- ADR 017 — OCR performance GPU vs template matching
- ADR 019 — INCOMING region subgrid OCR optimization
- ADR 029 — GAME_LOBBY quick-scan thread
- ADR 036 — GAME_LOBBY PLAY template matching pilot

### Template Asset Source

- c:/dev-tools/github/wingman/test_screenshots/INCOMING3.png