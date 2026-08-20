# ADR 080 — Health OCR Dropout Diagnosis Instrumentation

| Status   | Date       | Wingman Version |
|----------|------------|-----------------|
| Accepted | 2026-08-18 | 1.8.4           |

## Context

Health OCR routinely fails to produce a **confirmed** reading for long
stretches of live flight. The 2026-08-17 sessions put numbers on it:

- The ADR 079 telemetry-liveness gate suppressed **89 weak-mark attempts**
  in the 18:57 session (1 h 43 min) and 27 in the 15:33 session — each
  suppression is a confirmed-read gap that crossed the death-evidence
  threshold (`health.death_no_confirmed_s`, 8 s) *while telemetry proved
  the aircraft was flying*.
- Observed gaps ran 7–25 s (the 15:21 episode flew a full climb, 1871 →
  4677 m, with zero confirmed health the whole way).
- Before ADR 079, four of these gaps became phantom respawns in a single
  day, cancelling freshly restarted missions.

ADR 079 gated the worst consequence but left the cause unmeasured. The
dropout still costs on every axis that reads health:

- **Alive detection lags every respawn by ~5–8 s** (digits must appear,
  pass the SAF-004 2-of-3 confirm window, then the 1.5 s respawn-clear
  stability window) — idle mission time on all ~50 respawns per session.
- The Evade tactic's health threshold remains disabled pending calibration
  (ADR 024) — pointless to calibrate against a signal that vanishes for
  25 s stretches.
- ADR 061 death provenance and the eject-termination path read the same
  signal.

What is NOT yet known is **why** the reads fail: crop drift, HUD contrast
in specific attitudes (steep climbs recur in the episodes), digit bleed
from neighbouring HUD elements, or OCR preprocessing. Nobody has seen the
failing frames — the failure is only visible as an absence in the log.
This ADR is measurement-first (the shadow-first culture applied to a
perception problem): instrument, capture, classify — then fix in a
follow-up decision grounded in frames instead of conjecture.

## Decision

### d1 — Confirmed-read-gap histogram in session stats

`_record_confirmed_read` already computes the gap between consecutive
confirmed reads (analyzer.py). The analyzer accumulates these gaps into a
session histogram — bucketed like the performance tracker's timing
histograms (e.g. `<2s / 2–5s / 5–10s / 10–20s / >=20s`), counted **only
while the FSM is in GAME_BATTLE and telemetry is live** (`altitude_fresh`,
the ADR 079 discriminator) so respawn screens and menus never pollute the
distribution. The block rides `MissionStatsTracker.finalize(extra=...)`
into `run_*_stats.json` (the ADR 062 shadow-detector block precedent) and
prints one summary line per session (count, p95, max).

### d2 — Frame capture on dropout episodes

A dropout recorder mirroring the ADR 074 anomaly recorder (episode-scoped,
rate-limited, config-driven, never raises):

- Triggers when a confirmed-read gap crosses `capture_after_s` (default
  5 s) with telemetry live.
- Saves the **full frame** to `test_screenshots/health_dropouts/` named
  `dropout_<stamp>_gap<N>s.png` — the full frame, not just the HEALTH
  crop, so crop-drift hypotheses can be tested against the same image.
- Rate limits: at most one capture per episode plus one recapture per
  `recapture_interval_s` (default 60 s) for long episodes, capped at
  `max_per_session` (default 12).
- Config block `health.dropout_capture:` with `enabled`, `capture_after_s`,
  `recapture_interval_s`, `max_per_session`, `dir`.

### d3 — Offline classification, then a scoped fix

After one or two sessions, the captured frames are run through the
existing offline tooling (`debug_crops.py`, the real-OCR test harness) to
classify each failure: (a) crop misalignment, (b) HUD contrast/attitude
(steep climb), (c) digit bleed / fragment reads that never confirm,
(d) overlay obstruction. **The fix is chosen from the classification and
recorded before this ADR moves to Accepted** — a crop recalibration
(`make calibrate-crop CROP=HEALTH`), a preprocessing variant in
`_process_health_region`, or a confirm-window tuning, each a small
follow-up change. If the fix grows beyond that (e.g. a second redundant
health crop), it gets its own ADR referencing this one's data.

### d3 findings and the chosen fix (2026-08-18, first instrumented session)

The recorder hit its 12-frame session cap within ~10 minutes — dropouts are
chronic, not episodic. The frames settled the classification immediately:

