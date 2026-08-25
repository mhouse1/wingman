# ADR 086 — Climb Exit Attitude and Time-to-Ground Dive Recovery

| Status | Date       | Wingman Version |
|--------|------------|-----------------|
| Draft  | 2026-08-21 | 1.8.5           |

Repairs a failure chain that spans [ADR 073](073-climb-tactic-shadow-first.md)
(climb tactic), [ADR 081](081-climb-pitch-ceiling-and-sustain-floor.md) (pitch
ceiling, sustain band) and [ADR 083](083-climb-lead-target-exit-and-burner-cut.md)
(predictive exit, burner cut). None of those are modified — each decision was
correct for the case it addressed. This ADR records what the composition of
them does at the moment the climb *ends*, and what the altitude-based recovery
band cannot do once the aircraft is falling.

## Context

The 2026-08-21 04:16 session (1h46m, 16 missions) recorded **51 respawns across
16 missions — 3.2 deaths per mission** — and **21 near-vertical stalls**
(flight-path angle at or above +80 degrees with speed under 120 KPH). One of
those deaths was a ground impact with **two missiles still in the rack**: not an
eject, not a missiles-empty dive, but the aircraft flying itself into the
ground under ordinary `Engage` control.

### The failure chain

`climb_mode`'s exit path releases NOSE_UP, NOSE_DOWN and AFTERBURNER together
(`controller.py`, the climb `finally` block). Releasing all three leaves the
aircraft **ballistic at whatever attitude the climb ended in** — it does not
lower the nose. ADR 083's predictive exit cuts the burner early and correctly,
but a burner cut is a thrust decision, not an attitude decision.

The traced crash, in full:

```
06:01:31  climb — reached target alt 5000 — afterburner cut
06:01:32  climb complete (altitude_recovered, 20.3s)   <- exits at 6364 m, nose +73
06:01:34  Alt 7030  Speed  814  Nose +79      <- still climbing after "complete"
06:01:37  Alt 7525  Speed  574  Nose +90
06:01:40  Alt 7814  Speed  298  Nose +90
06:01:43  Alt 7906  Speed   60  Nose +90      <- stalled
06:01:48  Alt 7879  Speed   24
06:01:52  Alt 7535  Speed  344  Nose -37      <- falls off into the dive
06:01:55  Ammo missiles: 2                    <- NOT an eject
06:02:05  Alt 5268  Speed 1286  Nose -46
06:02:08  Alt 3946  Speed 2134  Nose -90
06:02:14  Alt  439  Speed 2138  Nose -68      <- impact
```

The same shape appears at the very first climb exit of the session, so this is
the systematic behaviour and not a one-off:

```
04:20:16  climb complete
04:20:19  alt  7715  speed 1294  nose +81
04:20:34  alt 10088  speed   94  nose +90     <- +2373 m further, airspeed gone
```

**84 climb completions produced 21 recorded stalls.** ADR 081's
`max_pitch_deg: 80` governs the climb *while it holds*; nothing governs the
attitude the climb *leaves behind*, and the aircraft coasts past that ceiling
after the hold releases.

### Why the recovery band could not fire

The Climb leaf is correctly ranked above Engage in the selector
(`behavior_tree.py`), so tactic priority is not the defect. The bands are:

| Band | `enter_below_alt` | Margin at the observed 552 m/s descent |
|------|-------------------|----------------------------------------|
| sustain | 4000 m | 7.2 s |
| emergency | 500 m | **0.9 s** |

Against that budget: telemetry ticks about every 3 s, and `confirm_reads: 2`
requires **roughly 6 s to confirm a band crossing**. The emergency floor
offers 0.9 s of altitude at dive speed. *The confirmation takes longer than the
margin provides* — the emergency band is arithmetically incapable of firing in
a dive, at any tuning of the altitude value alone.

