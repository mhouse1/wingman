# ADR 069 — Eject Descent: Impulse Rotation and Ballistic Descent

| Status | Date       | Wingman Version |
|--------|------------|-----------------|
| Accepted | 2026-08-10 | 1.7.2           |

Supersedes [ADR 068](068-eject-dive-angle-target-and-over-rotation-evidence.md)
(Accepted) on decisions 6 and 7 — hold-nose-down-through-descent and the
angle-target confirmation criterion. ADR 068 is not modified; its decisions 1-5
(evidence-gated over-rotation, per-attempt scoping, the nose-up reversal
precondition) stand and are carried forward. Also revises
[ADR 067](067-metric-hud-units-pitch-normalization-recalibration.md) on which
speed value the angle ratio uses.

## Context

ADR 068 decision 6 held NOSE_DOWN through the descent on the reasoning that the
game auto-levels the moment pitch input stops. The 2026-08-10 06:21 eject trace
(v1.7.2, post-CR-014 implementation) shows that design producing a limit cycle
and descending at **less than half the rate of the released aircraft**.

The sequence, abridged — one eject, 79 s from command to script exit:

```
06:21:08  eject commanded (NOSE_DOWN + AFTERBURNER)
06:21:12  correction 1/3 (band=climb)
06:21:19  correction 2/3 (band=dive)
06:21:25  dive confirmed via target angle (nose -90deg) — holding
06:21:30  Altitude: 10902 | Speed: 303 | Nose: -22deg     <- decayed while held
06:21:31  dive decayed while held — re-establishing
06:21:37  correction 1/3
06:21:43  dive confirmed via target angle (nose -90deg) — holding
06:21:48  Altitude:  9658 | Speed: 296 | Nose: -20deg     <- decayed again
06:21:49  dive decayed while held — re-establishing
06:21:55  correction 1/3
06:22:00  total nose-down budget (40s) exhausted — releasing
06:22:03  Altitude:  8530 | Speed: 522 | Nose: -90deg     <- hands off from here
06:22:27  Altitude:  5417 | Speed: 1286
```

Three independent faults, each verified against the trace arithmetic.

### Fault A — the angle metric saturates during acceleration

`pitch_angle_deg` divides the altitude rate by the **smoothed** speed
(`TelemetrySignal.stable_value`, a 3-sample windowed mean spanning ~9 s). In a
dive the aircraft accelerates faster than that window tracks, so the ratio
inflates and clamps at 1.0 — reported as `-90deg`. Every confirmation in the
trace is such an artifact:

| Sample | Alt rate | Instantaneous speed | Angle (instantaneous) | Smoothed speed | Angle (as computed) |
|--------|----------|---------------------|-----------------------|----------------|---------------------|
| 06:21:24 | -110 m/s | 469 KPH | **-58 deg** | 313 KPH | -90 deg (ratio -1.26) |
| 06:21:42 | -108 m/s | 481 KPH | **-54 deg** | 343 KPH | -90 deg (ratio -1.14) |
| 06:22:00 | -108 m/s | 481 KPH | **-54 deg** | 341 KPH | -90 deg (ratio -1.14) |

The controller confirms "vertical" at a true flight-path angle near -55 deg,
then reads the artifact clearing as a decay. Both the confirmation and the
decay that follows it are measurement artifacts of the same smoothing lag.
ADR 067 chose `stable_value` deliberately for noise immunity; that choice is
correct for the plausibility filter and wrong for a ratio whose denominator is
changing fast.

### Fault B — continuous nose-down descends at less than half the hands-off rate

| Phase | Duration | Altitude lost | Mean descent rate | Best sample |
|-------|----------|---------------|-------------------|-------------|
| Held (nose-down commanded) | 51 s | 3018 m | **-59 m/s** | -127 m/s |
| Released (hands off, afterburner on) | 27 s | 3504 m | **-130 m/s** | -192 m/s |

The released aircraft descends **2.2x faster** and accelerates monotonically
throughout (481 → 522 → 551 → 583 → 615 → 646 → 676 → 703 → 817 → 1286 KPH),
while the held aircraft oscillates (469 → 303 → 224 → 481 → 471 → 296 → 214)
with descent rate oscillating in lockstep.

The physical reading consistent with all of it: continuous nose-down rotates
the airframe past its velocity vector, angle of attack goes extreme, induced
drag spikes, speed bleeds, and the flight path flattens — a high-drag mushing
descent, not a dive. Each "correction" releases and re-presses the key, the
AoA normalises briefly, the jet accelerates and the rate recovers, and the
cycle repeats with a ~18 s period. ADR 068 d6 read that repetition as the game
auto-levelling; it is the controller fighting itself.

