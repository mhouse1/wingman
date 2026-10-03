# Anomaly 011 — Resupply Focus Steers at Terrain and Exhaust, Not the Beacon

| Status | Date       | Wingman Version |
|--------|------------|-----------------|
| Draft  | 2026-10-02 | 1.9.0           |

## Summary

**Status: cause measured 2026-10-02; detector replaced the same day, live
validation pending ([ADR 152](../adr/152-pursuit-resupply-priority.md) V5f).**
With resupply actuation on, the aircraft did not fly to the resupply beacon
(operator report). In one 46-minute session the resupply focus took the
steering 13 times and pointed it at yellow terrain, the aircraft's own exhaust,
explosions and flares. Eleven of the 13 focus episodes ended in a respawn.

## Evidence

One session on 2026-10-02 (46 minutes), code at `d43dba8`.

**What the detector pointed at (measured).** The pursuit saves the first two
marker detections of each pursuit. All 41 saved frames were cropped at the
reported position and read by eye:

| At the reported position            | Frames |
|-------------------------------------|--------|
| Yellow-ochre terrain or grass       | 23     |
| Own engine, afterburner, explosion  | 13     |
| Flares                              | 1      |
| Minimap rim                         | 1      |
| The resupply icon                   | 3      |

One of the three was a piece of the icon's ring, 76 px from the icon
(reported `(1024,486)`, disc at `(959,526)`).

**The target moved between scans (measured).** Of 215 pairs of consecutive
fresh detections less than a second apart, the median distance was 345 px and
139 were more than 200 px apart. Over one 3 s stretch the marker read
`(884,486)`, `(1373,927)`, `(723,574)`, `(1145,190)`, `(1002,834)`,
`(1033,241)`, and `HOLD[roll]` and `HOLD[pitch]` reversed every 150 to 300 ms.

**How the focus episodes ended (measured).** 13 focus starts: 11
`respawn_detected`, 1 `match_ended`, 1 `RESUPPLY: confirmed ammo=2` (23 s
after `missiles exhausted`, with terrain as both saved candidates, so the
timer rearm of [ADR 088](../adr/088-eject-rearm-abort-and-incoming-priority-afterburner.md)
explains it as well).

## Cause

The detector matched a shape that many things have, in a color band that many
things have (measured, by running `find_resupply_marker` on the saved frames).

1. **Color.** The band was hue 18 to 42, saturation and value from 100. The
   icon is one color: hue 27, saturation above 225 in clear air and about 160
   through haze, value above 200. The terrain it matched was hue 18 with
   saturation 100 to 150; grass was value 100 to 130; flame was hue 18 to 22.
2. **Shape.** After a morphological close it accepted any component 58 to
   154 px wide that filled 12% to 42% of its box. That was meant for the dashed
   ring. The ring is not a fixed size: it measured about 70 px across in the
   operator captures and 150 to 190 px in a frame of this session, so its arcs separate into
   pieces and the largest piece wins.
3. **Range.** The icon's disc measured 21 to 58 px. A 58 px floor on the ring
   meant a distant beacon was never accepted, while real icons 24 to 57 px
   across were in view in 29 of 187 other pursuit frames of the same session.

## Fix

`find_resupply_marker` now finds the icon's disc
([ADR 152](../adr/152-pursuit-resupply-priority.md) Implementation Status):

- color band hue 24 to 30, saturation from 140, value from 200, with no close;
- a square component 24 to 69 px across on a 1920 px frame that fills 12% to
  28% of its box (the disc measured 18% in every clear frame);
- each quarter of the box holds at least 10% of the component (the disc
  measured 14% to 25%; ring arcs and the glyph alone measured 0% to 1%).

Measured on the saved frames with the edited function:

| Corpus                                  | Before                    | After                         |
|-----------------------------------------|---------------------------|-------------------------------|
| 41 saved candidates                     | 41 hits, 3 on the icon    | 7 hits, all on the icon's disc |
| 187 other pursuit frames (1920x1200)    | not counted               | 29 hits, all on the icon's disc |
| Detector time per frame                 | not measured              | 2.2 ms                        |

The four extra hits in the candidate frames are real icons 26 to 27 px across
that the old detector ignored while it reported terrain in the same frame.

