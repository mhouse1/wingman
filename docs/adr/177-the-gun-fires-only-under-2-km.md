# ADR 177 — The gun fires only under 2 km

| Status   | Date       | Wingman Version |
|----------|------------|-----------------|
| Draft    | 2026-11-13 | 1.9.4           |

## Decision

Operator, 2026-11-13: "machine gun activation should only be allowed when the
target distance is less than 2km away". The example frame
(`tests/fixtures/gun_range/f106_1p3km_full.png`) shows an F-106 centred at 1.3 km,
where the gun must open.

- **D1.** The pursuit holds `FIRE_MACHINE_GUN` only while the tracked target is
  centred (the 2026-09-28 rule, unchanged) **and** a fresh reading of that
  target's range is under `pursuit_mode.gun_max_range_km` (2.0). It lets go when
  either stops being true. 2.0 km itself is out of range.
- **D2.** The range is read off the tracked nameplate's middle row ("1.3km") by
  `wingman/target_range.py`, on its own long-lived worker thread. The tracker's
  observation now carries the nameplate `cluster` it steered on.
- **D3.** No reading means no gun. A reading counts only if it is at most
  `gun_range_max_age_s` (1.5 s) old and was taken on a nameplate within 200 px
  of the one tracked now. Text that is not `<n>.<n>km` is no reading: the game
  always prints the point, so "13km" is a misread and not 13 or 1.3.
- **D4.** `gun_max_range_km: 0` turns the gate off and restores the centre-only
  rule.

## Consequences

- The gun fires less. The baseline below shows how much it fired before.
- If the OCR misses most ticks, the gun goes nearly silent. That is the fail-safe
  direction the operator asked for. `GUNRANGE:` lines and the session's
  `GUNRANGE: N reads, M without a range` line show it.
- One more long-lived EasyOCR reader (about 300 MB), on the `TargetRange` thread.

## Evidence

How the reader was chosen, measured on VEDA's CPU with a session running:

| Method | Result |
|---|---|
| EasyOCR detect and read on the 300 x 170 px nameplate window | 3.6 to 6.8 s per call; read "1.3km" as "13km" |
| Recognition only on the range row, plain grey | 6 of 12 curated ranges |
| The same, padded white | 7 of 12 |
| The same, red minus green | 11 of 12 |
| The same, red mask, dark glyphs on white (shipped) | 12 of 12, about 50 ms each |

The 12 readings are the operator's frame plus 11 nameplate windows from archived
frames, 0.95 to 14.0 km, each checked by eye (`tests/test_target_range.py`).

Baseline before the change (session 2026-11-13 00:03 to 02:37, `make r2`, 22
missions): 443 gun bursts, 1,192 s of firing, so 20 bursts and 54 s per mission;
median burst 1.5 s, p90 7.0 s. The range at each burst was not logged.

## Validation

- [ ] Live: `HOLD[gun]: None -> fire` lines carry `range=` under 2.00 km, and
  none fires with `range=none`.
- [ ] Live: `GUNRANGE:` hit rate (reads against reads plus misses) on battle
  ticks. Under half means the gate is starving the gun; look at the misses' text.
- [ ] Live: `GUNRANGE` cost per read, and no slowdown of the pursuit cycle.
- [ ] Over 40 or more missions: kills per round against the baseline above.
  Round score is noisy (SD about 200), so this check gives a direction, not a verdict.

## Trial log