It was also blinded. The telemetry plausibility filter rejected readings
throughout the descent (`total_rejected` 94 to 101 across that window), and
`make_climb_condition` documents that **`altitude is None` freezes the
decision — neither entering nor exiting the band**. Rapid altitude and speed
change is exactly what trips the plausibility filter (ADR 038/067), so the
recovery net goes deaf precisely when the flight condition is most extreme.

The root mismatch: **the safety net is specified in altitude, but the emergency
is governed by descent rate.** 4000 m is 40 s of margin in level flight and
7 s in a dive; a fixed altitude cannot express "how long until the ground".

## Implementation status

| Decision | Status |
|----------|--------|
| d1 — exit attitude | **Implemented 2026-08-21** (`Controller._climb_exit_push`, `@relation(SAF-010)`), config `exit_pitch_deg` / `exit_push_pulse_s` / `exit_push_max_pulses`, six unit tests. First live session: fired on every climb, 3 of 4 exhausted the budget without moving the nose — see d6. Re-measure once d6 keeps climbs inside the ceiling. |
| d6 — no relight over the ceiling | **Implemented 2026-08-21** (climb fuel-recovery branch), one regression test. Necessary but insufficient on its own — see d7. |
| d7 — predicted-angle ceiling | **Implemented 2026-08-21** (`_at_pitch_ceiling`), config `pitch_lead_s`, one regression test. d6 alone was a no-op in practice. |
| d2 — time-to-ground trigger | **Implemented 2026-08-21** (`make_climb_condition`), config `recover_below_time_s`. |
| d3 — single-read bypass | **Implemented 2026-08-21**, config `confirm_bypass_time_s`. |
| d4 — descent memory | **Implemented 2026-08-21**, config `descent_memory_s`. Ten tests replay the 18:41 crash. |

d1 is deliberately first: it removes the *upstream cause* of the stalls, while
d2-d4 improve recovery once a dive has already started. If d1 works, d2-d4
protect a case that should stop occurring — worth having, lower urgency. The
ADR stays Draft until d2-d4 land and V1-V5 report.

## Decision

### d1 — The climb exits by commanding attitude, not by going neutral

`climb_mode` must end with a bounded NOSE_DOWN command that returns the
flight-path angle to a configured `exit_pitch_deg` band, and only then release
to neutral. Releasing three keys at once and hoping the airframe settles is
what leaves it ballistic at +73 degrees.

The push is bounded the same way ADR 069 bounds the eject rotation: impulse
plus observation gap, a pulse budget, and a wall-clock cap. It stops on the
first sample showing the angle inside the band. If telemetry is unavailable the
push runs for a single fixed pulse and then releases — an unverified small
nose-down is safer than an unverified ballistic climb, which is the current
behaviour.

### d2 — Recovery triggers on predicted time-to-ground, not on altitude

The emergency band's condition becomes:

```
time_to_ground = altitude / max(descent_rate, epsilon)
```

evaluated only while descending, and compared against
`recover_below_time_s` (proposed 12 s) rather than an altitude constant. The
existing `enter_below_alt` is retained as a floor for the case where descent
rate is unavailable — a hard altitude backstop, not the primary trigger.

This makes the trigger scale with the emergency: in level flight it never
fires, and in the observed 552 m/s dive it fires at roughly 6600 m — nearly
twenty seconds before impact instead of 0.9 s.

### d3 — Short time-to-ground fires on a single read

`confirm_reads: 2` is correct for a quiet band crossing, where one bad read
must not launch a climb. It is wrong when the predicted time-to-ground is
inside the confirmation window itself: waiting for a second read spends the
margin the trigger exists to protect.

When `time_to_ground` is below `confirm_bypass_time_s` (proposed 6 s), the
recovery fires on the first qualifying read. The asymmetry is deliberate and
matches ADR 064's reasoning about tiers: a spurious climb costs a few seconds
of mission time, an unrecovered dive costs the airframe.

### d4 — Rejected telemetry does not freeze the recovery decision

