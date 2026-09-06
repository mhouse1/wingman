# ADR 117 — Capture What Blindness Looks Like

| Status | Date       | Wingman Version |
|--------|------------|-----------------|
| Draft  | 2026-09-04 | 1.8.8           |

## Context

ADR 113 filtered the boundary reading's noise. Measuring the session that ran
on it changes the priority entirely:

| | |
|---|---:|
| behaviour-tree ticks | 2204 |
| ticks with a boundary reading | 514 |
| **readability** | **23%** |
| ticks with no reading | 1464 |
| suppressed (respawn settle) | 83 |

**The detector is blind on 77% of ticks.** The crossing at 22:39:10 had its last
reading at 22:38:56 — fourteen seconds earlier. BoundaryTurn did not fail to act
on that approach; it was never told about it.

That reorders the work. ADR 107 tuned the tactic, ADR 108 improved the detector,
ADR 113 filtered its noise — all operating on the 23% of ticks where a reading
exists. None of them can reach a crossing that happens in the other 77%.

**And the failure case has never been looked at.** Approach frames are written
when a reading fires, crossing frames when a crossing confirms. Every frame in
`unknown_anomalies/` is one where detection *succeeded*. There is no evidence at
all of what the minimap looks like when the detector returns None — whether the
boundary is off-crop, off-colour on a particular map, occluded by HUD elements,
or genuinely absent because the aircraft is nowhere near an edge.

That last possibility matters: **77% blind may be correct.** An aircraft in the
middle of the arena has no boundary within the minimap disc, and returning None
is the right answer. Without frames, "blind" and "wrong" are indistinguishable,
and tuning the detector against an unknown mix would be guesswork.

## Decision

**D1. Capture frames when the detector returns None.** The whole frame, matching
the crossing captures, so the map is identifiable.

**D2. A separate budget (`blind_capture_max`, 40).** Not shared with rtb or
approach. Blindness is the common case, so a shared counter would hand it
priority over the rare and valuable frames — the mistake ADR 108 D4 made in the
other direction, with 18 approach frames and 0 crossing frames in a session with
six crossings.

**D3. Rate-limited (`blind_capture_interval_s`, 45 s).** 1464 blind ticks in one
session would be 1464 frames. The question is what blindness looks like across
maps and situations, and a sample every 45 s answers that; a frame per tick
answers it 30 times over at 30 times the disk.