### Fault C — the speed-independent confirmation path is unreachable

`confirm_descent_fps: 250` was carried over from imperial-era tuning (ADR 067
found the naming misleading but left values alone). Interpreted in the metric
units the signal actually carries, 250 m/s exceeds the fastest descent observed
anywhere in this trace (-192 m/s). Every confirmation therefore comes through
the angle path — the one Fault A corrupts — and the raw-rate path that would
have been immune never fires.

### Why the nose-down budget looked useless

It is worse than useless as specified and accidentally beneficial in effect.
As specified (ADR 068 d4, a loop-prevention backstop) it never prevented a
loop; what it actually did at 06:22:00 was terminate a counterproductive
controller, after which the aircraft descended twice as fast. The budget was
the only thing that stopped the limit cycle.

## Decision

**1. Confirm on the flight-path ANGLE, with descent rate as the fallback.**
*(Revised 2026-08-10 — see "Revision: rate alone is satisfied by speed".)*

The dive must reach `target_dive_angle_deg` (75) to count as established, and
rotation resumes once it sags past `dive_angle_floor_deg` (60); the band
between them is a deliberate anti-flapping deadband. `descent_target_mps` /
`descent_floor_mps` remain as the fallback criterion for when the angle is
unavailable (stale speed), where a rate test is better than no test.

**2. Impulse rotation, not continuous hold.**

Rotation is commanded as a bounded NOSE_DOWN pulse (`rotation_pulse_s`,
default 2.0) followed by a mandatory observation gap of at least one telemetry
refresh (`observe_after_pulse_s`, default 3.5). The controller never holds the
key while waiting to see what the last input did — the measure-correct-measure
principle ADR 038 established, applied to the key itself rather than only to
the decision.

**3. Ballistic descent is the steady state.**

Once the rate target is met, NOSE_DOWN stays released and AFTERBURNER stays
held. This is the phase that actually produces the descent; the controller's
job in it is to watch, not to fly.

**4. Re-pulse only on sustained rate degradation.**

A single sub-floor sample is noise. Two consecutive distinct samples below
`descent_floor_mps` (default 60) return to rotation. This replaces the decay
detector, which fired on angle artifacts.

**5. Budget in pulses, with a wall-clock backstop.**

`max_rotation_pulses` (default 4) bounds actuation; `eject_max_s` (default 120)
bounds the whole sequence. `total_nose_budget_s` and `hold_max_s` are retired —
held seconds stop being a meaningful quantity when the key is only ever pulsed.

**6. The angle ratio uses instantaneous speed.**

`TelemetrySnapshot.pitch_angle_deg` and `pitch_band` both divide by the last
accepted speed reading rather than `stable_value` (they share the ratio, so
leaving one smoothed would make the logged angle and band contradict each
other). Revises ADR 067's data-model choice for these two consumers only; the
plausibility filter keeps the smoothed value, where noise immunity is the point.
This makes the logged nose angle truthful (-58 deg where it read -90 deg) and
the over-rotation guard trustworthy.

**7. ADR 068 decisions 1-5 carry forward unchanged.**

The over-rotation guard still requires an observed descent in the current
attempt, still demands a distinct-sample streak, and the nose-up reversal still
requires the dive target to have been reached. Those decisions fixed real
misdiagnoses and are orthogonal to the actuation change.

**8. Afterburner is gated on descending flight.**

Burner while the flight path is shallow accelerates the aircraft across the map
— the arena-exit failure tracked as Roadmap 001 M1. Engage it once the descent
is established, not at eject command.

## Consequences

**Positive:**
- Descent rate roughly doubles on the evidence above, halving time-to-respawn.
- The confirmation criterion stops depending on a metric that saturates exactly
  when it matters.
- Far fewer key events per eject (bounded pulses vs continuous hold plus
  release/re-press churn), reducing the missed-injection surface.
- The logged nose angle becomes usable for diagnosis instead of pinned at -90.

**Negative:**
- Ballistic descent is open-loop by design; a genuinely stuck shallow descent is
  detected only after two sub-floor samples (~6 s at the current cadence).
- `descent_target_mps` and `descent_floor_mps` are new tuning values without
  historical baselines; the defaults here come from one trace and need a
  multi-session sample.

**Neutral:**
- The behavior-tree Eject leaf (ADR 024 3.1b) is unaffected — it selects and
  starts the tactic; this ADR changes only what the tactic does.

## Threshold derivation (archive corpus, 2026-08-10)

Fault B replicates across the whole archive, not just the trace above. All 66
session logs, **624 eject windows**, altitude samples split by whether
NOSE_DOWN was commanded at the time:

