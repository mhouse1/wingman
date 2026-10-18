# ADR 159 — The Emergency Climb's Nose-Up Carries the Afterburner

| Status | Date       | Wingman Version |
|--------|------------|-----------------|
| Draft  | 2026-10-05 | 1.9.0           |

Supersedes D1 item 1 of
[ADR 137](137-emergency-climb-airbrake-and-crash-instrument.md) (the airbrake for
the whole emergency hold) and the 2026-10-02 amendment of
[ADR 148](148-a-dive-recovery-flies-through-a-pursuit.md) (brake while the flight
path is below level). The rest of both stands: the tightened pulse cadence, the
mid-hold escalation, the recovery that flies through a pursuit and its hand-back.

## Decision

Operator, 2026-10-05: "wingman frequently applies nose up into a stall, when it
applies nose up it should activate afterburner to prevent stall."

**D1. The emergency climb pulls up on the afterburner.** `_run_climb_hold`
presses `AFTERBURNER_KEY` at the start of an emergency hold exactly as a routine
climb does, under the same fuel floor and rearm margin. It no longer presses
`AIRBRAKE_KEY` because the hold is an emergency.

**D2. The airbrake is for a steep dive only.** The hold brakes, and drops the
burner, only while the flight path is steeper than
`behavior_tree.climb.emergency_airbrake_below_deg` (shipped at -40). Shallower
than that the airbrake comes off and the burner lights, while the path is still
below level. A hold that starts with no angle does not brake. A missing angle
mid-hold keeps the current state, as before. `null` turns the airbrake off
altogether.

**D3. A mid-hold escalation changes the pulse cadence, and brakes only under
D2.** A de-escalation always releases the airbrake.

**D4. A hold releases the airbrake at its end only if it holds it.** The key is
not leased, and most emergency holds never press it now.

## Consequences

- The cruise afterburner and the afterburner evade yield to an emergency climb
  only while its airbrake is actually held. In a shallow recovery they keep
  their burn.
- A shallow recovery leaves the dive faster and loses more height in the
  pull-out than a braked one did. That is the cost of D1. It is what to watch in
  the first live session: terrain deaths in the session summary, against the
  stall count.
- A steep dive is braked as before until its path comes up through -40 degrees.
  The burner then lights there instead of at level.
- D2 is one exception to the operator's rule, kept for the dives the airbrake
  demonstrably saved (see Why). `emergency_airbrake_below_deg: null` removes it.
- Not changed: stall prevention below 300 KPH, and every other nose-up writer.
  The pursuit's own nose-up still takes its burner from the cruise hold, and a
  spawn or mission-start climb that reaches +90 degrees with the burner already
  lit still stalls. Those are the 7 trips this decision does not address.

## Why

`wingman.log`, 2026-10-05 02:36 to 03:39, one `make rd` session. Stall
prevention tripped 54 times in 63 minutes. What held nose-up in the 12 s before
each trip:

| Nose-up source before the trip | Trips | Burner during the pull |
|---|---|---|
| Emergency climb, airbrake held | 47 | suppressed |
| Pursuit pull-up within 10 s of such a climb handing back | 2 | off, cruise below its 90 percent rearm |
| Spawn or mission-start climb to +90 degrees | 5 | lit |

The braked recoveries start from a shallow dive. Three of the 47, HUD reads 3 s
apart, as altitude m, speed KPH and path angle:

| Emergency climb starts | Read 1 | Read 2 | Read 3 | Read 4 |
|---|---|---|---|---|
| 02:42:32 | 1929, 1162, -17 | 1586, 1116, -22 | 1317, 624, -31 | 1330, 266, +3 |
| 03:01:07 | 1840, 1289, -19 | 1597, 1296, -13 | 1628, 580, +2 | 1826, 176, +90 |
| 03:29:01 | 1889, 1079, -20 | 1380, 1347, -27 | 1069, 716, -31 | 1258, 191, +90 |

Read 1 is the last one before the hold starts. The third, as logged:

```
03:29:00,394 [INFO] PADLOCK: OFF | Altitude: 1889 | Speed: 1079 | Nose: -20° (dive)
03:29:01,931 [INFO] ⬆️  CLIMB — holding nose up + airbrake (EMERGENCY, afterburner suppressed) (target alt 5000, cap 90s)
03:29:03,386 [INFO] PADLOCK: OFF | Altitude: 1380 | Speed: 1347 | Nose: -27° (dive)
03:29:06,392 [INFO] PADLOCK: OFF | Altitude: 1069 | Speed: 716 | Nose: -31° (dive)
03:29:09,389 [INFO] PADLOCK: OFF | Altitude: 1258 | Speed: 191 | Nose: +90° (steep_climb)
03:29:09,713 [INFO] Controller: climb — path +90 deg, at or above level: airbrake released, thrust allowed
03:29:09,714 [INFO] Controller: climb complete (crash_cleared, 7.8s)
03:29:10,911 [WARNING] ⚠ STALL PREVENTION — speed 191 KPH below 300 KPH — airbrake released, afterburner held (operator directive)
```

