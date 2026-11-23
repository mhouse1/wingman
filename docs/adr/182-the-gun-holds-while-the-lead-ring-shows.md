# ADR 182 — The gun holds while the lead ring shows

| Status   | Date       | Wingman Version |
|----------|------------|-----------------|
| Draft    | 2026-11-15 | 1.9.4           |

## Decision

Operator, 2026-11-15, on two `v` screenshots (`tests/test-output/screenshot_20261115_201207.png`
and `screenshot_20261115_201210.png`): "as long as the gun lead is being displayed it should
activate, it is wingmans job to align the center of the screen to the gun lead".

- **D1. The gun holds while a lead ring is in use**, wherever the ring is on the screen. "In use"
  is [ADR 178](178-the-chase-steers-the-crosshair-onto-the-lead-ring-in-half-the-rounds.md) D2,
  unchanged: the ring was confirmed within `ring_memory_s` (0.45 s), at least `ring_min_dots` (2)
  of its dots run to the tracked marker, the target is in view, and a fresh range reading of it is
  under `steer_max_km` (1.2 km). This replaces ADR 178 D3, under which the gun held only while the
  ring was within `fire_px` (60 px) of the crosshair, and only in a `ring` round.
- **D2. It applies in every round.** A `marker` round fires on the ring too.
- **D3. The chase steers the crosshair onto the ring in every round.** `steer_share` is 1.0 (was
  0.5), so ADR 178's per-round trial ends: there is no `marker` arm left to compare against.
- **D4. Without a ring in use, the 2026-09-28 rule is unchanged.** The gun holds while the marker
  is within `gun_centre_err` and lets go at 1.5 times that. The range gate is also unchanged:
  [ADR 177](177-the-gun-fires-only-under-2-km.md)'s 2 km, or
  [ADR 173](173-the-special-weapon-fires-on-the-jets-trigger-when-its-icon-is-lit.md) D2c's 3 km
  on a jet whose special-weapon glyph is the gun cool-down. A ring in use is always under 1.2 km,
  so the gate never stops the ring rule.
- **D6. It applies whatever has the steering** (operator, 2026-11-15, "yes"): a resupply run, a
  resupply search or an objective run as well as the chase. Before, the gun was allowed only while
  the chase steered at its target, so on the 21:01 session a ring stayed in use for 27 pictures
  with the gun off, as at 22:09:38 (`PATH: goal=resupply`, ring 212 and 307 px out, `gun=-`).
  Those runs steer at their own point, not onto the ring, so these bursts are mostly well off
  target and add gun heat. Without a ring, the gun still needs the chase to have the steering.
- **D5.** `pursuit_mode.gun_lead.fire_while_ring_shows: false` restores ADR 178 D3. The schema
  default is false, so a config without the key behaves as before.

ADR 178 is not edited. `fire_px` now only sets the radius of the ring-on-crosshair measure
(`GUNAIM: ring within 60 px under 1.2 km`).

## Consequences

- **The gun fires far more under 1.2 km.** Today it was off for 85% of the pictures that had a
  ring in use ("Evidence").
- **More gun heat.** On a jet without the cool-down, longer bursts reach overheat sooner, and the
  gun is silent until it cools. The cool-down jets press `q` at 80% heat (ADR 173 D2f).
- **No control arm.** Steering share 1.0 and the fire rule on in every round leave nothing to
  compare within a session. Round score has an SD of about 200, so a before-and-after
  comparison gives a direction, not a verdict.
- `HOLD[gun]: None -> fire (lead ring showing ring_px=N range=…)` is the new open line. A release
  with no ring in use reads `off centre err=…` as before.

## Evidence

The screenshot `screenshot_20261115_201210.png`: an F-100 with the LAU-3 rocket pod, a MiG-29
at 0.55 km, the red lead ring right of the crosshair. The gun had let go 50 ms before:

