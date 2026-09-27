# ADR 150 — Reject Digit-Dropped Altitude Reads

| Status | Date       | Wingman Version |
|--------|------------|-----------------|
| Draft  | 2026-09-26 | 1.8.11          |

## Context

On 2026-09-26, from 01:54 to 02:41, wingman ran three sessions with the look-down
search and a 2,300 m pursuit floor (Design 005, "Look-down search"). They logged
**16 hard-emergency climbs inside a pursuit** (ADR 148). Every one checked was started
by a single altitude read with a digit missing. The filter accepted it. The time-to-ground
term computed a descent of 700 to 990 m/s, and the climb pulled the aircraft out of the
fight. The true altitude reappeared on the next read, 3 s later.

Log excerpts (`wingman.log`, `PADLOCK` lines are the published altitude):

```
02:25:39,647 PADLOCK: OFF | Altitude: 3078 | Speed: 159 | Nose: +41° (climb)
02:25:42,654 PADLOCK: OFF | Altitude: 312 | Speed: 260 | Nose: -90° (steep_dive)
02:25:44,457 Controller: climb — hard emergency inside a pursuit ... the chase yields (ADR 148)

02:31:42,911 PADLOCK: OFF | Altitude: 3196 | Speed: 214 | Nose: -14° (dive)
02:31:45,917 PADLOCK: OFF | Altitude: 312 | Speed: 282 | Nose: -90° (steep_dive)
02:31:45,959 BT: DIVE RECOVERY — 2s to ground (alt=2249m rate=-960m/s) — climb forced (ADR 086 d2)

02:39:40     PADLOCK: OFF | Altitude: 2817 | ...
02:39:4x     PADLOCK: OFF | Altitude: 2 | Speed: 259 | Nose: -90° (steep_dive)
```

The pairs seen tonight (true, then misread) were: 3078/312, 3106/311, 3196/312, 2853/285,
2959/300, 3299/331, 2729/276, 3396/334, 2817/2, and further single-digit `2` reads. Once
(02:26:12 to 15) the filter accepted 334 and then **rejected the true 3396**.

Why the gate admits them: ADR 097 D2 bounds a step by `max_alt_rate_mps` (1000) times
the gap, and altitude lands about every 3 s. A drop of up to about 3,000 m per read
therefore passes, and 3078 to 312 is 2,766 m. ADR 097 D3 then makes it worse. Two
agreeing rejects reseed the filter, so a true value arriving after an accepted
misread can be treated as the outlier.

Accepted rates tonight (4 logs, 8,133 ticks; measured): descents fall off smoothly to
about 200 m/s. Distinct descent events were: 9 at 200 to 300 m/s, 6 at 300 to 490 m/s
and 27 at 697 to 993 m/s. There is a mirror set of +500 to +999 m/s climbs, which are
the snap-backs after each misread. The fastest real dives inspected tonight descended
at about 100 to 142 m/s, and the top airspeed read was 873 KPH (242 m/s).

## Decision

**D1. An altitude read below `telemetry.digit_drop_ratio` (0.2) of a fresh anchor of at
least 1,000 m is rejected.** "Fresh" means within `stale_after_s`, the same test the
delta gate uses. It targets the observed failure, the true value divided by about 10
or collapsed to one digit, and leaves ADR 097's ceiling as it is. Lowering the ceiling
instead would contest ADR 097's calibration of real dives up to 919 m/s, which this
ADR does not re-examine.

**D2. These rejects are never seedable.** Two digit-drops in a row agree with each
other (312 and 311), and under ADR 097 D3 they would reseed the filter onto the
misread.

**D3. Below a 1,000 m anchor the rule does not apply.** Near the ground a real descent
can cross the ratio within one gap (900 m to 150 m in 3 s is 250 m/s).

**D4 (added after live run 2). A run of misreads cannot age the anchor out.** An anchor that went stale only
through rejections (`rejected_streak > 0`) still counts for the digit-drop test for `digit_drop_window_s` (15).
Digit-drop rejects hold the anchor. They count toward neither D3's reseed nor the three-reject clear, because
clearing would make the next misread the seed. Cases: 03:18:47 (294, 28) and 03:50:22 (30, 297).

**D5. A lost leading digit.** A read under 1,000 m against a four-digit anchor is rejected when reaching it would
need a fall faster than 300 m/s. That is above the top airspeed read that night (1,057 KPH, 294 m/s). It catches
783 for 2,449 (555 m/s). It keeps a real 1,198 to 910 m in 3 s (96 m/s, 03:25:24), which the flat
"under four digits" rule first tried would have rejected, blinding the recovery at the bottom of a dive.