The airbrake takes 500 to 600 KPH off every 3 s while the hold pulls. The ADR 148
amendment released it at level, by which time the aircraft was at 250 to 600 KPH
with the nose still rising.

The session had 85 emergency holds. Of the 84 with a path angle at the start, 73
started at -40 degrees or shallower and 11 steeper. The steep ones are the dives
ADR 137 was written for, and two of them left almost no height:

| Emergency climb starts | Path at the start | Steepest read | Lowest read |
|---|---|---|---|
| 02:43:06 | -46 | 394 m, 848 KPH, -90 | 138 m |
| 03:27:31 | -46 | 166 m, 910 KPH, -70 | 59 m |

A pull-out at 900 KPH needs more height than one at 400 KPH, so D2 keeps the
airbrake there. Start angles run without a gap from -10 to -55 degrees, so no
limit separates the two groups cleanly. -40 leaves 6 degrees under these two
dives and still sends 87 percent of the holds to the burner. It is a first value
to tune, not a measured optimum. Of the 47 braked recoveries that ended in a
stall trip, 35 never read steeper than -40 degrees.

## Validation

- `tests/test_climb_mode.py`, the `ADR 159` block: a shallow or unknown-angle
  emergency presses the burner and never the airbrake; a steep one brakes; the
  two trade places as the path crosses the limit; `null` never brakes; the
  escalation and de-escalation cases.
- `tests/test_pursuit_recovery.py`: the same inside a pursuit, at -19 and at -70
  degrees.
- Live, not yet run: stall-prevention trips per hour and the share that follow
  an emergency climb (was 47 of 54), with terrain deaths in the session summary
  no higher than before. `CLIMB — holding nose up + afterburner (EMERGENCY` marks
  each hold, and `is a steep dive: airbrake` each time D2 brakes.

## Amendment 1 (2026-10-17, Code Review 019 cycle 21): a fast dive brakes at any angle

The decision above keyed the airbrake on the angle alone. On 2026-10-17 the
kill card (Code Review 019 cycle 19) counted 92 crashes in the operator's 10-hour
account-2 session (`logs/wingman_20261017_192826.log`); an emergency climb had
started before 71 of them, 31 of those 3 to 12 s before the impact, and in most
of the 31 the dive got steeper while the nose-up ran. Measured over the emergency
climbs of six logs that day (320 pairs of consecutive HUD samples in a dive): the
nose came up a median 21 deg/s at 400 to 599 KPH, 8 at 600 to 799, 7 at 800 to
999, 5 at 1,000 to 1,199 and 3 at 1,200 to 1,399. A dive shallower than the -40
limit carried the burner and gained speed, losing the pitch authority it needed:

```
20:23:36,712 PADLOCK: OFF | Altitude: 1524 | Speed: 1067 | Nose: -29° (dive)
20:23:36,783 ⬆️  CLIMB — holding nose up + afterburner (EMERGENCY, airbrake only in a steep dive)
20:23:38,213 PADLOCK: OFF | Altitude: 1231 | Speed: 1152 | Nose: -38° (dive)
20:23:39,709 PADLOCK: OFF | Altitude: 841 | Speed: 1329 | Nose: -45° (dive)
20:23:40,042 Controller: climb — path -45 deg is a steep dive: airbrake on, afterburner off
20:23:41,210 PADLOCK: OFF | Altitude: 409 | Speed: 1459 | Nose: -45° (dive)
```

Amendment: a dive (path below 0) at or above `emergency_airbrake_above_kph` (900)
brakes too, at any angle, and holds until the speed is down to
`emergency_airbrake_release_kph` (750). The stall the decision above was written
against (1,116 KPH braked to 266) is braking past that release; at the airbrake's
measured 500 to 600 KPH per 3 s and a speed reading about 1.5 s old, the release
lands near 450 KPH, above stall prevention's 300. Both keys null: the decision
above, unchanged. Tests: the `cycle 21` block of `tests/test_climb_mode.py`.
Validation, live: the crashes that follow an emergency climb started 3 s or more
before them (31 of 92, 34%, before), and the stall-prevention trips that follow an
emergency climb.
