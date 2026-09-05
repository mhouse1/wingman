# ADR 115 — The Hold Altitude Must Be One the Aircraft Can Fly

| Status | Date       | Wingman Version |
|--------|------------|-----------------|
| Draft  | 2026-09-04 | 1.8.8           |

## Context

ADR 112 gave the orbit a closed loop on altitude. ADR 114 made the hold level
the aircraft before circling. Neither could be validated, because the hold could
not reach a state where either mattered.

The 21:31-21:35 hold on 2026-09-04, climbing from 908 m after a respawn:

```
21:32:43  5018 m   954 KPH
21:32:58  6011 m   605 KPH
21:33:25  6240 m   245 KPH      climb has stopped gaining
21:33:52  6061 m   479 KPH      60 s, no net altitude
21:34:13  6630 m   in band, orbiting
21:34:19  6497 m   back below the band, climbing again
```

Speed by altitude across that hold, all 85 telemetry samples:

| altitude | n | median speed | min |
|---|---:|---:|---:|
| 0-1 km | 5 | 1032 KPH | 947 |
| 2-3 km | 8 | 950 KPH | 700 |
| 3-4 km | 7 | 998 KPH | 846 |
| 4-5 km | 6 | **1154 KPH** | 809 |
| 5-6 km | 11 | 876 KPH | 481 |
| **6-7 km** | **35** | **369 KPH** | **84** |
| 7-8 km | 4 | 576 KPH | 252 |
| 8-9 km | 7 | 531 KPH | 215 |

The 7000 m target put the hold band — 6500 to 7500 m — precisely in the region
where the aircraft has no energy. Thirty-five of the 85 samples are in that
band, at a median of 369 KPH and a minimum of 84.

Everything previously attributed to the control loops follows from this. The
climb plateaus because it is at its ceiling for the energy it has. The orbit
cannot hold altitude because a banked turn at 400 KPH descends whatever the
pitch loop commands. The stall/departure cycle in ADR 110 and ADR 114 is what
an aircraft does when asked to loiter above its sustainable altitude.

**The control loops were being blamed for a target they could not reach.**

## Decision

**D1. Hold at 5000 m.** The 4-5 km band carries the highest median speed
measured (1154 KPH), and 4500-5500 m keeps the whole hysteresis band inside the
region where the aircraft has energy to spare.

**D2. Altitude is not the objective — staying alive is.** There is no tactical
reason for 7000 m. A hold has no requirement to be high; it needs to be
somewhere the aircraft can fly indefinitely. Lower is strictly better here
because energy is control authority.

**D3. Change ONLY the target.** ADR 114's recovery phase and ADR 112's closed
loop stay exactly as they are. Their live behaviour has never been observed in a
band the aircraft can hold, so changing them now would confound the next
measurement with three variables. This is the one-change-at-a-time rule that
ADR 106's unattributable row exists to enforce.

**D4. No adaptive target yet.** A plateau detector that holds wherever the climb
gives up is the obvious generalisation, and it may still be needed for a
different airframe or loadout. It is deliberately NOT in this ADR: if 5000 m
works, the mechanism is unnecessary complexity, and if it does not, the plateau
data from the next session will say what the detector should key on.

## Consequences

The hold sits 2000 m lower and closer to the fight. That is a real cost — the
survival case for altitude is that it buys reaction time — but altitude bought
at the price of 369 KPH is not survivable altitude, and the previous four holds
all ended in a stall or a departure rather than in a circle.

The evidence is **one hold in one session, 85 samples, on one aircraft**. The
effect is large and consistent with four separate failures, but the specific
figure of 5000 m is a first estimate, not a tuned value. If the next hold sits
comfortably, this number is right; if it plateaus again, D4's detector is the
next step and the plateau altitude is the number to read.

Nothing about the climb changes. `climb_mode` still exits on altitude and still
hands over whatever attitude it happens to have — ADR 114 catches that, and its
V6 is now testable for the first time.

## Validation

> **The first attempt at V1 did not test this ADR.** The session flew 7000 m
> anyway: the `loiter_mission` config block was never read by the controller.
> See ADR 116. The evidence above stands; the change itself has not yet flown.

- **V1 — live.** A hold reaches 4500-5500 m and stays there, with the orbit
  running rather than the climb re-triggering. Not yet observed.
- **V2 — live.** Speed during the hold stays in the region the table above
  associates with control authority, rather than decaying toward the stall.
- **V3 — live.** ADR 114 V6 and ADR 112 V4 become observable: a hold that keeps
  its band with no stall/departure cycle.

## References

- ADR 112 — the closed loop, correct and unobservable until now
- ADR 114 — the recovery phase, same
- ADR 110 — the stall/dive cycle, now attributed to the target rather than to
  the orbit
- ADR 073 / ADR 086 — `climb_mode`, unchanged
- ADR 106 D4 — one change at a time, which D3 follows
- `wingman/config.yaml` — `loiter_mission.target_alt`