| Regime | Descending samples | Mean rate | p25 | Median | p75 |
|--------|--------------------|-----------|-----|--------|-----|
| NOSE_DOWN held | 2016 | -93 m/s | -128 | **-80** | -40 |
| Nose released (ballistic) | 3110 | -138 m/s | -205 | **-117** | -57 |

Ballistic descents are ~1.5x faster at the median across the corpus (2.2x in
the 06:21 trace). Defaults follow from this:

- **`descent_target_mps: 100`** sits between the two medians — "descending
  better than a typical held mush". Reached in 86% of the 427 ballistic runs
  with 3+ samples, median 4 samples after release. A 120 target reaches only
  80% of runs for no meaningful gain in dive quality.
- **`descent_floor_mps: 50`** is well clear of the ballistic median, so only a
  genuine flattening trips it; combined with the two-consecutive-sample rule it
  is the conservative end of the 40-60 band the corpus supports.

Both remain provisional until multi-session live data exists (see plan item 3).

## Validation plan (gate to Accepted)

1. ~~Replay the archived session corpus through the new rate-based
   confirmation.~~ **Done 2026-08-10** — 624 eject windows; results and the
   resulting defaults above.
2. ~~`make test` and `make tp` green, including both runtime gates.~~ **Done
   2026-08-10** — 458 tests pass and `make tp` is fully green (ADR 044 replay
   gate, ADR 045 live lane, performance preview showing no regressions). The
   replay gate exercises the new controller end to end (`descent control
   engaged` → `rotation pulse 1/4` → `rotation pulse 2/4` → `cancelled during
   descent (reason=respawn_detected)`), with pulses spaced 7.0 s apart exactly
   as the pulse plus observation gap plus check interval predict.
3. ~~Live sessions showing sustained descent rates, no limit cycling, and
   reduced time-to-respawn.~~ **Done 2026-08-10** — see below.
4. Re-validate `max_rotation_pulses: 6` (raised from 4 on this evidence) and
   revisit `descent_target_mps` / `descent_floor_mps`, which the live data did
   not challenge.

### Live validation session 2026-08-10 08:52 (6h 11m, 21 missions, 52 ejects)

Zero errors, zero tracebacks, one manual takeover. Every failure mode this ADR
targets is absent:

| Measure | v1.7.2 (ADR 068 hold design) | v1.7.2 + ADR 069 |
|---------|------------------------------|------------------|
| Limit-cycle events (`descent degraded`) | 9-26 per session | **0** |
| False over-rotation aborts | 2-7 per session | **0** |
| Eject duration | mean 63-69 s | **mean 39 s, p50 42 s** |
| Ballistic descending rate | -117 m/s (corpus median) | **-219 m/s median** |
| Nose readings saturated at -90 | dominant | **4.2%** (65 of 1546) |

Descent established in 39 of 52 ejects, median 17 s. Afterburner gating fired
50 engagements and 3 shallow-descent releases. One eject, from the hardest
starting state observed (a +339 m/s zoom climb):

```
08:53:49  descent control engaged (impulse rotation, target 100 m/s)
08:53:51  rotation pulse 1/4 (rate 339 m/s, nose +59deg)
08:53:58  rotation pulse 2/4 (rate 213 m/s, nose +59deg)
08:54:05  rotation pulse 3/4 (rate  17 m/s, nose +14deg)
08:54:12  rotation pulse 4/4 (rate -36 m/s, nose -52deg)
08:54:19  descending (-129 m/s) — afterburner engaged
08:54:20  descent established (-177 m/s, nose -63deg, 4 pulse(s), 31.0s) — ballistic
          10263 -> 9606 -> 8777 -> 7769 -> 6605 -> 5210 -> 3037 m
          speed  713 ->  899 -> 1115 -> 1334 -> 1549 -> 1940 -> 3277 KPH
08:54:43  cancelled during descent (reason=respawn_detected)
```

Three decisions are visible working in that one trace: the burner stayed OFF
through the entire 30 s climb (d8 — under the old design it burned from the
eject command, which is what crossed the arena); the angles read -50 to -65
instead of pinning at -90 (d6), with -90 appearing only on the final genuinely
near-vertical sample; and one establishment held to impact with no decay cycle
(d3/d4).

**Tuning change from this data:** `max_rotation_pulses` 4 -> 6. Twelve ejects
needed all four pulses and four (8%) exhausted them without establishing —
rotating out of a hard zoom climb costs 4 pulses at ~7 s each. With zero
over-rotation events recorded, the tight budget was not buying safety.

**Open observation:** 10 of 52 ejects (19%) ended on `telemetry lost during
descent` rather than respawn detection. They still completed; whether this is
OCR dropout during high-speed descent or a genuine gap deserves its own look.

