# Action Item 004 — Terrain Avoidance: the Levers Left After the Camera Study

| Status | Date       | Wingman Version |
|--------|------------|-----------------|
| Draft  | 2026-10-25 | 1.9.3           |

## Request

Operator, 2026-10-25: "what improvements can be made other than terrain mapping", then "document this in a
new action-items doc".

## Where this starts

Every figure here is measured unless marked. The evidence is in
[HLDD 001](../hldd/001-terrain-avoidance-hldd.md) ("Where it stands (2026-10-24)"),
[Research 017](../research/017-camera-vision-for-terrain-avoidance.md) ("Step 3 results" and "Ordinary
flight"), [ADR 151](../adr/151-one-actuator-and-throttle-leases.md) D6 and the 2026-10-24 rows of
[Code Review 019](../code-review/019-2026-10.md).

Closed, each by measurement:

| Idea | Why it is closed |
|---|---|
| A pull-out floor per map | Crash heights vary as much inside a map as between maps; about 2 points better at the same alarm rate, inside one standard error (660 matches) |
| Holding the climb through objective hand-backs | Saves about 0.01 crashes a mission and costs as many objectives (880 missions) |
| The forward-view warnings as triggers | 1.9% to 8.2% of their alarms followed by a crash, against the live pull-out's 11.8% (49 missions) |
| ESCAPE limited by free air | 10.0% at best (498 missions) |
| The depth model with the telemetry | Ahead on death windows, but in ordinary flight 3.6% of its alarms followed by a crash against the pull-out's 6.4%, and it ranks the seconds before a crash no better than the telemetry alone (AUC 0.708 against 0.709; 32 missions) |

Fixed on 2026-10-24: another owner's key release no longer cuts the emergency climb's nose-up (ADR 151 D6;
128 cuts in 62 missions before, none in 84 after).

Open, the operator's decision: a terrain database built from the game's own map meshes (Research 017, open
questions). It is not an action item here.

## Action items

Ordered by expected value for the effort. Each one is judged on crashes a mission and on what it costs the
chase (objectives a mission, the `PURSUIT SUMMARY` lock share), over 40 missions or more.

### AI-004-1 — Refit the pull-out trigger on whole-battle logs (recommended first)

**Why.** Over ordinary flight (the recorded session of 2026-10-24 18:18, 6,117 battle seconds, 104 of them 2
to 10 s before a crash), the trigger's own continuous figure, the predicted lowest point, ranks danger well
(AUC 0.709); its live yes-or-no alarm ranks it poorly (0.553), with 6.4% of its alarm episodes followed by a
crash at 2.44 a mission. The threshold step throws information away. Two of its inputs also predate fixes:

- its nose-up rates (cycle 21) were measured while the climb's nose-up was being cut (inferred: some pulses
  lasted a fraction of a second, so the rates read low);
- its `reaction_s` (2.0) was sized when readings were a median 1.54 s old, before cycle 38 made them fresher.

**Change.** Re-measure the nose-up rates on logs after D6 (2026-10-24 02:25 on). Then refit the floor, the
reaction time and the alarm rule on whole-battle readings, which every log has (`PADLOCK` lines), scored like
the trigger study: alarm episodes 5 s apart, the share followed by a crash in 2 to 10 s, the crashes warned 2 s
ahead, alarms a mission. Fit on one set of logs and check on another.

**Success.** More crashes warned 2 s ahead at the same alarm rate, or as many at fewer alarms, held out of
sample; then a live A/B.

**Status.** Closed 2026-10-25: no setting beats the shipped one; nothing to implement.

**Result (2026-10-25, operator `/proceed with implementation`).** The nose-up rates, re-measured with cycle
21's method (consecutive readings 1 to 2.2 s apart inside an emergency climb, the first one diving;
`test_screenshots/terrain_dataset/tools/pitch_rates.py`):

| Speed (KPH) | Shipped curve (deg/s) | After D6, 139 missions (pairs) | Before D6, 10-22 and 10-23 (pairs) |
|---|---|---|---|
| 400 to 599 | 21 | 20.7 (114) | 22.2 (316) |
| 600 to 799 | 8 | 21.3 (143) | 16.8 (481) |
| 800 to 999 | 7 | 17.3 (165) | 17.6 (577) |
| 1,000 to 1,199 | 5 | 13.7 (63) | 14.4 (307) |
| 1,200 to 1,399 | 3 | 12.0 (23) | 12.2 (89) |