The freeze-on-None policy stays for the *sustain* band, where it prevents
band-edge chatter. For the emergency band it inverts: a rejected reading during
an established descent is itself evidence of rapid change, so the condition
holds the last known descent state rather than clearing it, for up to
`descent_memory_s` (proposed 5 s). Absence of perception must not read as
absence of danger.

### d5 — Configuration

```yaml
behavior_tree:
  climb:
    exit_pitch_deg: 20          # d1 — release only once the angle is at or below this
    exit_push_pulse_s: 1.0      # d1 — bounded impulse, ADR 069 pattern
    exit_push_max_pulses: 3     # d1 — budget; then release regardless
    recover_below_time_s: 12.0  # d2 — predicted seconds-to-ground trigger
    confirm_bypass_time_s: 6.0  # d3 — below this, fire on one read
    descent_memory_s: 5.0       # d4 — hold descent state across rejected reads
```

`enter_below_alt` and `confirm_reads` keep their current meanings and values.

### d6 — The burner does not relight while the aircraft is over the pitch ceiling

ADR 083 d3 cut thrust at the target altitude on the finding that *"the pitch
ceiling fighting a lit burner is what left 59% of high-angle stretches
stalled"*. That gate is keyed on **altitude only**. Below the target altitude
the fuel-recovery branch could still relight the burner at any pitch angle —
the same trap the ADR describes, in the one regime its gate does not cover.

Observed on 2026-08-21, first session running d1:

```
08:44:50,179  Altitude: 3869 | Speed: 1241 | Nose: +64deg (steep_climb)
08:44:50,493  Controller: climb - fuel recovered to 59% - afterburner re-engaged
08:44:53,181  Altitude: 4968 | Speed: 1361 | Nose: +76deg (steep_climb)
08:44:56,182  Altitude: 6006 | Speed: 1248 | Nose: +86deg (steep_climb)   <- ceiling exceeded
08:44:59,182  Altitude: 6931 | Speed: 1039 | Nose: +90deg (steep_climb)
08:45:02,183  Altitude: 7595 | Speed:  720 | Nose: +90deg (steep_climb)
08:45:02,291  Controller: climb exit - pitch budget (3 pulses) exhausted
08:45:05,181  Altitude: 7939 | Speed:  392 | Nose: +90deg (steep_climb)
```

The relight at +64deg carried the nose to +90deg. The ADR 081 d1 ceiling did
fire nose-down at +86deg and the d1 exit push then spent its full three-pulse
budget — **neither moved the nose**, because at +90deg with speed collapsing
1241 -> 392 the elevator has no authority left. Both controls were commanding
the right thing far too late.

**Decision:** the fuel-recovery relight is additionally gated on being inside
`max_pitch_deg`. Thrust is not restored to an aircraft that is already
over-angled, regardless of altitude.

The gate is angle-specific, not a blanket suppression: once the nose returns
inside the ceiling the burner relights normally, so a climb that is merely
fuel-starved is unaffected.

**This supersedes nothing.** It extends ADR 083 d3 to the case its altitude
gate misses, on the same physical reasoning.

### d7 — The pitch ceiling tests the predicted angle, not the current one

d6 shipped and did not help: 28 of 29 climb exits still exhausted the d1 pulse
budget and the nose still reached +90deg. The gate itself worked — it was
simply never true when it mattered.

```
09:40:39,966  climb - fuel 8% reached floor 10% - afterburner released
09:40:39,472  Altitude: 1099 | Speed: 712 | Nose: +48deg
09:40:41,496  climb - fuel recovered to 89% - afterburner re-engaged   <- legal at +48deg
09:40:42,470  Altitude: 1648 | Speed: 629 | Nose: +90deg               <- 1s later
...
09:40:51,855  climb - fuel 4% reached floor 10% - afterburner released
09:40:53,672  climb - fuel recovered to 50% - afterburner re-engaged   <- legal at +57deg
09:40:57,482  Altitude: 6031 | Speed: 1409 | Nose: +90deg
```