```
20:12:09,910 [DEBUG] LEADRING: confirmed at=(997,615) red score=0.68 km=0.73 cross_px=38 marker_px=133 dots=4 gun=fire cost=21ms
20:12:09,950 [DEBUG] HOLD[gun]: fire -> None after 0.2s (off centre err=+0.164 err_y=+0.034)
20:12:10,000 [INFO] Controller: 'v' key pressed - capturing screenshot
20:12:10,095 [DEBUG] LEADRING: confirmed at=(1038,618) red score=0.68 km=0.59 cross_px=79 marker_px=156 dots=4 gun=- cost=23ms
20:12:10,289 [DEBUG] LEADRING: confirmed at=(1054,632) red score=0.66 km=0.55 cross_px=97 marker_px=173 dots=4 gun=- cost=23ms
20:12:10,751 [DEBUG] LEADRING: confirmed at=(1234,610) green score=0.69 km=0.36 cross_px=275 marker_px=212 dots=4 gun=- cost=29ms
```

Half a second earlier, the ring sat 15 px from the crosshair with the gun off, because the marker
was 0.092 off centre:

```
20:12:09,082 [DEBUG] HOLD[gun]: fire -> None after 1.0s (off centre err=+0.092 err_y=+0.007)
20:12:09,508 [DEBUG] LEADRING: confirmed at=(944,610) red score=0.68 km=1.10 cross_px=15 marker_px=85 dots=4 gun=- cost=25ms
```

The round was a `marker` round (`GUNHIT: … arm=marker`), so the chase steered at the marker and the
ring rule did not apply.

The first screenshot (`201207`, 2.4 km by the log) was not a fault. This F-100's special weapon
is not the gun cool-down (`GLYPH: unknown`, best match refuel at 0.14; the session logged
`Gun range limit: 2.0 km`), so ADR 177's 2 km applies. The gun opened at the first reading under
2 km: `GUNRANGE: km=1.70` at 20:12:08.019, then `HOLD[gun]: None -> fire` at 20:12:08.036.

Measured on the eighteen session logs of 2026-11-15 plus the live one, counting `LEADRING:
confirmed` pictures under 1.2 km with 2 or more dots (a ring in use):

| | Pictures |
|---|---|
| Ring in use | 1,168 |
| Gun held | 175 (15%) |
| Gun off, ring within 60 px | 58 |
| Gun off, ring 61 to 150 px out | 88 |
| Gun off, ring past 150 px | 847 |

`HOLD[gun]: fire -> None … off centre` was logged 394 times over the same logs.

## Implementation

- `Controller._ring_aim` sets `_ring_shown_px` while a ring is in use in any round, and the
  steering error only in a `ring` round, as before.
- `Controller._pursuit_gun_tick` holds while `_ring_shown_px` is set and `fire_while_ring_shows`
  is on.
- `wingman/config.yaml`: `gun_lead.steer_share: 1.0`, `gun_lead.fire_while_ring_shows: true`.
- `tests/test_gun_lead.py`: the ADR 182 section; the ADR 178 D3 tests now pin the rule off.

## Validation

- [ ] Live: `HOLD[gun]: None -> fire (lead ring showing …)` lines, and no `LEADRING: confirmed …
      gun=-` under 1.2 km with 2 or more dots after the first picture of a ring.
- [ ] Live: `GUNAIM: ring` in every round that sees a ring.
- [x] Live, 21:01 session: 74 opens `lead ring showing`; the gun held on 57% of ring-in-use
      pictures (15% before); no off-centre release with a ring in use. 27 pictures missed while a
      resupply run had the steering, which D6 fixes.
- [x] Live: no `LEADRING: confirmed … gun=-` repeat during a resupply run. Session of
      2026-11-15 23:51 to 11-16 07:03 (69 missions): after a ring's first picture, the gun was
      on in 2 of 3 ring-in-use pictures under a resupply run. The one off picture followed a
      `target lost` release, and the gun reopened on the next picture
      (`06:15:30,152 HOLD[gun]: None -> fire (lead ring showing ring_px=56 range=0.61km)`).
      In the chase the gun was on in 528 of 531. The sample under a resupply run is small.
- [ ] Gun damage per round (`GUNHIT:`) and overheat count (`GUNHEAT:`) against the 2026-11-15
      sessions.
