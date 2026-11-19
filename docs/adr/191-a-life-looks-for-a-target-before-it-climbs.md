# ADR 191 — A life looks for a target before it climbs

| Status   | Date       | Wingman Version |
|----------|------------|-----------------|
| Draft    | 2026-11-18 | 1.9.5           |

## Decision

Operator, 2026-11-18: "currently it always performs a nose up manoeuvre to try
to avoid crashing when the jet spawns in front of terrain … if target isn't
detected for a short period assume terrain is blocking the view and nose up to
avoid it. The goal is to not waste time performing nose up and entering a stall
if we don't need to and go directly to engaging a target."

- **D1. Each life gets a target watch of `acs_mission.target_watch_s`
  (2.0 s).** During it the jet flies level, and `mission_acs` checks for a
  target (a tracker lock, a ring icon, or an objective marker) every `tick_s`.
  If one shows up, the mission goes straight to the pursuit. If none does,
  terrain may be hiding the targets, so the mission climbs as before.
  0 brings back the old behaviour: one check, then the climb.
- **D2. Inside the watch, `climb_mode` refuses an ordinary climb.** A hard
  emergency (terrain, or time to ground: the tree's `emergency` verdict) still
  climbs. Arming the watch stops an ordinary climb that is already running.
- **D3. The watch starts when the jet is in the world.** On a respawn that is
  the spawn guard's `telemetry_handoff` (first fresh speed and altitude), and
  only while acs is the life's mission. On battle entry it is the mission
  start, which is the moment the HUD is confirmed (ADR 189). If a watch was
  armed in the last 10 s, it belongs to the same life, so the mission does not
  arm a second one.

- **D4. For an acs life with a watch, the spawn guard presses nothing**
  (added after the first respawn under D1 to D3). The guard (ADR 076/078)
  pulses NOSE_UP through the whole respawn screen, so the jet's first frames
  in the world are already a pull-up. It still runs, but only to find the
  hand-off that opens the watch. A j20 life, or `target_watch_s: 0`, keeps
  the pulses.
  ```
  12:10:37,711 SPAWN GUARD — holding nose up until the respawn hands off
  12:10:45,833 target watch — level for 2.0s ... (respawn, ADR 191)
  12:10:45,833 spawn guard complete (telemetry_handoff, 8.1s)
  12:10:48,235 PADLOCK: OFF | Altitude: 2128 | Speed: 470 | Nose: +90° (steep_climb)
  12:10:49,803 PADLOCK: OFF | Altitude: 2402 | Speed: 285 | Nose: +90° (steep_climb)
  ```
  The window held the tree's climb off, and nothing else pressed NOSE_UP. On
  the two earlier sessions, the first nose reading after a hand-off was climb
  or steep climb on 87 of 110 respawns, and level on 23.

- **D5. A watch-only spawn guard ends the last life's climb.** A climb that
  was still running at the death held NOSE_UP through the respawn screen,
  so the new life spawned pitching up. D3 stopped it only at the hand-off:
  ```
  12:59:53,828 spawn guard complete (telemetry_handoff, 1.7s)
  12:59:53,829 climb complete (stopped, 11.2s)
  12:59:55,345 PADLOCK: OFF | Altitude: 1483 | Speed: 496 | Nose: +90° (steep_climb)
  ```
  The climb now ends when the respawn starts the guard, whatever started
  it, emergency included. That life is over.

- **D6. A watch-only spawn guard holds ordinary climbs off too**, from the
  respawn to the hand-off. On 2026-11-18 at 13:21:47, the floor climb pressed
  NOSE_UP for 0.9 s in that gap, before the watch had opened. Then the spawn
  guard ended on `state_exit` rather than a hand-off, so it never opened the
  watch at all.

### Open: terrain deaths after D4

| Session | Lives | Terrain deaths | Per life |
|---------|-------|----------------|----------|
| 07:53 (before ADR 191) | 76 | 3 | 4% |
| 10:30 (before ADR 191) | 76 | 3 | 4% |
| 12:08 (D1 to D3) | 24 | 1 | 4% |
| 12:42 (D4) | 35 | 6 | 17% |

None of the six came in a life's first 10 s. The three traced ones came
24 to 75 s into the life, in a low pursuit dive. An example from 13:22, 24 s
into the life: a lock in the window, then pursuit from 555 m, a dive to 457 m
with "pull-out would not clear", recovery, and terrain at about 1,000 m. The
D4 session flew Crimson Canyon (3 of its 7 deaths were terrain) and Crucible
(3 of 17), while the earlier sessions flew other maps. So the map mix accounts
for some of the rise. The sample cannot separate the map from the change.

**D7 (2026-11-18 13:45): D4 ships off** (`acs_mission.spawn_watch_only:
false`), with D5 and D6, which apply only with it. D1 to D3 stay on. Splitting
lives by how they started settles it:

| Sessions | Straight to the pursuit: terrain deaths | Climbed after the window: terrain deaths |
|----------|-----------------------------------------|------------------------------------------|
| Three before D4 (pulsing guard) | 5 of 106 (5%) | 2 of 70 |
| D1 to D3 alone (12:08) | 0 of 17 | 1 of 7 |
| D4 (12:42) | 8 of 34 (24%) | 0 of 15 |

With D4 on, a life that saw a target in the window got neither the guard's
nose-up nor the climb, so its pursuit started low. The window alone (D1 to
D3) did not raise terrain deaths. A level spawn should come back through the
pursuit's terrain avoidance, not by switching D4 back on as it is.

**D7 live, 13:58 to 14:55** (shipped defaults: watch on, guard pulsing):
straight-to-pursuit lives had 5 terrain deaths in 30 (17%), and climbing lives
0 in 16. The maps were Stormbreak and Stone Forest. Median altitude when the
pursuit took over, for straight-to-pursuit lives:

| Session | Lives | Median altitude | Under 800 m |
|---------|-------|-----------------|-------------|
| 07:53 (before ADR 191) | 45 | 1,706 m | 1 |
| 10:30 (before ADR 191) | 44 | 1,554 m | 0 |
| 12:08 (D1 to D3) | 17 | 2,402 m | 0 |
| 12:42 (D4) | 36 | 1,121 m | 5 |
| 13:58 (D7) | 21 | 1,220 m | 0 |

What might cause it (inferred): the window holds off the tree's floor climb,
which used to add height in the ~2.7 s between hand-off and pursuit. Spawn
altitude varies by map, which confounds these figures. In every crash traced,
the pursuit's own "pull-out would not clear" fired too late. Recommendation:
fix this in the pursuit's terrain avoidance (the operator's rule:
low-altitude crashes belong there). `target_watch_s: 0` restores the old
height.

