# Design 014 — Void-Region Boundary Detection

| Status | Date       | Wingman Version |
|--------|------------|-----------------|
| Draft  | 2026-09-21 | 1.8.11          |

## The problem, measured

`detect_map_boundary` (`wingman/analyzer.py`) finds the boundary by matching
a fixed HUD hue (8-28) inside the minimap disc, reconnecting fragments
(ADR 108), median-filtering the result (ADR 113), and accepting a shorter
fragment when the out-of-bounds void corroborates it (ADR 133). Every stage
past the first is compensating for the same root fact: **the thing being
matched is a thin, easily-confused colored line.**

The 2026-09-20/21 session (ADR 117 D8-D11) measured exactly how easily,
while hardening an unrelated diagnostic that reads the same detector: four
distinct real, live false-positive sources for anything thin and roughly the
right hue —

| Confound | What it actually was |
|---|---:|
| Raw pixel-count floor (D8) | Compass-rim decoration + rocky terrain color |
| Compass letters | Font strokes are thin by construction, same as a line |
| A circular UI marker's rim | A fragment of an icon's own ring — a real arc, wrong scale |
| An aircraft flight-path trail | Thin, elongated, curved — geometrically like a line |

Each fix (radial exclusion, elongation, arc-curvature residual, minimum
fit-radius) closed one leak and the next live session found another. That
pattern — one heuristic at a time, each patching a hue-matching problem
found the hard way — is the signal this document responds to, not any one
of the four confounds individually.

**A second signal already exists, already measured, and structurally cannot
share any of those four confounds.** ADR 133's `minimap_void_fraction`
already classifies the out-of-bounds region directly: dark and desaturated
(measured `V≈51, S≈0` on confirmed crossings), an **area**, not a line, so
fragmentation cannot break it the way it breaks hue-matching. ADR 133 also
already measured that this area grows monotonically with real proximity to
the edge — median void fraction **0.076 on blind frames, 0.117 on approach
frames, 0.272 on confirmed crossings** — exactly the geometric behavior a
`(dist, forward, lateral)` reading needs. Today this signal is used only as
a corroborating vote to relax the line detector's span requirement. It has
never been asked to produce a reading on its own.

**None of the four confounds above would fool it.** A compass letter, a
UI-marker rim, and a flight-path trail are all thin foreground strokes —
none of them is a large, dark, desaturated *region*, so none of them would
register as void regardless of their color. Terrain color contamination
(the fifth thing D9 had to guard against, in the same session) is precisely
what the void test already excludes by construction — bright terrain is
neither dark nor desaturated.

## Why not keep patching the line detector

Extending D8-D11's approach with a fifth, sixth, seventh shape heuristic was
considered and rejected as the next step, not as a bad idea in isolation —
each individual fix was well-measured and correct for the confound it
targeted. The pattern across four rounds in one session is what argues
against a fifth: every heuristic added to "is this thin thing a line"
narrows the false-positive rate without closing the underlying vulnerability
(matching *any* thin, roughly-hued, correctly-shaped foreground mark will
always have another instance nobody has hit live yet). A region-area signal
does not inherit that vulnerability class at all — it is a different kind of
answer, not a fifth patch on the same one.

## Why not replace the line detector outright, either

`detect_map_boundary`'s return value drives live steering (`BoundaryTurn`,
ADR 107/108/122/133/141) today. Swapping its primary signal without a
parallel-run comparison would be exactly the mistake ADR 133 itself avoided
— that ADR's own validation (`tests/test_corroborated_span.py`) is a
side-by-side detection-rate comparison against the strict, pre-existing
gate on the same real corpus, not a swap taken on faith. This document
proposes the same discipline: build the void-region reading alongside the
existing one, measure both on the same frames, and only promote it to the
one `detect_map_boundary` actually returns once it demonstrably matches or
beats the line detector on real, confirmed-crossing frames.

## Phase 1 — void-contour reading, validated in parallel

### Detection: the same nearest-point math, a different source mask

`detect_map_boundary`'s existing acceptance path already reduces to one
operation once a candidate line is chosen: find the candidate pixel nearest
the disc center, and report its distance and bearing as fractions of the
radius —

```python
dx, dy = xs - cx, ys - cy
dist = np.hypot(dx, dy)
i = int(np.argmin(dist))
return (dist[i] / radius, -dy[i] / radius, dx[i] / radius)  # dist, forward, lateral
```