**D4. Not during a respawn.** The respawn overlay is already known to produce
garbage (ADR 107's settle), so those frames would be evidence of a thing already
understood.

**D5. A cap of 0 disables it.** This is a diagnostic to answer one question, not
a permanent cost. When the question is answered, the cap goes to 0 or the code
goes away.

## Consequences

The next session yields up to 40 frames of the failure case, which is the input
needed to decide whether 77% blindness is a detector defect or the correct
answer for an aircraft in open airspace.

Until those frames are read, **no further tuning of the detector or the tactic
is justified**. ADR 107's V10, ADR 108's V6 and ADR 113's V6 all target
crossings per mission, and none of them can move a rate dominated by ticks with
no input.

This adds a `cv2.imwrite` on at most one tick every 45 s, off the OCR path. The
capture helper already swallows every exception because it runs on the tick
path.

## First frames read (2026-09-04) — the 23% figure was wrong

Twenty-three frames, read the same evening. **Twenty-two of them have no
minimap on screen at all**: loading screens, the lobby, the post-match panel,
the event banner. Returning None there is correct — there is nothing to read.

The 23% readability that motivated this ADR was measured across every
behaviour-tree tick in the session, including every tick spent outside a
battle. Restricted to `GAME_BATTLE`:

| | ticks | readings | readability |
|---|---:|---:|---:|
| in GAME_BATTLE | 682 | 385 | **56%** |
| outside battle | 274 | 66 | 24% |

**56%, not 23%.** The detector is roughly twice as good as the number that
prompted this ADR, and the deficit that remains is a far smaller target. The
conclusion that ADR 106's rows are input-limited still holds — 44% is a lot of
missing ticks — but the scale of it was overstated, and by me.

Two corrections follow, both applied:

- **Capture only in `GAME_BATTLE`.** Eleven of the first twelve frames were
  useless for the question asked.
- **Capture only on a RAW None.** The first version tested the reading *after*
  the respawn settle and the median filter, so a frame whose boundary was
  detected and then deliberately suppressed was written as "blindness". Frame 3
  is exactly that case: it reproduces offline as `(0.136, -0.095)` — a clean
  reading of a clearly visible arc — while the live tick logged no reading.

**One discrepancy remains open.** That frame reads offline with production's own
crop (verified identical, 0.8319/0.0044/0.9986/0.2689) and the real
`detect_map_boundary`, yet the live tick did not. Suppression is the likely
explanation and is now excluded by construction, but it has not been proved: the
log shows `no reading` rather than `reading suppressed` on that tick, and there
are two `detect_map_boundary` call sites. Until it is explained, **offline
replay of a saved frame is not proof of what the live tick saw.**

## In-battle blindness, read (2026-09-05, preliminary)

The corrected capture — in `GAME_BATTLE`, on a raw `None` — produced its first
frames. Both show the aircraft mid-arena over open water with **no boundary
anywhere in the minimap disc**. The only orange present is the compass rim,
which the 0.93R radial mask already excludes.

So `None` was the correct answer on both. Taken with the earlier corpus (22 of
23 frames were lobby, loading or post-match screens), the emerging picture is
that **blindness is mostly the right answer rather than a detector defect**, and
that 56% in-battle readability reflects how often the aircraft is near enough to
an edge for one to be in view.

**Two frames is not a conclusion.** It is consistent with the hypothesis and
nothing more; the capture continues at one frame per 45 s of in-battle
blindness. What would overturn it is a frame with a visible arc and no reading,
and none has appeared yet under the corrected rules.

If this holds, the consequence matters for ADR 106: the readability figure is
not a deficit to close, and effort belongs in the tactic rather than the
detector — which is where ADR 122 put it.

## Validation

- **V1.** The blind kind has its own budget, independent of rtb and approach.
- **V2.** A cap of 0 disables capture entirely.
- **V3.** Capture stops at the cap.
- **V4.** A bad or missing frame never raises.
- **V5.** Capture is restricted to `GAME_BATTLE` and to a raw None.
- **V6 — the question. ANSWERED IN PART.** Most captured blindness was the
  correct answer to a screen with no minimap. Battle-only readability is 56%.
  What the remaining 44% consists of needs the corrected corpus.

## References

- ADR 106 — the crossing rate, which this says cannot move until blindness is
  understood
- ADR 107 — BoundaryTurn, acting on 23% of ticks
- ADR 108 — the detector, and D4's shared-budget mistake that D2 avoids
- ADR 113 — the noise filter, which operates only where a reading exists
- `wingman/tick_handlers.py` — `_capture_boundary_frame`
- `tests/test_tick_handlers.py` — `TestBlindFrameCapture`, V1-V4

## What the frames actually show (2026-09-06)

Forty blind frames were captured in the 2026-09-05 (night) session. Read with the
real `detect_map_boundary` — the analyzer constructed, not re-implemented — they
answer D1's question and change the diagnosis.

### The sample is narrower than D2/D3 intended

All 40 frames are timestamped 22:00-22:51. The budget of 40 at one frame per 45 s
is **30 minutes of coverage in a 4h 36m session**, after which 158 further blind
ticks hit the cap. D3 set the interval to answer "what blindness looks like across
maps and situations"; the budget spends itself on the first map and cannot.

### Nearly a third of blind frames have no minimap at all

Eleven of 40 (27.5%) contain **five or fewer** boundary-coloured pixels in the
crop, and the minimap is simply not rendered — killcam, transition or cinematic.
`None` is right, and the capture told us nothing. D4 excludes respawn; it does not
exclude these.

### On the 29 frames that do have a minimap, one gate rejects everything

| | blind (sampled, n=24 of 29) | approach, detector fired (n=12) |
|---|---:|---:|
| longest thin component | **20-76 px** | **88-264 px** |
| components rejected too SHORT | 3660 | 819 |
| components rejected too THICK | **0** | 6 |
| components passing | **0** | 33 |

The span gate is 79.2 px. **Every** blind rejection is a span rejection; the
thickness test never fires. The blind frames sit just under the gate and the
successful ones just over it.

### RETRACTED — the hue window is correct

An earlier revision of this section claimed the detector's hue window had come to
match the new terrain rather than the boundary. **That was wrong, and the corpus
says so.** It rested on one blind frame (`223440`) containing a visible red line at
hue 0-3, which was assumed to be the arena boundary. It is not; the arena boundary
is the terrain-to-void interface, and the red stroke is a different overlay.

The audit that settles it locates the boundary **without assuming any colour**:
find the out-of-bounds void by brightness alone, take a band on the terrain side of
that interface, and subtract the hue histogram of deep terrain far from the edge. A
hue enriched at the interface but absent inside it is the drawn line. Rim arcs are
excluded by clipping to 0.78 of the minimap radius — without that the band picks up
the blue rim and reports hue 112, which is what a first pass did report.

Over all 58 confirmed-crossing frames, 62,169 sampled pixels:

| Corpus | Top enriched hues | Sum over window 8-28 |
|---|---|---:|
| Pre-update (Jul/Aug calibration frames) | 3, 112, 1, 2 | **-0.055** |
| Post-update, 2026-09-03 (n=9) | 18, 17, 16, 12 | +0.101 |
| Post-update, 2026-09-04 (n=22) | 16, 3, 17, 12 | +0.074 |
| Post-update, 2026-09-05 (n=23) | 17, 18, 16, 15 | +0.226 |
| Post-update, 2026-09-06 (n=4) | 12, 17, 11, 1 | +0.175 |
| **Post-update, all (n=58)** | **17, 16, 18, 12** | **+0.145** |

The line sits at hue 16-18 on every post-update date — the centre of the 8-28
window, and a match for the 16.9-18.5 this ADR's parent measured on the Design 010
frames. **The colour did not move and the window does not need changing.**

### Then blindness is not correct, and that is worse

The same void measure answers the question this ADR was written to ask, and the
answer is the opposite of its hypothesis. Of the 29 blind frames that have a
minimap at all:

| Boundary in view (void fraction) | Frames |
|---|---:|
| > 0.02 | **26 of 29 (90%)** |
| > 0.05 | 22 of 29 (76%) |
| > 0.10 | 16 of 29 (55%) |

Median void fraction on blind frames is 0.108, against 0.297 on confirmed
crossings. This ADR's context allowed that "77% blind may be correct" — an
aircraft mid-arena has no boundary to see. On this sample it is **not** correct:
on 90% of blind frames the boundary is in view and the detector returned None.

Combined with the gate table above, the failure is now specific and single: the
colour is right, the line is present, and it arrives in fragments none of which
reaches the 79.2 px span gate. ADR 108 identified exactly this and added
morphological closing; these frames show the closing is not sufficient. The span
gate requires the line to be ONE connected component, and the post-update minimap
does not reliably draw it as one.

### The measure that located the boundary is itself the candidate detector

Out of bounds renders as a large dark, desaturated region (V approx 51, S approx 0
— not black; an earlier `V<45` test missed it entirely). Its share of the minimap
disc orders exactly as the geometry requires:

| Frame class | median void fraction |
|---|---:|
| blind (no reading) | 0.076 |
| approach (reading fired) | 0.117 |
| confirmed crossing | **0.272** |

Unlike hue, this does not depend on which map is loaded. It is a correlate on a
biased sample, not a detector, and it is recorded as the most promising lead
rather than as a decision.

### What this means for ADR 106

The series in ADR 106 is flat across 472 post-update missions. These frames offer a
mechanism, though not the one first proposed here: the boundary is in view on 90%
of the ticks the detector calls blind, and is discarded by a connectivity
requirement rather than missed by colour. Nine sessions of tactic tuning were spent
on a tactic that was told about the edge on a minority of the ticks where the edge
was actually there.

The lead worth pursuing is that **the void interface needs no connectivity at all**
— it is an area measure, so a line broken into 200 fragments and a line drawn
solid give the same answer. That is the property the current detector lacks, and it
is measurable on frames already archived, with no live session required.

## D2/D3 revised (2026-09-06) — and one test now fails on purpose

**D2 revised. `blind_capture_max` 40 to 120; D3 revised, `blind_capture_interval_s`
45 s to 300 s.** The original pair is thirty minutes of coverage, and D3's stated
aim — blindness "across maps and situations" — cannot be met from one map. The cap
is now a disk guard rather than the thing that ends sampling; 120 at 300 s spans a
10 h session. `tests/test_blind_capture_coverage.py` asserts the arithmetic, so an
edit that shrinks coverage fails in CI rather than in a session nobody re-reads.

**D7 (new). Capture only frames that actually have a minimap.** GAME_BATTLE is not
sufficient: killcam, transition and cinematic frames are in battle with no HUD, and
11 of the 40 frames (27.5%) were exactly that. `GameStateAnalyzer.minimap_present`
tests the boundary-hue mask inside the disc, which separated with a very large
margin on the corpus — no-minimap frames held 0-5 matching pixels, real minimaps
558 or more, and the threshold is 50. It fails OPEN: a frame that cannot be
classified is still captured, because failing closed would silently switch off the
diagnostic. Deliberately NOT a test for the boundary — a minimap showing no
boundary is the evidence this capture exists to collect.

A skipped frame does **not** advance the interval timer, so a killcam frame cannot
spend the five minutes the next real minimap needs.

### A pre-existing test is now failing, and it should stay failing

`test_the_boundary_is_found_at_the_centre_on_crossing_frames` asserts that at least
90% of confirmed-crossing frames yield a reading. Every frame in that corpus was
captured with RETURN TO BATTLE on screen, so the line is unambiguously at the
aircraft. It now reads 55 of 63 (87.3%) and fails. Verified pre-existing: it fails
identically with every code change of 2026-09-06 stashed.

By capture date:

| Date | Frames | No reading | Miss rate |
|------|-------:|-----------:|----------:|
| 2026-09-03 | 9 | 0 | **0%** |
| 2026-09-04 | 22 | 2 | 9% |
| 2026-09-05 | 23 | 5 | **22%** |
| 2026-09-06 | 9 | 1 | 11% |

Per-date samples are small and no single row means much. The aggregate does: on
frames where the boundary is certainly present, the detector now misses **one in
eight**. That is the same defect the blind frames show, measured on independently
labelled positives.

**The assertion is not being relaxed.** It is a rate rather than a count precisely
so a growing corpus can reveal drift, and it has. Weakening it to green would
discard the clearest evidence in the repository that detection is getting worse.
It stays red until the detector is fixed.

