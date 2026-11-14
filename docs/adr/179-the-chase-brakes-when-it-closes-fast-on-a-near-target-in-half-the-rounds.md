# ADR 179 — The Chase Brakes When It Closes Fast on a Near Target, in Half the Rounds

| Status   | Date       | Wingman Version |
|----------|------------|-----------------|
| Draft    | 2026-11-14 | 1.9.4           |

Operator, 2026-11-13: "there are engagements where if we see a target is moving rapidly to the left or
right we can slow down while turning and then speed up quickly to continue following target."

## Decision

- **D1. Two arms, drawn per round.** At each round start, `pursuit_mode.closing_brake.share` (0.5) of
  rounds draw `brake` and the rest draw `control`. Each round is tagged `close_brake` in the session
  stats, as [ADR 178](178-the-chase-steers-the-crosshair-onto-the-lead-ring-in-half-the-rounds.md)
  tags `gun_aim`. The two trials draw independently.
- **D2. When a `brake` round brakes.** The chase holds the airbrake while all of these hold:
  - the tracked target has the steering (no objective, no resupply run);
  - a range reading of it no older than 1 s is under `on_km` (1.5 km);
  - the range is closing at `on_mps` (150 m/s) or more, by least squares over the last 1.5 s of
    readings (three or more, spanning 0.5 s or more);
  - a fresh speed reading is at `min_kph` (650 KPH) or more.
- **D3. When it lets go.** When the range closes at less than `off_mps` (50 m/s), the target is past
  `on_km` or lost, the speed falls under `min_kph`, or after `max_hold_s` (4 s). After a 4 s hold it
  waits `cooldown_s` (3 s). The speeding up is cruise's: its afterburner comes back on its next tick
  after the brake lets go, as it does after the approach brake.
- **D4. Who holds the key.** The existing approach brake holds it (`_approach_brake_tick`, goal
  "closing target"), so cruise's afterburner yields and the same vetoes apply: a manual takeover, a
  climb, an eject and a missile evade. A dive recovery, the rearm climb-out and an objective's own
  braking outrank it as before. Within the chase it is asked before the dive's brake (rev 3).
- **D5.** `share: 0` turns the trial off.
- **D6 (rev 2, 2026-11-14 04:30).** When another brake takes the key while the closing brake holds it
  (a fast dive's, an objective's), the key is handed over held, logged `CLOSEBRAKE: airbrake handed to
  the <goal> brake`. When the pursuit yields the aircraft (dive recovery, missile evade, flare roll),
  the release names the yield. Live at 04:24:37 the dive brake took the key 0.2 s in, and the key was
  released and pressed again in the same millisecond. At 04:24:52 the flare roll took the aircraft.
  Both releases were logged as "the target no longer has the steering", which was wrong both times.
- **D7 (rev 3, 2026-11-14 04:40).** Within the chase the closing brake is asked first and the dive's
  brake second (it was the other way round). The dive goal is set in every dive of -10 degrees or
  steeper under 3,000 m, whether it brakes or not (its cap is 850 KPH). At 04:37:29 a -11 degree dive at
  726 KPH ended a closing brake 1.6 s in, the dive brake did not brake, and the release was again
  logged as the target's. Both hold the same key, so a fast dive is braked either way. When the closing
  brake lets go, the dive's takes the key over.

This ADR is the braking alone. Turning toward the side the target is going (Design 021's projection)
is a later change with its own ADR.

## Consequences

- In `brake` rounds wingman is slower in a fight at close range. Deaths per round are judged by arm.
- The afterburner is lifted while the brake holds, as on the approach brake.
- A head-on pass also closes fast, and braking cannot make it last. The closing speed is on every
  `CLOSEBRAKE: airbrake on` line, so head-on passes can be told apart afterwards. No upper limit is set.
- Logs: `CLOSEBRAKE TRIAL: round N — <arm> arm` at each round start; `CLOSEBRAKE: airbrake on — the
  target is closing at N m/s`; `SPEEDCAP: airbrake released (<why>)`; at shutdown `CLOSEBRAKE: <arm> arm
  N encounters under 1.2 km, median S s, M of 3 s or more; H holds, T s`.

## Evidence

The baseline, measured on the six Phase 3 logs of 2026-11-13 and 14
([Design 021](../hldd/021-gun-aim-on-the-lead-ring-hldd.md)): 89 encounters with three or more range
readings under 1.2 km.

| Measure | Value |
|---|---|
| Time under 1.2 km, median | 2.0 s (a quarter under 1.1 s; 71% under 3 s) |
| Closing speed to the nearest point, median | 252 m/s (73% over 150 m/s) |
| Nearest point, median | 0.48 km |
| Ended with the range opening again by 150 m or more | 6 of 89 |

Two seconds in gun range is not enough to bring the lead ring to the crosshair. Inferred: most
encounters end at their nearest point because wingman flies past the target.

The settings:

| Setting | Value | Basis |
|---|---|---|
| `on_km` | 1.5 | Named guess: a little outside the 1.2 km the encounters are counted in, since braking takes time to act |
| `on_mps` | 150 | 73% of the encounters closed faster |
| `off_mps` | 50 | Named guess |
| `max_hold_s`, `cooldown_s` | 4.0, 3.0 | Named guesses; the approach brake's longest dose is 3 s |
| `min_kph` | 650 | The lowest speed the approach brake aims for (`resupply_target_kph`) |

The range is printed to 0.1 km past 1 km, so a closing speed there is coarse (about 70 m/s either way
over 1.5 s).

## Validation

Recorded in Design 021's trial log:

- [ ] 40 or more rounds in each arm.
- [ ] Seconds under 1.2 km per encounter, by arm (the shutdown `CLOSEBRAKE:` line), against 2.0 s.
- [ ] Pictures with the ring within `fire_px` of the crosshair, and gun damage per round, by arm.
- [ ] Deaths per round and deaths with no kill card per round are not higher in the `brake` arm.

## Related

- [Design 021](../hldd/021-gun-aim-on-the-lead-ring-hldd.md): the plan's step 4 and the encounter
  measurement.
- [ADR 178](178-the-chase-steers-the-crosshair-onto-the-lead-ring-in-half-the-rounds.md): the ring aim
  trial this one runs beside.
- [ADR 151](151-one-actuator-and-throttle-leases.md): how cruise's afterburner yields to the airbrake.
- [ADR 159](159-the-emergency-climbs-nose-up-carries-the-afterburner.md): the airbrake's effect on speed.