| Session | Code | Game UI | Missions | Bursts per mission | Gun s per mission | Range read rate | Notes |
|---|---|---|---|---|---|---|---|
| 2026-11-13 00:03 | before ADR 177 | unchanged | 22 | 20.1 | 54 | n/a | baseline |
| 2026-11-13 02:58 | ADR 177 rev 1 (uncommitted on acs-j20 at 76213e3) | unchanged | 1 | 14 | 16.9 | 86% (783 of 906) | `make r2`, 8 min, stopped with z to load rev 2. Every one of 14 opens at 1.2 to 1.9 km, none at 2 km or with no range; readings fell smoothly 3.1 to 0.38 km on one closing target; cost median 62 ms, p90 217 ms. One burst cut short by a single miss (rev 2) |
| 2026-11-13 03:21 | ADR 177 rev 2: a miss no longer replaces a good reading | unchanged | 2 | 5.5 | 4.3 | 83% (876 of 1,053) | `make r2`, 15 min, stopped with z for rev 3. No release on a miss (rev 1 had 1). Two defects below, one an open at about 4.6 km; cost median 65 ms, p90 189 ms |
| 2026-11-13 03:46 | ADR 177 rev 3: impossible jumps held back | unchanged | 1 | 4 | 6.1 | 86% (422 of 486) | `make r2`, 7 min, stopped with z for rev 4. Opens at 0.62, 0.77, 1.40, 1.90 km; no release on a misread; 4 holds, all the 03:47:57 junk below |
| 2026-11-13 04:05 | ADR 177 rev 4: a repeated picture is read once | unchanged | 1 (CRIMSON) | 0 | 0 | 57% (137 of 242) | `make r2`, 9 min, stopped with z for rev 5. 67 repeated pictures skipped (rev 4's premise measured). No gun at all: misses were long junk ("845084kk.06") through the mission. Earlier runs were CRUCIBLE, STORMBREAK, STONE at 83-86%: the read rate is map-dependent (one CRIMSON mission, inferred) |
| 2026-11-13 04:28 | ADR 177 rev 5: a systematic junk first digit is dropped | unchanged | 2 (CRUCIBLE) | 3.5 | 4.9 | 84% (724 of 861) | `make r2`, 21 min, stopped with z for rev 6. No open at 2 km or more; 5 junk digits recovered, 9 held; 204 repeated pictures skipped; crops captured |
| 2026-11-13 05:01 | ADR 177 rev 6: the jet icon is cut off the range row | unchanged | 12 (COUNTDOWN, STORMBREAK) | 2.7 | 2.2 | 84% (4,022 of 4,809) | ended 06:11 by the operator (Backspace, standby in GAME_WAITING). No open at 2 km or more in 12 missions; 54 held, 5 junk digits recovered, 1,071 repeated pictures skipped. Round score 605 per round read, K/D/A 2.8/2.6/1.3 (baseline 522, 2.8/3.3/2.4): 12 rounds, within the SD of about 200, not a verdict | `make r2`. Junk digits nearly gone: 2 recovered in 2,578 reads (rev 5: 5 in 861); 26 held, mostly drops held one read. Highest open 1.90 km |

Against the baseline (20.1 bursts and 54 s of gun per mission, before ADR 177), rev 6 fires
3.6 bursts and 2.9 s per mission over 5 missions: the gate removes about 95% of the gun
time. That is the operator's rule working, not yet a judged cost: kills per round need 40 or
more missions on randomized arms to compare (round score SD about 200).

Misses after rev 6 (stacked crops, 05:00-05:05), two kinds:

- **A clean range row that still did not parse** ("1.3km", "8.1km", "7.7km" tried first and
  readable by eye). Inferred: OCR dropped the point ("13km"), which D3 refuses. Not measured:
  the log keeps only the last row's text. Next: log every tried row's text on a miss.
- **Overlapping nameplates**: another contact's jet icon and health bars cross the label
  ("Rollcage / 7.6km" under a red icon and bars); the rows merge and no clean range row
  exists. Missing here is right.

Both fail safe, and a good reading stays valid for 1.5 s across a miss.

Rev 6 (2026-11-13 04:50), the junk digit at its source. Every captured junk-digit crop
(04:32-04:39: read 44.5, 45.4, 46.5, 49.2; on the picture 4.5, 5.4, 6.5, 9.2) has a small red
jet icon left of the range text, in the same rows, and the crop ran from the icon to the text.
Measured: gaps inside the range text are 4 to 6 px on all 11 curated rows; icon to text was 13
to 105 px. `text_span` splits a row at gaps over 10 px and keeps the widest group; real OCR
then reads 4.5, 5.4, 6.5, 9.2. Rev 3 and 5's jump rules stay as the second line. The four rows
are fixtures (`rowcrop_icon_*`). A miss's capture now stacks every row tried: the last row alone
(names and types, 04:31) could not say whether the range row had been tried.

Rev 5 (2026-11-13 04:15): the junk digit is systematic, not random. At 04:10:32 a 6.2 km
range read "76.0km" and, from the next picture, "75.7km"; the two agreed (rev 4's rule) and
were kept, and the real 5.0 km was held back. Gun off: the safe direction. A reading of 10 km
or more that does not fit the kept one is now read without its first character, and kept if
that fits ("76.0" as 6.0). Replayed over all four logs: 17 recoveries, every one a range that
fits its neighbours; 31.2 against a kept 8.1 (strips to 1.2) stays held back. Instrumentation:
the colour crop behind each junk read is saved as `test_screenshots/unknown_anomalies/gunrange_*.png`
(20 per session), to find what puts the digit there; that is the fix at the source. Misses'
crops too, as `gunrange_miss_*.png` (15 per session), for the CRIMSON read rate.

**Open: maps with red scenery.** On CRIMSON the gate starves the gun (57% reads, 0 bursts in
one mission). Fail-safe as D3 intends, but it costs the gun on that map. The guess to test
with the crops: red terrain inside the 300 x 170 px nameplate window joins the band mask and
the OCR mask.

Rev 4 (2026-11-13 03:50): at 03:47:57 a range near 7 km read as "31.2km" (held back
against 8.1), then "31.2km" twice more from the same nameplate position 67 and 154 ms
later, and the second "agreed" with the held-back first and was kept; 76.9, 16.8 and 76.8
were then held back against it. Gun off throughout (the safe direction), but the agreement
rule had been satisfied by re-reads of one picture (inferred: the nested display repeats
pictures, and a repeat keeps its first picture time). A picture whose time equals the last
one submitted is no longer read, and agreement must come from another picture time. The
`GUNRANGE:` line now carries `pic=` to measure the premise.

Rev 3 (2026-11-13 03:30), two defects seen under rev 2:

- 03:24:11: "1.2km" read with a junk digit in front as "81.2km", then "21.2km", between
  good 1.2 km reads; the gun let go for 0.9 s. Gun off: the safe direction, but a cut burst.
- 03:26:00.884: "4.7km" read as "1.7km" between 4.7 and 4.5 km, and **the gun opened at
  about 4.6 km for 0.1 s**. The one breach of D1 seen so far.

D3 gains a continuity test (`TargetRangeReader.note`): a reading of the same nameplate
within 1.5 s of the kept one must lie within 0.3 km plus 1 km per second of it, or it is
held back, unless the reading before it was held back too and the two agree. It applies to a
**drop** of any size (holding it back keeps the gun off) and to a **rise to 10 km or more**
(the junk-digit signature). A rise under 10 km always replaces the kept reading, so a stale
in-range reading never outvotes a fresh out-of-range one. Replayed over both runs (1,122
readings): all 7 junk-digit reads and the 4.7-as-1.7 misread held back; 15 other
holds, every one a drop or a junk rise, so each kept the gun off for one read. Named guesses:
1.0 km/s, 0.3 km, 10 km.

Rev 2 (2026-11-13 03:05): at 03:04:15.486 the gun opened on a 1.8 km reading; the next
picture read "770" and released it 0.5 s later (`out of range range=none`), and the read
after that was 1.5 km. `latest` returned the newest read even when it found no range, so one
miss in about seven ended a burst. Misses are now counted but not kept; D3's age and nameplate
bounds are unchanged. Test: `test_a_miss_does_not_replace_a_good_reading`.