- **Not crop drift, not HUD absence**: every captured frame shows the
  digits (250) centred in the crop, legible to a human.
- **The dropout background is sky**: pale-green digits over light blue —
  near-equal luminance. Nose-up flight puts sky behind the HUD, which is
  why the episodes correlate with climbs.
- The live log shows the actual failure is **fragment churn, not digit
  absence**: raw reads cycle through partials (`50`, `6` from 250),
  concatenations (`2601`, `4511` — multi-box digit merging), and
  near-misses, so the SAF-004 2-of-3 confirm window rarely finds
  agreement. Occasional clean reads land (~every 6–10 s), which is why
  the value eventually confirms.
- Per-variant measurement over the 12 dropout frames + 2 corpus frames:
  Otsu binary (the pipeline's **first** variant, whose result wins the
  early return) read correctly **1/9**, emitting the fragments; plain
  gray read 7/9; an **HSV hue mask for the HUD green read 9/9 exactly
  right**, including a fuel read the gray variant missed.

**Fix (implemented with this ADR):** `_process_health_region` gains a
label-scoped variant order. For the green-digit crops (`health`, `fuel`):
hue mask first, gray second, Otsu binary demoted to last resort — its
fragments no longer win the early return. The ammo counters render white
digits (the mask sees nothing there — measured) and keep the original
order untouched. Validated offline: 9/9 dropout frames and the full
`make ocr` corpus lane pass with the new order.

### d4 — Zero behavior change until d3

d1/d2 are pure observation: no flight-control path, no detection verdict,
no FSM input reads the new data. The only side effects are a stats block,
a log line, and PNG files.

## Consequences

- The dropout stops being invisible: every session quantifies it
  (histogram) and documents it (frames), so the eventual fix has a
  before/after measure built in.
- Disk: worst case ~12 full frames per session (~25 MB); the directory is
  gitignored like other capture output until frames are promoted into the
  calibration corpus.
- Once the dropout rate is driven down, the direct beneficiaries are
  alive-detection latency (mission restart), the evade health-threshold
  calibration (unblocked), and the ADR 079 gate (fewer suppressions to
  perform).
- The histogram also becomes the acceptance instrument for any future
  health-perception change — the same role the spawn-crash counter plays
  for ADR 076/078.

## Verification

- Unit tests: gap histogram buckets and battle+telemetry-live gating;
  recorder trigger threshold, rate limits, episode reset, and never-raises
  contract (the ADR 074 recorder's test pattern); stats block present in
  `finalize(extra=...)` output.
- `make test` green; replay gates unaffected (recorder inert without
  live telemetry in replay doubles).
- Live before/after (identical histogram definition both sessions):
  - **Before** (2026-08-18 02:39 session, pre-fix): 1076 counted gaps,
    **43 over 5 s**, p95 4.46 s, max 18.0 s; the recorder hit its 12-frame
    cap in ~10 minutes.
  - **After** (04:21 session, HSV fix live): 810 counted gaps, **0 over
    5 s**, p95 1.69 s, max 4.6 s; the 2–5 s bucket fell 108 → 14. The
    fragment class is gone from the live log entirely (residual gap cycles
    read `raw: []`, not garbage).
  - Second residual class identified and closed as game behavior: ~10 s
    all-variant-blank gaps correlate one-to-one with missile-firing cycles
    (six of six sampled episodes; gap length matches missile
    time-of-flight) — a game UI overlay covers the bottom-centre HUD
    during engagements. It blanks telemetry too, so the whole-gap
    liveness rule excludes it from the histogram in both sessions
    equally; the confirm window and ADR 079 gate ride through it. Not an
    OCR defect; no further action.
  - Same session: 10/10 missions, 26/26 guard telemetry-handoffs, zero
    weak-tier fires, zero spawn crashes, zero errors — and the first
    genuine >150 s entry exercised the ADR 077 stall path end-to-end
    (150 s timeout → 3 s stall → classifier → lobby bounce recovered).

## References

- ADR 079 — telemetry-liveness gate (the suppression counts that sized
  this problem; the live-flight discriminator reused by d1/d2)
- ADR 074 — GAME_UNKNOWN anomaly recorder (the capture pattern d2 mirrors)
- ADR 063/064 — health value confirmation (SAF-004) and the death-evidence
  windows the dropouts cross
- ADR 062 — stats `extra=` block precedent
- 2026-08-17 15:33 and 18:57 session logs — 27 and 89 gated dropout
  episodes respectively