The proposed `detect_map_boundary_void` (new method, additive — the existing
method is untouched) applies the identical formula to a different `(xs,
ys)`: the void mask's own pixels, computed the same way
`minimap_void_fraction` already does (`V < boundary_void_v_max (62)`, `S <
boundary_void_s_max (35)`, inside `boundary_void_radius_frac (0.78)` of the
disc to stay clear of the compass rim's own dark ring). No new HSV
calibration, no new mask-radius decision — both already exist and are
already validated on real data.

**Guard against small dark artifacts.** A UI marker's black icon background
(eyeballed from this session's crop of `blind_20260920_220226_2.png`'s crown
badge, roughly 16 px diameter, ~200 px² — not precisely measured, unlike the
figures below) would itself register as "dark, desaturated" and must not be
mistaken for the void. Unlike the line detector's confounds, this is
resolved by scale alone: `minimap_void_fraction`'s own median fractions
(0.076/0.117/0.272), applied to the actual disc its denominator uses
(`boundary_void_radius_frac (0.78) × radius`, ≈48,000 px² on this crop
size), put real void regions at roughly **3,600-13,100 px²** even at the
low (blind-frame) end — an order of magnitude larger than the icon
estimate, comfortably enough margin for a size floor without needing exact
icon dimensions. `cv2.connectedComponentsWithStats` on the void mask,
keeping only the largest component (or all components above a
`boundary_void_min_component_px` floor sitting in that gap), is the same
"connected-component + minimum size" primitive `detect_map_boundary`'s own
line path already uses — no new technique. The exact icon-scale ceiling
should still be measured, not assumed, before the floor is finalized — see
Testing plan.

### Actuation: none yet — Phase 1 changes no live behavior

`detect_map_boundary`'s return value, and therefore `BoundaryTurn`'s
steering, is **unchanged** in Phase 1. The new method is called
alongside the existing one, purely for comparison — the same shadow
posture this codebase uses for every detector change before trusting it
(ADR 070, ADR 073, HLDD 001 Phase 1, ADR 028 revision 4).

### Config

```yaml
minimap:
  # ADR 133's existing void thresholds are reused unchanged. New:
  boundary_void_min_component_px: 1000   # sits between icon-scale (~200 px,
                                          # estimated) and measured real-void
                                          # area (3600+ px, blind-frame floor)
```

### Testing plan

- **Measure the icon-scale ceiling directly**, before picking
  `boundary_void_min_component_px`'s final value: run the void mask (no size
  floor) over the archived corpus and record the connected-component sizes
  found on frames with UI markers but no real void, the same
  measure-before-threshold discipline this session used for every D9-D11
  constant. The ~200 px² figure above is a starting estimate, not this
  number.
- Unit: synthetic frames (following the `TestThinComponentShapeCheck`
  pattern in `tests/test_blind_capture_coverage.py`) — a large dark region
  produces a reading at the correct nearest-point bearing; a small dark
  icon-scale blob does not; no dark region produces `None`, matching the
  existing method's behavior on a clean frame.
- Comparative live validation (required before Phase 2's promotion
  question is even asked): run both methods over the same real corpus used
  to validate ADR 133 (`test_screenshots/unknown_anomalies/rtb_*.png`,
  confirmed crossings — line is definitely present) and the archived blind
  corpus (line is definitely absent or unconfirmed). Report, per frame:
  did each method agree on presence/absence, and where both fired, how far
  apart were their `(dist, forward, lateral)` readings? Same shape as
  `tests/test_corroborated_span.py`'s existing recall comparison, extended
  to a second detector rather than a threshold change.
- The known, already-documented line-detector failure
  (`test_the_boundary_is_found_at_the_centre_on_crossing_frames`'s "line
  crosses bright tan terrain, thickness gate rejects that stretch" case) is
  the single most useful comparison frame available — if the void method
  reads correctly where the line method is known to fail, that is the
  concrete case for promotion; if it also fails there, that is direct
  evidence against replacing rather than merely corroborating.

## Phase 2+ (not designed here, explicitly deferred)

- **Promotion decision.** Whether `detect_map_boundary` switches its
  primary source to the void method, keeps the line method as the primary
  with void as corroboration (today's arrangement, unchanged), or runs both
  and reports disagreement as a new diagnostic — decided from Phase 1's
  comparative data, not from this document.
- **Grid-texture corroboration.** The polar "spider web" grid overlay
  (confirmed this session: centered exactly on the aircraft/minimap center,
  and its lines are visible over in-bounds terrain but do not continue into
  the void) is a *third*, independent channel reporting the same in-
  bounds/void fact via texture rather than color. Deferred here so Phase 1
  validates one new signal at a time, matching how ADR 133 itself landed
  the void signal alone before D8-D11 built further on it. If Phase 1's
  void-color reading turns out to be unreliable on some map style (night
  maps, unusual terrain palettes), texture-presence is the named fallback
  to reach for next, not a redesign from scratch.
- **Minimap-center calibration via the same grid.** Because the grid's
  convergence point is provably the crop's assumed center by construction,
  fitting it independently would give a drift check on the `MINIMAP` crop
  coordinates themselves — a different problem (crop calibration) from
  boundary detection, noted here only because the same frame evidence
  motivated both ideas in the same conversation.

## Open Questions

1. **Void separation on maps not yet in the corpus.** ADR 133's `V≈51, S≈0`
   measurement and this document's area-scale numbers both come from the
   maps captured so far. Night maps, weather effects, or a future map pack
   could shift the void's rendered brightness/saturation — the comparative
   validation in Phase 1 is what would surface this, not an assumption
   either way.
2. **Shape of the void near a shallow crossing angle.** A boundary crossed
   at a shallow angle could produce a thin sliver of void near the disc
   edge rather than a large filled region — worth checking whether the
   component-size floor (`boundary_void_min_component_px`) accidentally
   excludes exactly the geometry that matters most (about to cross).
3. **Whether the two detectors' `forward`/`lateral` readings agree in sign
   and rough magnitude when both fire.** Not measured yet — Phase 1's
   comparative trial is designed to answer this directly.

## References

- ADR 108 — the line detector's reconnection/thickness pipeline this
  document does not modify
- ADR 113 — the median filter, applied identically regardless of which
  detector produces the raw reading
- ADR 133 — `minimap_void_fraction`, the void thresholds and area
  measurements this design reuses unchanged
- ADR 117 D8-D11 (2026-09-20/21) — the four confounds motivating this
  document, and the diagnostic they hardened (a different consumer of
  `detect_map_boundary`, unaffected by this change)
- Design 013 — the center-seeking steering design that would consume
  whichever detector Phase 2 promotes
- `wingman/analyzer.py` — `detect_map_boundary`, `minimap_void_fraction`
- `tests/test_corroborated_span.py` — the comparative-validation pattern
  this document's testing plan follows