D6 barely moved them: the cut pulses did not depress the rates (the premise above was wrong). The aircraft
flown on 10-22 to 10-24 pulls up two to four times faster above 600 KPH than the curve, which was measured on
the Su-33 and MiG-31 of 10-17 (inferred: the rate belongs to the airframe).

Scored over whole battles (`tools/score_pullout.py`, the trigger recomputed from every reading, alarm episodes 5
s apart, a crash 2 to 10 s after), settings chosen on 441 missions of 10-22/23 and checked on 138 of 10-24
(78 crashes): every floor (300 to 900 m) and reaction (1.0 to 2.0 s) trades alarms for warned crashes at 5.4%
to 6.5% precision on the test logs, and at about the same alarm rate the measured curve warns fewer crashes
than the shipped one (floor 600, 2.0 s: 3.61 a mission, 5.8%, 37.2% warned, against the shipped curve's floor
450, 2.0 s: 3.75, 6.4%, 42.3%; the training logs agree, 7.4% and 33.0% against 8.4% and 39.3%). The shipped
curve's pessimism at speed tracks the risk, as cycle 33 found. The case for this item was also wrong: the live
alarm's AUC (0.553) against the lowest point's (0.709) compared a yes-or-no figure with a continuous one;
thresholding the continuous figure differently only moves along the same trade.

What carries over: a recovery is faster than the trigger assumes (from -50 deg at 1,000 KPH the nose comes
level in about 3.6 s, not 10), which bears on AI-004-3.

### AI-004-2 — No new dive straight after a recovery

**Why.** In the 4-hour run of 2026-10-24 (49 missions, 35 crashes), 4 of the 18 crashes that an emergency
climb started 2 s or more ahead of had the climb hand the airframe back at +7 to +15 deg (`crash_cleared`),
after which the chase dived again into the ground.

**Change.** First count it at scale: crashes within 15 s of a `crash_cleared` hand-back, over the logs since
2026-10-19, against how often a hand-back is followed by an objective or a kill. If it holds up, a short
period after a recovery in which the chase does not command a steep dive.

**Success.** Fewer crashes within 15 s of a hand-back, objectives a mission unchanged.

**Status.** Open. Not yet measured at scale.

### AI-004-3 — A continuous nose-up in a hard emergency

**Why.** The emergency climb pulses its nose-up (1.5 s on, then a check). In the 4-hour run, 7 crashes came
from climbs that started under 2 s ahead, and most of the 8 others that failed without a hand-back started 2.1
to 2.3 s ahead in -49 to -59 deg dives, where every part of a second of nose-up counts (inferred). HLDD 001
part 2 already asks for the rates to be re-measured "once part 2 holds the key continuously".

**Change.** Hold the nose-up without gaps while the hard emergency stands, keeping the existing guards
(the pitch ceiling, the misread-path check).

**Success.** A higher measured nose-up rate in dives and fewer crashes after a climb that started 2 s or more
ahead.

**Status.** Open. AI-004-1's re-measure is done: in a dive the current aircraft's nose comes up 12 to 21 deg/s
at every speed band (pulsed), so the gaps between pulses, not the aircraft, may be what limits a late
recovery (inferred); measure the climbs' pulse gaps against their outcome before building.

### AI-004-4 — Keep the speed down for the whole dive, not per reading

**Why.** The dive brake doses once per fresh speed reading, and a dive gains speed between readings: before
the resupply approaches the pull-out trigger broke into, the reading before was a median 823 to 834 KPH and the
speed at the break 850 to 1,003. In the HLDD's risk table a dive steeper than -30 deg at 1,100 KPH or more is
followed by a crash within 8 s 37.1% of the time, against 6.4% under 600 KPH.

**Change.** Hold the airbrake while the path stays steeper than the dive threshold and the last reading is
over the cap, instead of one dose per reading.

**Success.** Lower speeds at the pull-out alarms and fewer crashes in dives, at an unchanged lock share.

**Status.** Open. The operator decides: it slows the chase. It limits speed, not pitch, so it is not the
nose-down limit on marker steering the operator rejected on 2026-10-02.

### AI-004-5 — Outside terrain: missile defence

**Why.** Of the 506 deaths labelled on 2026-10-24, 417 were enemy kills and 88 crashes. The 4-hour run
logged 213 missile engagements and 3 missile evades, and 50 of its deaths came by enemy fire with missiles
still aboard. Terrain work addresses about one death in five.

**Change.** Not designed here: a separate review of the missile alerts, the evade's trigger and the flares.

**Status.** Open, for the operator to prioritise against AI-004-1 to AI-004-4.
