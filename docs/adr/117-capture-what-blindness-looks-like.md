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
