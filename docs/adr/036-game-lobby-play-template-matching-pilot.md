# ADR 036 - GAME_LOBBY PLAY Template Matching Pilot

| Status   | Date       | Wingman Version |
|----------|------------|-----------------|
| Accepted | 2026-05-23 | 1.6.9           |

## Context

GAME_LOBBY scanning currently relies on OCR-first logic. We want to introduce template
matching in a low-risk way that validates architecture, thresholds, telemetry, and
fallback behavior before widening coverage to other lobby signals.

The PLAY button is the best first target because it is:

- Frequent and easy to observe during normal runs
- Operationally important to mission flow
- Visually stable in typical lobby states
- Easy to validate with clear success/failure outcomes

This ADR aligns with existing lobby orchestration and FSM ownership decisions and does
not replace the state machine or controller click flow.

## Decision

Adopt a phased template-matching pilot for GAME_LOBBY PLAY detection only.

Key decision points:

- Keep FSM transitions and click flow unchanged; only the PLAY detection source changes
- Introduce template matching behind config flags
- Keep OCR as fallback during pilot and initial rollout
- Run shadow-mode comparison before allowing template-driven clicks
- Promote to template-primary only after defined quality thresholds are met

## Scope

In scope:

- PLAY button detection in GAME_LOBBY
- Template score logging and confidence thresholds
- OCR fallback behavior when template confidence is insufficient
- Regression tests for positive, negative, and threshold-boundary cases

Out of scope for this ADR:

- READY and popup template migration
- Full OCR removal from lobby flows
- Changes to FSM transition definitions or ownership

## Implementation Approach

1. Add config gates:
   - lobby_template_matching_enabled
   - lobby_play_template_enabled
   - lobby_template_fallback_to_ocr
   - lobby_play_template_threshold

2. Implement a lobby template matcher path restricted to PLAY crop/region.

3. Start in shadow mode:
   - Evaluate template score
   - Log template-vs-OCR agreement
   - Do not alter click behavior yet

4. Enable template-driven PLAY click only after pilot criteria are met.

5. Keep OCR fallback enabled for at least one release cycle after promotion.

## Pilot Acceptance Criteria

Promote from shadow mode to active mode only when all conditions are met across live sessions:

- Template/OCR decision agreement >= 98%
- No critical false positives causing wrong clicks
- No observed increase in mission-start misses
- No measurable regression in lobby reaction latency

If criteria are not met, keep OCR-primary and iterate on template assets/thresholds.

## Consequences

Positive:

- Lower OCR dependence for a high-frequency lobby action
- Faster detection path potential in stable UI regions
- Reusable template architecture for READY and popup regions

Trade-offs:

- Additional template asset maintenance
- Threshold tuning required across UI variants
- Dual-path complexity during rollout (template + OCR fallback)

## Alternatives Considered

1. Migrate all GAME_LOBBY regions to template matching in one step.
   - Rejected due to high regression risk and reduced debuggability.

2. Keep OCR-only lobby scanning.
   - Rejected because it delays architectural validation for Phase 3-era behavior work.

## References

- ADR 017 - OCR performance GPU vs template matching
- ADR 026 - GAME_LOBBY state-machine sequence
- ADR 029 - GAME_LOBBY quick-scan thread
