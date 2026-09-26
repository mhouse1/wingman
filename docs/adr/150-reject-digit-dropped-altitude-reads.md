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

## Related

- ADR 097 — Altitude Plausibility Gate: Units and Anchor Poisoning (Accepted, unchanged)
- ADR 086 — dive recovery (time-to-ground trigger)
- ADR 148 — a dive recovery flies through a pursuit
- Design 005 — Target Tracking, "Look-down search" (`docs/hldd/005-target-tracking-hldd.md`)
