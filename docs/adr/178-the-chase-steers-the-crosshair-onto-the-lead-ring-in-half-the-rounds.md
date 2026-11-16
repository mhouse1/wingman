# ADR 178 — The Chase Steers the Crosshair onto the Lead Ring in Half the Rounds

| Status   | Date       | Wingman Version |
|----------|------------|-----------------|
| Draft    | 2026-11-13 | 1.9.4           |

Operator, 2026-11-13: "implement phase3" of
[Design 021](../hldd/021-gun-aim-on-the-lead-ring-hldd.md), the design for aiming the gun at the
game's lead ring.

## Decision

- **D1. Two arms, drawn per round.** At each round start, `pursuit_mode.gun_lead.steer_share` (0.5)
  of rounds draw `ring` and the rest draw `marker`. Each round is tagged `gun_aim` in the session
  stats, as [ADR 172](172-agreeing-terrain-warnings-leave-the-resupply-run-in-half-the-rounds.md)
  and [ADR 174](174-the-map-warning-starts-the-emergency-climb-in-half-the-rounds.md) tag theirs.
- **D2. Steering in the `ring` arm.** While a lead ring of the tracked target is in use, the chase
  takes its roll and pitch error from the ring against the crosshair, not from the marker against
  the frame centre. A ring is in use when all three hold:
  - it was confirmed (seen in two pictures in a row) within `ring_memory_s` (0.45 s);
  - at least `ring_min_dots` (2) of its four dots run to the tracked marker;
  - the target is in view;
  - a fresh range reading of the target is under `steer_max_km` (1.2 km). Added after 19:53 in round 7, the second
    ring round, which steered for 0.7 s on control point A's dashed circle, matched as a ring at
    1.9 km. The game draws the ring at about 1.1 km and under.

  Without such a ring, the chase steers at the marker as before. Every rule that outranks the chase
  acts on whichever error is steering: the dive guard, terrain, the resupply run, objectives and the
  climb-out. The steering code and the hold model are unchanged.
- **D3. Firing in the `ring` arm.** While a ring is in use, the gun holds while the ring is within
  `fire_px` (60 px at 1,200 px high; 40 until 2026-11-14) of the crosshair. It lets go past 1.5 times
  that, the factor the gun already uses. The operator, 2026-11-14: "we just need to get it close, the
  game's aim assist will zoom the camera in and bring it closer". From 40 to 60 px the next picture had
  the ring within 60 px 80% of the time (Design 021). With no ring in use, the gun uses the marker rule (the target within
  `gun_centre_err`), so a round that never sees a ring flies as a `marker` round does. ADR 177's
  2 km range gate applies in both arms.
- **D4. The `marker` arm** is today's rule, unchanged.
- **D5.** `steer_share: 0` turns the trial off, and every round flies the marker rule.

Phase 2 of the design (fire on the ring, steering unchanged) is not flown on its own. When the
chase steers at the marker, the ring is a median 275 px from the crosshair (2026-11-13, 219
confirmed rings), so the ring's fire rule would almost never open the gun. Steering on the ring
and firing on it have to come together.

## Consequences

- In `ring` rounds the aim moves from the target to a point ahead of it, up to about 300 px away.
  Near the ground that is new behaviour. Deaths and crashes per round are judged by arm.
- The steering deadband (`gun_centre_err` 0.05, about 48 px by 30 px) is wider than `fire_px`, so
  the chase may stop correcting short of the 40 px the gun needs. That is Design 021's D6 (finer
  aim), measured here and not changed.
- Logs:
  - `GUNAIM TRIAL: round N — <arm> arm` at each round start;
  - `GUNAIM: ring|marker` when the aim point changes;
  - `HOLD[gun]: … (ring on the crosshair ring_px=…)` when the gun opens on the ring;
  - `arm=` and `aim=` on each `GUNHIT:` line;
  - at shutdown, `GUNAIM: ring within 40 px under 1.2 km` and the `GUNHIT:` sums, both by arm.

## Evidence

The baseline, measured on the Phase 1 shadow (Design 021 trial log): over 44 missions on
2026-11-13, the gun fired 41 bursts with no missile launch nearby, for 28.0 s, and gained no damage.
Only 5 of 100 bursts opened with a confirmed ring, at 17 to 367 px from the crosshair.

The settings are named guesses with a measured basis:

| Setting | Value | Basis |
|---|---|---|
| `fire_px` | 40 | The operator fired with the ring a median 30 px from the crosshair, 7 of 8 times within 51 px |
| `ring_memory_s` | 0.45 | Confirmed rings followed each other within 0.26 s 60% of the time and within 0.42 s 70% of the time (196 gaps, 2026-11-13), so 0.45 s spans one missed picture |
| `ring_min_dots` | 2 | 216 of 254 confirmed rings (85%) had 2 or more dots. Inferred: a ring with none is a false match or another aircraft's ring |

## Validation

Recorded in Design 021's Phase 3 trial log:

- [ ] 40 or more rounds in each arm.
- [ ] The share of pictures under 1.2 km with the ring within `fire_px` of the crosshair, by arm.
- [ ] Gun damage per round is higher in the `ring` arm.
- [ ] Deaths per round and terrain crashes per round are not higher in the `ring` arm.

## Related

- [Design 021](../hldd/021-gun-aim-on-the-lead-ring-hldd.md): D3, D4 and the Phase 1 baseline.
- [ADR 177](177-the-gun-fires-only-under-2-km.md): the range gate, in force in both arms.
