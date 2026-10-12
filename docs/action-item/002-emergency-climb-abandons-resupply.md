# Action Item 002 — The Emergency Climb Abandons the Resupply and the Pursuit

| Status | Date       | Wingman Version |
|--------|------------|-----------------|
| Draft  | 2026-10-08 | 1.9.0           |

## Request

Operator, 2026-10-08: "review logs in the last few days, the climb we added last
week is causing missed resupply and pursuit, change the climb to execute only
after target or resupply disappears."

The climb is the crash recovery of
[ADR 148](../adr/148-a-dive-recovery-flies-through-a-pursuit.md)
(`pursuit_mode.crash_recovery`, 2026-10-02): the tree's hard emergency, time to
ground under 30 s, takes pitch and roll from a pursuit until the flight path is
level again.

## What the logs show

Nine sessions, 2026-10-05 12:13 to 2026-10-08 08:00, about 12.6 hours, all on
the afterburner recovery of
[ADR 159](../adr/159-the-emergency-climbs-nose-up-carries-the-afterburner.md).
The archived logs are `logs/wingman_20261005_134015.log` to
`logs/wingman_20261008_055024.log`; the last session (2026-10-08 05:50 to 08:00)
was still `wingman.log` when this was written.

The recovery took the airframe inside a pursuit 657 times and flew 97 minutes of
pursuit. What the pursuit had in view in the second before each one:

| In view when the climb took over | Climbs | Share | Median hold | Median altitude | Median path |
|---|---|---|---|---|---|
| Its target | 316 | 48% | 7.5 s | 2068 m | -25 deg |
| The resupply marker it was flying to | 144 | 22% | 7.5 s | 2084 m | -26 deg |
| Nothing, searching for a resupply | 68 | 10% | 8.5 s | 1830 m | -23 deg |
| Nothing | 129 | 20% | 7.5 s | 1753 m | -23 deg |

- **The target is lost.** Of the 316 climbs that started with the target in
  view, 310 handed back with the target out of view. The tracker went on seeing
  it during 314 of them; the pursuit was not allowed to steer.
- **The resupply is missed.** A marker approach of 4 s or more ended in a rearm
  within 10 s in 15 of 68 cases (22%) when no climb took over, and in 10 of 121
  (8%) when one did. The period has 27 rearms against 315 pursuit deaths.
- **Most of these are not close calls.** Altitude over descent rate at the
  start was 20 s at the median, 9 s at the tenth percentile. 45 of 642 started
  with under 8 s to ground.

One of the 144, from `logs/wingman_20261007_083055.log`. The marker is in view
from 06:39:57, the climb takes the airframe 4 s into the approach, the marker
is gone 3 s later, and the chase gets the aircraft back at 297 KPH with nothing
to fly to:

```
06:39:59,522 [INFO] RESUPPLY: urgency overtook pursuit at spent=2; rearm focus begins (actuating)
06:40:03,643 [INFO] PADLOCK: OFF | Altitude: 2324 | Speed: 784 | Nose: -61° (steep_dive)
06:40:03,924 [INFO] Controller: climb — hard emergency inside a pursuit: flying through GAME_BATTLE_EJECT and the chase yields (ADR 148, cap 30s)
06:40:03,953 [INFO] Controller: pursue_and_engage — yielding pitch and roll to the dive recovery (ADR 148)
06:40:06,781 [DEBUG] RESUPPLY: spent=2 empty=False marker=- weight=10.0 opponent=0.0 proposed=False seeking=True mode=actuate marker_stale=False target_nearer=False search=False pin=-
06:40:09,641 [INFO] PADLOCK: OFF | Altitude: 1819 | Speed: 240 | Nose: -0° (level)
06:40:12,941 [INFO] Controller: climb — crash no longer predicted after 9.0s (path +20 deg), handing the airframe back to the chase
```

## What changed

`pursuit_mode.crash_recovery_clear_view_s` (shipped at 1.0, a named guess).

- Each steering cycle the pursuit reports what it is flying at and can see: its
  target, or the resupply marker when the resupply has the steering.
- The recovery does not start until neither has been in view for that long. It
  logs `crash recovery held off — the pursuit has the target in view` (or
  `the resupply marker`), at most once in 10 s.
- A recovery that is flying hands back when one comes into view:
  `climb — the pursuit has the target in view after 2.3s, handing the airframe
  back to the chase`, then `climb complete (objective_in_view, …)`.
- With nothing in view the recovery is as before. That is where it was added
  for: a resupply search pushing the nose down to the ground (2026-10-02 20:20).
- `dive_safety: true` still puts the recovery first, and `0` or `null` for the
  new key restores the recovery that does not wait.

On the period's figures, 460 of the 657 would have been held off at the moment
they started, and 68 of the other 197 would have handed back when a target came
into view.

## What it costs

- The pursuit now follows a target or a marker it can see toward the ground with
  no recovery. Resupply markers sit near terrain. With the recovery, 61 of the
  460 still ended in a death within 15 s; that number is the one to watch.
- A false lock holds the recovery off as a real one does. The tracker's locks on
  terrain and exhaust
  ([Action Item 001](001-target-tracking-lock-stability-and-false-positives.md))
  and the marker detector's
  ([Anomaly 011](../anomaly/011-resupply-focus-steers-at-terrain-and-exhaust.md))
  are the exposure.
- 1.0 s of clear view at the median descent of 130 m/s is 130 m of height before
  the recovery starts.

## Not yet done

- A live session. The checks: rearms per hour (2.1 in the period), pursuit
  deaths per hour (25), and how many `held off` episodes end in a death.
- The direction icon and the resupply pin do not count as "in view": the target
  or the marker itself has to be on screen. A climb can still interrupt a turn
  toward an off-screen target.
