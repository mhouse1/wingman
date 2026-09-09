# ADR 114 — The Hold Levels Before It Circles

| Status | Date       | Wingman Version |
|--------|------------|-----------------|
| Draft  | 2026-09-04 | 1.8.8           |

## Context

ADR 112 made the orbit's pitch a closed loop on altitude. Its V1-V3 were met on
the first live hold — `hold`, `down` and `up` each fired on the correct side of
the deadband. **V4 was not**, and the trace shows the control loop was never the
limiting factor:

```
21:20:50  climb starts, 2087 m below the hold
21:20:54  HANDOVER at 7113 m — nose +82 deg, 962 KPH, still zooming
21:20:57  orbit pitch down (7833 m)   nose +41   663 KPH
21:21:00  8401 m                      nose +14   407 KPH
21:21:06  8269 m                      nose -31   236 KPH   stalled
21:21:09  8108 m                      nose -69   207 KPH
21:21:21  6208 m                      nose -68   861 KPH   ballistic
```

`climb_mode` exits on **altitude** and says nothing about **attitude**. It
reached 7113 m and handed over an aircraft in a near-vertical zoom at 962 KPH.
Everything after that is ballistics: the aircraft coasted 1300 m higher, ran out
of speed, fell through the stall and departed.

The orbit's response was correct and irrelevant. A 0.35 s pitch pulse is a trim
correction; it cannot arrest an 82-degree zoom or a 69-degree departure. Worse,
the orbit's other half — the unconditional roll — is actively harmful here:
banking an aircraft at 207 KPH is how a stall becomes a departure.

ADR 112's Consequences named this exactly: *"the climb handed over at 7469 m,
already past target... If holds still enter high after this change, the climb
exit is next."* It is.

## Decision

**D1. A RECOVER phase between climb and orbit.** Inside the hold band, if the
nose is outside `level_band_deg` (20 deg), the hold levels the aircraft and does
not circle.

**D2. No roll during recovery.** The circle is what the hold is for, but an
aircraft that cannot hold its nose cannot hold a turn. Rolling first is what
turned a recoverable zoom into a departure.

**D3. A manoeuvre-length input, `recover_hold_s` (0.8 s), not the orbit's
0.35 s.** The orbit value is a trim nudge against a drift. Using it against an
82-degree attitude error is what made the live failure unrecoverable.

**D4. Below the band, a steep nose-up is NOT a recovery.** That is `climb_mode`
doing its job. Levelling there would fight the climb and the hold would never
reach altitude, which is a worse failure than the one being fixed.

**D5. Fix it in the HOLD, not in `climb_mode`.** The climb is shared with ADR
073's Climb tactic, which is `Accepted` and whose combat behaviour is not in
question here. A hold that recovers what the climb hands it is contained; a
changed climb exit would move every consumer at once. If the same overshoot
turns out to hurt the Climb tactic in combat, that is a separate ADR with its
own evidence.

## Consequences

The hold now has three phases — climb, recover, orbit — and the log names which
one it is in. The added phase costs time before the first circle, and that is
the point: the previous build began circling at 7113 m and was at 6208 m and
inverted thirty seconds later.

Recovery is attitude-only. It does not manage energy, so a hold entered at very
low speed will level the nose and still be slow. If holds keep departing after
this, throttle and the climb's own exit condition are the next candidates, and
the trace to look at is speed rather than nose angle.

`pitch_angle_deg()` is derived from altitude rate over speed, so it is None
until telemetry has two reads. With no attitude the hold behaves exactly as it
did before this ADR — it orbits — because a hold that refuses to act without
attitude would be blind more often than it is wrong.

## Validation

- **V1.** A steep nose-up handover levels and does not roll.
- **V2.** A departed nose-down aircraft is recovered, not orbited.
- **V3.** A level aircraft at altitude still orbits — recovery must not swallow
  the normal case.
- **V4.** Recovery commands a longer input than the orbit's trim pulse.
- **V5.** Below the band, a steep climb is left alone.
- **V6 — live.** A hold reaches its band and stays inside it without a
  stall/departure cycle. This is ADR 112 V4, still unmet, now with the cause
  addressed rather than the symptom. Not yet observed.

## Also found on the way

The loiter test fake did not implement `pitch_angle_deg` at all, so every new
branch would have been dead code under a passing suite — the same failure ADR
109 recorded. The fixture now builds a **real** `TelemetrySnapshot`, and it
immediately failed six existing tests by exposing stub attributes that did not
exist. Each new test also asserts its own premise (`abs(pitch) > 20`) against
the real derivation rather than assuming the numbers chosen produce the
attitude the test name claims.

## References

- ADR 112 — the closed loop, whose V4 this unblocks and whose Consequences
  predicted this cause
- ADR 110 — the stall/dive cycle first recorded there
- ADR 073 / ADR 086 — `climb_mode`, deliberately not modified (D5)
- ADR 109 — the fake-mirrors-the-caller trap this repeats
- `wingman/controller.py` — `mission_loiter`
- `tests/test_mission_loiter.py` — V1-V5
