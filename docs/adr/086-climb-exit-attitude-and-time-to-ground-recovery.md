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

- **V1 — stalls disappear.** Over a session with a comparable climb count
  (~80 completions), near-vertical stalls (angle at or above +80 with speed
  under 120 KPH) should fall from 21 to near zero. This is the primary test and
  it can only be run live.
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
- **V5 — deaths per mission.** The 2026-08-21 baseline is 3.2 (51 respawns /
  16 missions) against 53 missiles-empty ejects. The stall share of deaths
  should drop; the eject share is expected to be unchanged, since that is the
  designed end of a mission.

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