## Revision: rate alone is satisfied by speed (2026-08-10)

The first live sessions on the rate criterion exposed a defect in decision 1 as
originally written. Two ejects established and then flew the whole descent at
-40 to -47 degrees, and the controller did nothing, because the rate target was
being met — by speed, not by attitude:

```
18:36:40  descent established (-149 m/s, nose -51deg, 2 pulse(s), 18.5s) — ballistic
18:36:43  Altitude: 9757 | Speed:  806 | Nose: -47deg      rate -187 m/s  (target -100: MET)
18:36:49  Altitude: 8571 | Speed: 1026 | Nose: -47deg      rate -234 m/s  (MET)
18:36:55  Altitude: 7106 | Speed: 1253 | Nose: -47deg      rate -278 m/s  (MET)
18:37:01  Altitude: 5347 | Speed: 1576 | Nose: -45deg      rate -309 m/s  (MET)
```

The rate is three times the target and rising, while the flight path never
gets steeper. The cost is horizontal: that 7545 m of descent at -47 degrees
carries the aircraft **7036 m across the arena**. The same altitude at -75
degrees costs 2022 m, and at -90 degrees, nothing.

The error is self-inflicted and specific: decision 1 chose rate *because* the
angle was saturating (Fault A) — but decision 6, in this same ADR, fixed the
angle by dividing with instantaneous speed. Verified against the trace above,
the angle is now truthful and stable (-47.5, -46.9, -47.7, -46.9, -47.2, -44.8
computed independently from altitude and speed). Decision 1 was compensating
for a defect that decision 6 had already removed.

Consequent changes:

- The criterion is the angle (decision 1 above); rate becomes the fallback.
- Afterburner gating moves to the angle for the same reason — burner in a
  shallow dive is precisely what crosses the arena (decision 8).
- `max_rotation_pulses` 6 -> 12: pulses now MAINTAIN the dive through the
  descent, not merely establish it once. Still bounded per pulse, still gated
  by the over-rotation guard (0 misfires in 52 ejects) and by `eject_max_s`.

Regression coverage added: a shallow-but-fast dive (-47 degrees at a rate past
the target) must neither establish nor engage the burner, an established dive
that sags to -47 must resume rotation, and the -75/-60 deadband must be left
alone. All three fail against the rate-only criterion and pass against the
angle one.

**Still unproven:** whether the aircraft can *sustain* -75 degrees, or whether
the game's auto-level will pull it shallower between pulses and produce
repeated maintenance pulsing. The trace shows pulses reaching -66 and -71
transiently before sagging, so the deadband may need widening or the target
lowering. Live evidence decides it.

### Calibration: -75 is not reachable; the target is -65 (2026-08-10 18:50)

The first session on the angle criterion established **zero** dives in 8 ejects
and pulsed 49 times. The flight path oscillates rather than settling:

```
18:52:55  Nose -70   18:53:04  Nose -72   18:53:16  Nose -62
18:52:58  Nose -56   18:53:07  Nose -53   18:53:19  Nose -75
18:53:01  Nose -55   18:53:10  Nose -45   18:53:22  Nose -71
```

It touches -75 and even -90, but never on two consecutive samples, so
`confirm_consecutive: 2` never fires. Across 84 in-eject descending samples the
median is **-58**; only 11.9% reach -75, 28.6% reach -65, 47.6% reach -60. Per
eject, two consecutive samples reach -75 in 2 of 8, -65 in 6 of 8, -60 in 7 of 8.

Two corrections follow:

- **`target_dive_angle_deg` 75 -> 65.** The game will not hold the aircraft near
  vertical between pulses; -65 is the steepest target that establishes reliably.
  (Note the log rounds `-74.5` to `-75`, so logged values overstate what the
  code sees — the real -75 hit rate is below even the 2 of 8 above.)
- **`dive_angle_floor_deg` 60 -> 55.** The floor must be steeper than the -47
  sag that prompted the revision, or the reported failure sits inside the
  deadband uncorrected. The remaining 10 degree band absorbs normal
  oscillation.

Horizontal cost at the new target: 0.47 m per metre of descent at -65, against
0.93 at -47 — roughly half the lateral travel, which is the arena-exit measure
this milestone (Roadmap 001 M1) actually cares about.

**Honest limitation:** -90 degrees, the intuitive goal, is not achievable with
impulse control in this flight model. The aircraft oscillates around a -58
median regardless of pulse cadence. Holding it steeper would require the
continuous nose-down that Fault B showed produces a high-drag mush descending
at *half* the rate. -65 is the best available trade, not the ideal.