Both relights were genuinely below the 80deg ceiling when they fired. Telemetry
lands every ~3s (`ocr_every_n_ticks: 2`) while a lit burner rotates the
airframe at roughly 11deg/s, so any test against the **current** angle reacts a
full sample late — by the first read that shows 86deg the aircraft is already
at 90deg with the elevator stalled.

**Decision:** the ceiling tests `angle + pitch_rate * pitch_lead_s`, where
`pitch_rate` is measured across the last two angle samples and `pitch_lead_s`
defaults to the telemetry interval. Both consumers — the ADR 081 nose-down
pulse and the d6 relight gate — go through one `_at_pitch_ceiling()` predicate,
so they cannot drift apart.

This is the same lead-prediction ADR 083 d1 applies to altitude ("compare the
PREDICTED altitude at the next sample"), for the same reason and with the same
shape. A falling or steady nose is never blocked, so a legitimate steep climb
is unaffected; only a nose *committed* to the ceiling is.

**Fuel note.** The relight cycle itself is worth a later look: fuel fell from
89% to 4% in twelve seconds, so the climb is spending its entire reserve in one
burst and relighting the moment it recharges. d7 stops that from being fatal,
but the underlying oscillation is untouched and is not in this ADR's scope.

## First live evidence — d7 works, d1 does not yet

2026-08-21 11:14, the first session to reach a battle after the ADR 087 chain
was cleared. Peak nose angle **within each climb window**:

| climb | peak nose | ceiling |
|-------|-----------|---------|
| 1 | (no reading) | 80 deg |
| 2 | +53 deg | 80 deg |
| 3 | +75 deg | 80 deg |
| 4 | +63 deg | 80 deg |

**No climb reached +90 deg.** Against the pre-d7 sessions, where a relight at
+48 deg had the nose vertical one second later and pinned there, this is the
mechanism working. The relight at 11:14:27 (+46 deg) now peaks at +75 deg and
comes back down — 57, 52, 53, 55, 62, 65 — instead of saturating.

The +90 deg readings that remain in the log are all outside climb windows:
eject-and-dive rotations and the ballistic coast after release.

**d1 is still not working.** The exit push exhausted its budget on 2 of 3
climbs. But the reason has changed, and the fix is different from what the ADR
assumes:

```
11:14:51  Nose +57deg, speed 892 — climb exit: pitch budget (3 pulses) exhausted
11:14:54  Nose +40deg
11:14:57  Nose +26deg
11:15:00  Nose +13deg
```

At +57 deg and 892 speed there is ample control authority — the airframe simply
needs more than 3 seconds to rotate 37 deg to the 20 deg band. And it gets
there on its own within 9s of release, with no input at all. So the budget is
mis-sized, and it is an open question whether the push earns its place: the
nose comes down either way.

**The stall moved, it did not stop.** The climb overshoots badly — target 5000,
actual apex 9047 — and dies at the top instead of on the way up:

```
11:14:45  Altitude 7009 — climb: reached target alt 5000 — afterburner cut
11:14:51  Altitude 8526  (climb releases)
11:15:03  Altitude 9673 | Speed 44 | Nose +90deg     <- mush, then eject
```

Cutting thrust at the target (ADR 083 d3) does not arrest a burner climb that
is already supersonic: it coasted 4000m past the cut. This is the next thing to
address and it is squarely ADR 083's mechanism, not d1's.

Sample is 4 climbs in one session. The d7 result is a clear mechanism change;
the stall-rate criteria in V1/V5 still need a full session to measure.

## Deviation from the proposed thresholds, and why

The 2026-08-21 18:41 crash — the case d2 was written for — showed the proposed
12 s window is not enough, for a reason the ADR did not anticipate.

```
18:40:35  Alt 9203 | Speed  197 | Nose  -5deg   missiles=3  AttackSupport
18:40:47  Alt 8226 | Speed  683 | Nose -58deg   missiles=2  Engage
18:40:56  Alt 5669 | Speed 1266 | Nose -74deg   missiles=2  Engage
18:41:02  Alt 2301 | Speed 2611 | Nose -68deg   missiles=2  AttackSupport
```

It mushed at 9203 m, fell 6900 m in 27 s, and the tree selected Engage or
AttackSupport the whole way down with two missiles aboard. No climb, no eject.

**The altitude the tree reads is not the altitude the aircraft is at.** The
snapshot carries the *smoothed* stable value, which lags badly at high descent
rates. At 18:41:02 the tree saw **4096 m** while the aircraft was at **2301 m**
— a 1795 m error, about 3 s of flight. So d2's arithmetic, run on the value
the tree actually has, yields:

| Time | Tree sees | Rate | Predicted TTG | Real TTG |
|------|-----------|------|---------------|----------|
| 18:40:56 | 6636 m | -338 m/s | 19.6 s | 16.8 s |
| 18:41:02 | 4096 m | -673 m/s | 6.1 s | 3.4 s |

At the proposed 12 s the trigger fires around 18:41:00 — roughly 4 s of real
margin, which is not a recovery. At **20 s** it fires at 18:40:56, about 10 s
before impact, which is.

Shipped values: `recover_below_time_s: 20.0`, `confirm_bypass_time_s: 10.0`
(raised from 6 s for the same lag reason), `descent_memory_s: 5.0` as proposed.

The lag is worth fixing at the source — the emergency band arguably wants the
freshest accepted reading rather than the smoothed one — but that changes what
every other consumer of `snapshot.altitude` sees, so it is deliberately not
bundled here.

## d2 live evidence 2026-08-21 — 18 firings, 16 genuine

First long session with the trigger active (2h 19m, 23 missions, 78 respawns).

| ttg at fire | altitude | count |
|-------------|----------|-------|
| 11-17s | 4998-6398 m | 10 |
| 2-4s | 296-4947 m | 6 |
| after respawn | 324 m, 3874 m | 2 |

The ten firings at 11-17s are the trigger doing exactly what d2 was written
for: catching an established dive at altitude with double-digit seconds of
margin, where the pure altitude band would not have fired until far too late.

The six at 2-4s are late, and the cause is the known smoothing lag documented
under "Deviation" above — the tree's altitude trails the aircraft's in a fast
descent, so ttg is computed from a value that is already stale. They still fire,
just without useful margin.

Two fired within 15s of a respawn (5.5s and 14.6s), the discontinuity artifact.
The respawn guard added after this session suppresses exactly these two and
none of the sixteen genuine firings.

Not yet established: whether any of the sixteen actually *prevented* a crash.
Respawn count (3.4 per mission) is unchanged from pre-d2 sessions (3.2-3.4), so
there is no aggregate effect visible yet.

## Consequences

- The climb tactic gains an attitude obligation it did not have. ADR 073 framed
  the climb as "hold nose up until altitude recovers"; it is now "recover
  altitude **and hand the airframe back in a flyable attitude**". The exit is
  part of the manoeuvre, not the absence of one.
- Two ADR 069 patterns are reused rather than reinvented: bounded impulse
  rotation with observation gaps, and a pulse budget with a wall-clock cap.
  That ADR's finding — that continuous nose input mushes the airframe — applies
  to the exit push and is why it is pulsed rather than held.
- Recovery becomes untestable by altitude alone. Any future tuning must be
  validated against descent *rate*, and the replay lanes cannot reproduce it
  (recorded frames carry no rate). This needs live validation, as V1 below.
- A predicted-time trigger inherits the quality of the rate estimate, which the
  plausibility filter is known to reject under exactly these conditions. d4
  mitigates by holding state, but a persistently wrong rate could fire the
  recovery spuriously in level flight. `recover_below_time_s` is deliberately
  set well inside the region where a spurious climb is cheap.
- The 21 stalls in the 2026-08-21 session are the measurable baseline. If d1
  works, that count should approach zero while climb completions stay near 84.

## Validation

- **V1 — stalls disappear.** Measured as a **rate**, not a raw count: sessions
  differ by more than 20x in climb count, so totals are not comparable. Two
  rates matter, both computable from any session log:

  | Session | Duration | Climbs | Stalls | Stalls / climb | Stall-caused deaths | Share of deaths |
  |---------|----------|--------|--------|----------------|---------------------|-----------------|
  | 2026-08-20 15:19 | 8 h | 508 | 48 | 0.09 | 14 | 6% |
  | 2026-08-20 23:29 | — | 21 | 1 | 0.05 | 1 | 8% |
  | 2026-08-21 06:02 | 1h46m | 84 | 21 | 0.25 | 3 | 6% |
  | 2026-08-21 07:34 | 56 min | 46 | 9 | 0.20 | 4 | 19% |

  A stall is a telemetry sample at or above +80 degrees flight-path angle with
  speed under 120 KPH. A **stall-caused death** is a respawn within 60 s of a
  stall with no `MISSILES EMPTY` between the two — i.e. the aircraft died
  without reaching the designed eject path. Pass criterion: **stalls per climb
  completion approaches zero** and stall-caused deaths fall to zero, with the
  eject share of deaths unchanged.

  Note that roughly **four in ten stalls kill** (4 of 9 in the 07:34 session),
  so the stall rate is not a cosmetic metric — it converts to deaths at a high
  rate. This is the primary test and can only be run live.
- **V2 — the exit push does not mush.** Confirm from telemetry that the exit
  reaches `exit_pitch_deg` within the pulse budget rather than exhausting it,
  which would indicate the ADR 069 mushing regime and call for a shorter pulse.
- **V3 — recovery fires with usable margin.** Instrument a real dive and
  confirm the trigger fires at the predicted altitude (roughly 6600 m at
  552 m/s) rather than at the old floor, and that the pull-out completes above
  ground.
- **V4 — no spurious recoveries in level flight.** Count Climb selections
  attributable to the time-to-ground trigger while descent rate is near zero;
  it should be zero. A non-zero count means the rate estimate is too noisy for
  d2 and the trigger needs a descent-rate floor as well.
- **V5 — deaths per mission.** Baselines: 3.2 (51 respawns / 16 missions,
  06:02 session) and 2.3 (21 / 9, 07:34 session). The stall share should drop
  to zero; the eject share is expected to be unchanged, since that is the
  designed end of a mission. Note the two sessions differ sharply in eject
  share (53 ejects vs 0), so deaths-per-mission alone is a poor signal — V1's
  stall-caused-death count is the discriminating measure.

## Alternatives considered

**Lower `enter_below_alt` further (or raise the sustain band).** Rejected: the
problem is not the value. At 552 m/s any fixed altitude either fires constantly
in level flight or arrives too late in a dive, because a metre threshold cannot
express a time budget.

**Cap Engage's pitch authority instead.** Rejected as the primary fix: Engage
was not commanding the climb — the aircraft was already ballistic when Engage
took the selection at +79 degrees. A pitch cap on Engage is worth considering
separately, but it would not have prevented this stall.

**Recover using the existing eject descent controller.** Rejected: ADR 069's
controller exists to *establish* a dive and hold it; recovery needs the
opposite sign and a different exit criterion. Reusing the pattern (bounded
impulses, budget, cap) is right; reusing the controller is not.

**Treat the stall as an OCR artifact.** Considered and rejected on evidence:
the flight-path angle saturates toward 90 degrees at low speed because it
divides altitude rate by speed, so the *angle* is unreliable there — but the
altitude series is not. The aircraft climbed 7715 to 10088 m while speed fell
1294 to 94 KPH. That is a real zoom to stall regardless of how the angle is
computed.
