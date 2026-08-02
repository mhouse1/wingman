# ADR 063 — Health OCR Value Confirmation Filter

| Status   | Date       | Wingman Version |
|----------|------------|-----------------|
| Accepted | 2026-08-01 | 1.6.29          |

Prerequisite for [ADR 062](062-health-signal-respawn-detection-retiring-respawn-ocr.md)
(Draft) advancing past Phase A: the health signal cannot become the primary
respawn detector while raw health OCR can degrade the way the 2026-08-01
17:34 session shows. Also fixes present-day noise independent of ADR 062.
Layers *under* [ADR 061](061-eject-termination-via-observed-death-health-signal.md)
(Accepted) — 061's death-confirmation semantics are unchanged and now receive
filtered values.

## Context

The 2026-08-01 17:34 live session exposed a health-OCR failure regime no
existing defense handles. Raw accepted values, in order, from a period where
true health was ~250-264:

```
250, 264, 250, 264, 26, 250, 350, 20, 250, 0, 260, 64, 250, 64,
260, 60, 250, 64, 6, 250, 64, 254, 6, 250, 254, 64, 250, 25, ...
```

Roughly half the reads are garbage: digit fragments of the true value
(`64`, `26`, `6`, `25`, `60`, `20` — subsets of "260"/"264"), concatenations
(`350`, `2112`, `9250`), and one false `0`. Session impact:

- **14 spike rejections** (vs 2-4 in every other session) — and the ceiling
  filter was *defeated*: each garbage-driven alive transition clears the
  ceiling (by design, for respawns), so the next monster read re-seeds it —
  `9250` was accepted as health moments after `8450` was rejected.
- **~20 spurious dead→alive transitions**, each consuming main-loop
  disposition work and re-clearing the spike ceiling.
- **The ADR 062 shadow detector went blind**: 0 of 3 OCR-detected respawns
  matched, because garbage digits kept arriving during overlays (the
  no-digits weak tier never accumulated) and no death was ever confirmable.

The existing ceiling filter only rejects values *above* `ceiling × 1.5`;
low fragments (`6`-`64`) pass freely, and the ceiling itself is rebuilt from
whatever follows a transition. ADR 061's two-read death confirmation guards
the death evidence but not the value stream feeding everything else.

## Decision

Insert a **value confirmation layer** between raw health OCR output and every
consumer (ceiling filter, alive/dead flag, ADR 061 death evidence, ADR 062
shadow detector), with three rules:

**1. Plausibility bound (pre-filter).** Raw reads above
`health.max_plausible` (default **500**) are discarded before any windowing —
concatenation garbage (`2112`, `9250`) never enters the pipeline, and can no
longer poison a freshly cleared ceiling.

**2. Recurrence confirmation.** A read becomes the accepted health value only
when at least 2 of the last `health.value_confirm_window` (default **3**) raw
reads agree within `health.value_confirm_tolerance` (default **15**,
absolute). The confirmed value is the most recent of the agreeing reads.
Unconfirmed reads are held: the previous confirmed value stands, and a debug
line records the rejection.

Why recurrence, not consecutive-agreement or a plain median: the observed
garbage is high-density (~50%) but *non-repeating* (fragments vary:
`64, 26, 6, 25...`), while the true value recurs constantly. Against the
logged sequence above, recurrence confirms the 250/254/260/264 track and
rejects every fragment; a median-of-3 fails at 50% contamination, and
consecutive-agreement deadlocks when garbage alternates with truth.

**3. Digits-present is not digits-confirmed.** An unconfirmed read still
counts as "digits present" — it resets the no-digits clocks (both the shared
window and the ADR 062 shadow clock). The weak death tier claims *absence*
of digits; garbage digits are present digits. This is honest about the
17:34 regime: when OCR hallucinates digits on the respawn overlay, the
health signal simply cannot see that respawn — which is exactly the
measurement ADR 062 Phase A needs to capture.

### Interaction with ADR 061 (unchanged semantics, new input)

ADR 061's two-read death confirmation now consumes *confirmed* values. A
death therefore requires the `0` to recur within the confirmation window
(rejecting the 17:34 session's lone false `0` at the value layer) **and**
then confirm at the evidence layer. Worst-case observed-death latency grows
to ~3 OCR rounds (~4.5 s) — still inside the ~8 s respawn overlay. The
respawn-restart alive transition similarly gains up to one round (~1.5 s)
of latency; ADR 059's "restart when health returns" tolerance comfortably
absorbs this.

### What is deliberately not done

- **No monotonicity assumption.** Health plausibly only decreases within a
  life, which would license rejecting all upward jumps — but repair/regen
  mechanics can't be ruled out, and recurrence handles the observed regime
  without that bet.
- **The ceiling filter stays** as a second layer: recurrence cannot reject
  the same high misread appearing twice within the window; the ceiling can.
- **No OCR preprocessing changes** (thresholds, scaling): the crop pipeline
  is shared with other regions and tuned by ADR-tracked calibration; this
  ADR contains the damage at the value layer instead. If a future session
  shows the *cause* of the degraded reads (aircraft/map/HUD variant), fixing
  the crop is a separate, complementary decision.

## Configuration

```yaml
health:
  death_no_digits_s: 6.0        # existing (ADR 062)
  max_plausible: 500            # reads above this are discarded outright
  value_confirm_window: 3       # raw reads considered for recurrence
  value_confirm_tolerance: 15   # absolute agreement tolerance
```

## Consequences

- The 17:34 garbage regime collapses to a stable 250/254/260/264 track:
  no fragment-driven transitions, no ceiling churn, no false death evidence.
  Clean-regime sessions see one extra OCR round of latency on value changes
  and on the post-respawn restart; nothing else changes.
- Session start (and the read after any confirmation-window flush) needs two
  agreeing reads before the first health value exists — ~1.5 s of "unknown
  health" at battle entry, already the pre-battle norm.
- ADR 062 Phase A resumes on filtered input; the 2 sessions scored so far
  (2/6 matched) predate this filter and are excluded from the exit-criteria
  count, which restarts at zero.
- If a garbage regime ever exceeds the filter (repeating misreads within
  tolerance), the ceiling filter and ADR 061 confirmation remain as the
  next layers; the failure mode degrades to "health frozen at last confirmed
  value", which consumers already tolerate (it is the no-reading behavior).

## Validation

- Unit tests replay the **actual logged 17:34 sequence** (the 28-read
  excerpt above plus the rejected-spike values) through
  `_process_health_reading` and assert: no spurious alive transition, no
  death evidence, no shadow mark, stable confirmed value.
- Clean-regime tests: value tracks damage ramps within one round; confirmed
  `0, 0` still produces observed death; respawn recovery (`0,0 → None → 250,250`)
  still latches `alive_after_observed_death` and fires the shadow strong tier.
- Boundary tests: `max_plausible` discard, window flush on
  `reset_health_for_respawn` and lobby entry, tolerance edges.
- `make test` and `make tp` green; Accepted only after one live session in
  each regime (a clean one, and ideally a recurrence of whatever produced
  17:34) shows a stable health track in the log.

**Accepted 2026-08-01**: the garbage regime recurred in the 18:40 session
(61 unconfirmed holds, 9 max_plausible discards) with zero spike rejections,
zero spurious transitions, and zero evidence corruption; the 19:03 session
provided the clean-regime pass (8 holds, 1 legitimate transition). Gates
green throughout.