Not covered: a wrong digit that keeps four (2214 for 2,842, 3941 for 3,331). That belongs to ADR 086's
single-read bypass, and changing that ADR needs its own ADR.

A persistent low reading is not held off forever. Rejects do not refresh the anchor's
timestamp, so after `stale_after_s` the gate stands aside. The low reading then seeds
fresh with no rate and therefore no false time to ground.

## Consequences

- The false "2s to ground" from a dropped digit is removed at the filter, so the
  dive guard, ADR 086's recovery and ADR 148's pursuit yield no longer see it.
- A misread that keeps its digit count (7049 for 3049, 9516 for 3516, seen on
  2026-09-25) is not caught. Those read high, which makes the guards fail late rather
  than falsely. They stay open.
- `digit_drop_ratio: 0` restores ADR 097 alone.

Tests: `tests/test_altitude_gate_adr150.py` (11, real `TelemetryProcessor`, sequences
from tonight's log; 7 fail on the unchanged filter).

## Validation

Pending the next live run. Criteria:
- no `hard emergency inside a pursuit` whose preceding `PADLOCK` altitude is under
  20% of the one before it;
- the count of such emergencies per hour against tonight's 16 in about 47 minutes;
- `plausibility filter rejected` lines showing the digit-drop reads being turned away.

### Live run 1 (2026-09-26 02:51:58 to 03:02:10, 10 m 06 s, 2 matches; `logs/wingman_20260926_030206.log`)

Measured:
- 0 hard-emergency climbs inside a pursuit and 0 yields to the dive recovery. The previous 47 minutes had 16.
- The filter rejected 6 digit-drop altitude reads (23, 31, 31, 270 and others). It also rejected 3 reads with an
  extra digit (26,891, 26,651 and 29,411), which ADR 097's ceiling already turns away.
- Pursuits locked 12% and 34% of scans.

This is a short sample, but the direction is unambiguous for the failure it targets.

### Gap found in live run 2 (03:09 run, measured)

At 03:18:51 the run produced a false emergency through a chain of misreads, with the true altitude about 2,800 m
throughout (2,811 m on the next read):
1. 03:18:38.755: 3118, accepted.
2. 03:18:41.746: 30431, rejected by the ADR 097 ceiling.
3. 03:18:44.753: 2, rejected by D1.
4. 03:18:47.747: 294, **accepted as a fresh seed**. The anchor was now 9 s old, past `stale_after_s`, so
   the gate stood aside.
5. 03:18:50.761: 28, **accepted**. The 294 anchor is below D3's 1,000 m, so D1 does not apply. The result was
   -89 m/s and "2s to ground".

The "not held off forever" behaviour in D1 lets two consecutive rejections age the anchor out. Candidate fix, not
applied: when the anchor went stale only through rejections (`rejected_streak > 0`), apply D1 against the last
accepted value for a longer window (about 15 s).

Second gap, 03:35:18 (measured). A read of 783 between the true 2,449 and 2,331 m was accepted and started the
hard emergency at 03:35:22.9. 783 is 32% of the anchor, so the 0.2 ratio missed it. A lost *leading* digit leaves
up to about 45% of the value (2,783 read as 783). Candidate replacement for the ratio: reject any read of three
digits or fewer against a fresh four-digit anchor (1,000 m or more). A real fall from 2,449 m to under 1,000 m
in one 3 s gap would be about 480 m/s, twice the top airspeed read tonight (1,057 KPH, 294 m/s).

Third class, 03:44:20 (measured). A read of 2214 between the true 2,842 and 2,717 m was accepted: a wrong digit that
keeps four digits. It implied 209 m/s against an airspeed of 445 KPH (124 m/s) and started the hard emergency at
03:44:24.9. Neither the ratio nor a digit-count rule catches it. ADR 097 dropped the "descent cannot exceed airspeed"
premise because stalls break it. The narrower option: ADR 086's single-read bypass (`confirm_bypass_time_s` 15)
should not fire on a reading that has not yet been confirmed by the next one.

## Related

- ADR 097 — Altitude Plausibility Gate: Units and Anchor Poisoning (Accepted, unchanged)
- ADR 086 — dive recovery (time-to-ground trigger)
- ADR 148 — a dive recovery flies through a pursuit
- Design 005 — Target Tracking, "Look-down search" (`docs/hldd/005-target-tracking-hldd.md`)