Earlier inference: a spawn that is not pulsed nose-up, or a
life that sees a target in the window, starts its pursuit hundreds of meters
lower, and the pursuit's dives have less height to lose.

## Consequences

- No more nose-up in the first seconds of a life when a target is about to
  come into view. Expected: about a quarter of today's climbs give way to an
  immediate pursuit. This is inferred from the climb timings below, not
  measured yet. Some of those targets may have come into view only because
  the climb raised the nose.
- On a respawn, the window mostly falls in the ~2.7 s between the spawn
  guard's hand-off and the mission restart. In that gap the tree's floor climb
  used to pull the nose up. Now the jet flies level, and the mission still
  checks for a target once when it starts.
- A spawn facing terrain with no hard emergency yet flies level for up to 2 s.
  The time-to-ground and terrain emergencies still climb inside the window.

## Evidence

Nose up from every spawn, before the mission even ran (2026-11-18, acct2):
```
10:33:35,955 BT[active]: selected=Climb ... alt=561.0 alt_rate=n/a ttg=n/a ... mission=False
10:33:35,968 Controller: spawn guard complete (telemetry_handoff, 1.7s)
10:33:37,566 PADLOCK: OFF | Altitude: 610 | Speed: 661 | Nose: +9° (climb)
10:33:38,861 Controller: mission_acs - starting mission sequence (lock acquired)
10:33:38,898 Controller: mission_acs - target in view (icon) at the start, no climb
```

`mission_acs` starts, 2026-11-18 10:30 to 11:30 session (65 starts): 38 saw a
target at the first check, and 27 climbed. 26 of the 27 found a target during
the climb.

How far into the climb the target appeared, logs 2026-11-16 to 11-18 (846
climbs; 90 never found one):

| Within | Climbs | Share |
|--------|--------|-------|
| 1 s | 107 | 13% |
| 2 s | 207 | 24% |
| 3 s | 279 | 33% |
| 5 s | 372 | 44% |

Deaths in a life's first 10 s, from black-box death stamps (same logs):
3 of 1,169 lives that did not climb, and 4 of 870 that did. Spawning into
terrain is rare either way, so a 2 s window costs little. The black box
covers only some deaths, so these are lower bounds.

### First live session, D1 to D3, 2026-11-18 12:08 to 12:42 (acct2)

| Lives | Target in the window, no climb | Climbed after the window | Ordinary climbs held off | Deaths in the first 10 s | Median life |
|-------|-------------------------------|--------------------------|--------------------------|--------------------------|-------------|
| 24 | 17 (71%) | 7 | 18 | 0 of 19 timed | 37 s |

These are the outcomes of D1 to D3. Respawns still had the spawn guard's
pulses, which D4 removes from 12:42 on. Median life over the two earlier
sessions on the old code was 67 s and 40 s, and 24 lives cannot separate
37 s from either.

### D4 live, 2026-11-18 12:42 to 13:00 (acct2)

The first nose reading after each respawn's hand-off:

| Spawn guard | Respawns | Level (0 to +1 deg) | Median | +90 deg |
|-------------|----------|---------------------|--------|---------|
| Pulsing (D1 to D3 session) | 19 | 3 | about +28 deg | 2 |
| Watch only (D4) | 9 | 5 | +1 deg | 1 (the D5 case) |

Deaths in a life's first 10 s: none of 9 timed, and the median life was 70 s.

## Validation

- [ ] Live session: `target watch` logged on each respawn and battle entry;
      count `at the start, no climb ... (+N s)` against `no target in N s`.
- [ ] Deaths in a life's first 10 s are no higher than the 3 to 4 per
      thousand lives above (at least 300 lives).
- [ ] Time from spawn to the first pursuit drops against the 11-16 to 11-18
      baseline.
