# ADR 108 — Boundary Detection for the New Minimap

| Status | Date       | Wingman Version |
|--------|------------|-----------------|
| Draft  | 2026-09-03 | 1.8.8           |

## Context

ADR 101 and ADR 107 both tuned what wingman does *when it sees the arena edge*.
Neither asked how often it sees one.

On the 2026-09-03 session the answer was **18.8%** — 3,292 ticks with a boundary
reading against 14,209 without. And the blindness lands where it matters: of the
eight confirmed crossings, **five had no boundary reading at all in the 30 s
before they happened**. Those five could not have been prevented by any tactic,
because no tactic had an input.

MetalStorm's 2026-09-02 minimap update is the cause. The old minimap drew dark
monochrome terrain, and `detect_map_boundary`'s docstring justified its mask as
map-independent on exactly that basis — "hue 16.9-18.5 while the map background
ranged V 66.6-118.4". The new minimap renders coloured terrain, tan land on blue
water, and draws the boundary as a thinner, antialiased line.

The colour gate was not the problem. Across nine captured crossing frames the
mask still found 550 to 1,400 boundary pixels. It arrived as **20 to 174
disconnected fragments**, and the `min_span_frac` filter — which exists to reject
terrain speckle — rejected every one of them.

## Decision

**D1. Lower the saturation floor to 60.** The line is thin and antialiased, so
its edge pixels sit well below the old floor of 120. Value stays at 120.

**D2. Reconnect the fragments with ONE morphological closing pass** (5x5
ellipse) before component analysis.

One pass, not two, and the difference is not cosmetic. Two passes bridge a 12 px
speckle grid into a single lattice whose thinness reads 0.019 against the real
line's 0.018 — indistinguishable, which is the 2026-08-30 terrain false positive
reintroduced. One pass reconnects all nine live crossing frames and bridges no
speckle at all. The existing synthetic speckle test is what caught this.

**D3. Choose the most line-LIKE component, not the largest**, by LOCAL
thickness — the largest distance-to-background inside the component. The
boundary is a stroke of fixed width, so this stays about 1.4 px however long or
curved it runs; a landmass is thick in the middle whatever its outline does.

Two weaker measures were tried first and **both failed on real data**, which is
worth recording because both looked convincing on the corpus available at the
time:

- **Bounding-box fill** rejects a straight line, which fills its own thin box
  completely. The live corpus is entirely curved arcs and would never have shown
  it; the synthetic straight-line tests failed immediately.
- **Aggregate thinness (`area / span^2`)** passes a large irregular blob,
  because a ragged outline inflates the span. Shipped, and the first live
  session produced **32 turns in 12 minutes** — against 65 in seven hours
  before — including one at round start with the aircraft nowhere near an edge.
  A capture from that session shows the mask covering an entire desert landmass,
  reading 0.334 against a 0.5 gate.

Local thickness separates them with a 3.7x margin: measured on the live corpus
the real line runs **1.4-9.2 px** and terrain runs **34.5-50.2 px**. The gate is
expressed as a fraction of the minimap radius so it does not depend on capture
resolution.

Selecting by shape rather than size also stops a landmass being read as an edge.
On the two non-crossing frames the old rule reported 0.78-0.80R from island
terrain — a confident reading of nothing.

**D4. Capture the APPROACH, not only the crossing.** *(Cap corrected
2026-09-04 — see below.)* Every frame in the corpus
was taken at the moment of a crossing, so all of them show the line at the
centre. That says nothing about whether the line is tracked at 0.3-0.5R, which
is the range at which a turn would have to act — and that is the range ADR 107
needs evidence for. One frame per approach, sharing the ADR 106 cap because
approaches outnumber crossings roughly eight to one and would otherwise crowd out
the rarer frames.

**D4a. Separate capture budgets, not a shared cap.** D4 argued for sharing the
ADR 106 cap "because approaches outnumber crossings roughly eight to one and
would otherwise crowd out the rarer frames" — and then implemented a single FIFO
counter, which hands priority to whichever event arrives first. That is the
frequent one, so it produced precisely the crowding-out the sentence rejected.

Measured 2026-09-04, a 4h26m session with six confirmed crossings: **18 approach
frames saved, 0 crossing frames, 125 suppressed.** Not one frame of the event the
corpus exists to study.

Crossings now hold a budget approaches cannot spend.

## Consequences

On the nine crossing frames the detector reports **9 of 9**, and **8 of 9 at
0.03-0.14R** — at the centre, as it should be with the banner up — against a
session-wide readability of 18.8% before. The two terrain frames return nothing
rather than a confident 0.78R.

An earlier draft of this ADR claimed 9 of 9 *correct*. It was 8 correct and one
false positive: `crossing5` was reading a terrain blob 44 px thick, which
happened to sit near the centre and so looked like a plausible crossing. The
local-thickness gate rejects it, which is the right outcome and also exposes a
limitation — see below.

Detection improving does not make the turn work. ADR 107's V9 failure stands:
where a turn *did* run, the range still closed. What changes is that the five
crossings with no reading are now a perception problem with a fix rather than an
unexplained gap, and the two failures can finally be told apart.

**Known limitation, one frame of nine.** On `crossing5` the line crosses bright
tan terrain and merges with it locally. The thickness gate rejects that stretch,
so only a distant fragment of the same arc survives and the range is measured to
the wrong part of the line: 0.44R with the aircraft sitting on the boundary. The
test asserts a majority rather than hiding this, because tightening it to 9 of 9
would mean loosening the gate that keeps desert terrain out — the worse trade.

The gate is tuned on live frames from two sessions and a handful of maps. The
previous version of this paragraph said such a threshold "looks settled until a
map with long thin terrain features arrives"; that map arrived on the very next
session. Treat the number as provisional and re-measure when a new map appears.

## Validation

- **V1.** Every archived crossing frame produces a reading, and every reading is
  under 0.30R.
- **V2.** The two non-crossing frames produce no reading — island terrain is not
  an edge.
- **V3.** A synthetic straight line is still detected; the thinness gate must
  not reject the shape it exists to keep.
- **V4.** A synthetic speckle field still produces nothing.
- **V5.** A short streak is still rejected by span.
- **V6 — live.** Readability rises well above 18.8%, and crossings with no
  reading in the preceding 30 s become rare. Not yet observed.
- **V7 — live.** Approach frames accumulate at 0.3-0.5R, so ADR 107's V9 can be
  judged on the range where a turn would act. Not yet observed.

## References

- ADR 106 — the crossings series, and the capture that produced this corpus
- ADR 107 — the tactic this feeds; its V9 failure is not addressed here
- Design 010 — the original instrumentation and its dark-background premise
- `test_screenshots/unknown_anomalies/rtb_*.png` — the nine crossing frames
- `test_screenshots/AMMO_MISSILE*.png` — the post-update minimap, no boundary