## First Live Session on the Disc Rule, and the Second Tightening

The next session (36m 39s) ran on the three rules above. Its first five
focus starts (measured):

| Focus    | First marker                              | What happened                                                    |
|----------|-------------------------------------------|------------------------------------------------------------------|
| 1st      | exhaust behind an indicator, `(980,805)`  | then the beacon, held near centre 20 s; `confirmed ammo=2` 24 s after the focus began |
| 2nd      | the beacon, `(1234,190)`                  | centred in 1.5 s, descent 1655 m to 373 m, count read 2 after 11 s, death about 1 s later |
| 3rd      | the beacon, `(1058,1002)`                 | descent 1035 m to 313 m, `confirmed ammo=2` after 17 s, climbing 3 s after that |
| 4th      | the crown indicator, `(890,790)`          | pitched down from 2270 m at the indicator; health 34 after 9 s, 1 after 15 s, respawn after 16 s |
| 5th      | own airframe lit by an explosion          | one tick; respawn 3 s later                                       |

Of the 19 candidate frames saved in that time, 12 showed the beacon and 7 did
not: exhaust behind an indicator icon (2), the crown indicator merged with the
red aircraft indicator (2), glare (1), the yellow squad arrow (1), the lit
airframe (1). All seven are the icon's color. The crown icon also appears in
the world with the same outline as the beacon (2 pursuit frames).

The matched shape of every real beacon is a bare circle outline with the
crossed-missiles glyph as separate pieces inside it. Two checks were added on
that (measured on the 48 hits in the frames saved up to that point):

- at most 10% of the outline component lies inside 0.62 of its radius (the
  beacon 4% or less; exhaust, glare, the arrow and merged indicators 29% to
  54%);
- the yellow inside the outline is 3% to 11.5% of the box and at least 22% of
  it lies in the box's corners (the crossed missiles 4% to 10% and 26% to 65%;
  the crown 13% to 14% and 9% to 18%).

On the 260 frames saved by both sessions the tightened detector reports 34
markers and drops 15 that the first disc-rule version reported: the 10 false ones above
and 5 real beacons whose outline had merged with the ring. The crown limits
rest on two frames.

**Session totals and a check on frames the rule was not fitted to
(measured).** The session ran 36m 39s: 6 missions, 33
pursuits, 15 `missiles exhausted`, 7 focus starts. Four focus episodes began on
or reached the beacon and rearmed (three logged `confirmed ammo=2`; in the
fourth the count read 2 before a death). Three began on something else and
ended in a respawn: the crown indicator, the lit airframe and the crown
objective's own icon and ring (52 s). The five candidates saved after the tightened rule was written were
three beacons, which it keeps, and two crown icons about 47 px across, larger
than the two it was fitted on, which it rejects. Session total: 24 candidates,
15 on the beacon.

## Known Limits

- **The minimap draws the same icon at exactly 20 px.** It matches the color
  and shape rules and is rejected only by the 24 px floor (13 minimap matches
  in the session's frames with the floor lowered to 20 px). A distant beacon
  between 20 and 24 px is therefore not detected.
- **The small hollow icon near the screen centre is not the beacon.** Operator,
  2026-10-02: it behaves like the aircraft indicator icon and points in the
  general direction of the resupply point. It is a duller, translucent yellow
  (saturation 170 to 213 against 255) and about 20 px, so the detector ignores
  it. Nothing steers by it yet; with the beacon out of view the pursuit has no
  resupply direction. This is the next change to make.
- The measurements are from one session on one map family. Other maps'
  terrain has not been checked against the new color band.

## References

- [ADR 152 — Escalating Resupply Priority in Pursuit Mode](../adr/152-pursuit-resupply-priority.md)
- `wingman/resupply.py`: `find_resupply_marker`
- `tests/test_resupply.py`: `test_live_resupply_icon_is_found_at_its_disc`,
  `test_live_yellow_lookalikes_are_not_the_resupply_icon`
- Fixtures: 13 crops named `tests/fixtures/resupply_pos_*.png` and
  `tests/fixtures/resupply_neg_*.png`, cut from the session's saved frames
