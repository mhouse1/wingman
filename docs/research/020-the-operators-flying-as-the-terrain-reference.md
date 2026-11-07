# Research 020 — The Operator's Flying as the Terrain Reference

| Status | Date       | Wingman Version |
|--------|------------|-----------------|
| Draft  | 2026-11-04 | 1.9.4           |

Operator, 2026-11-04, taking over the controls during a `make r` session: "watch how i fly and learn how
to improve wingman", then "see how i avoid terrain, this is the biggest wingman issue".

## Summary

The operator flies low and does not crash; wingman crashes when it flies low. The difference is not how
hard either can pull out. It is knowing where the ground is going to be, the input
[Design 001](../hldd/001-terrain-avoidance-hldd.md) names as missing.

Measured over 121 logs, 2026-10-20 to 2026-11-04 (`exposure.py`):

| Per hour of flight | Operator (manual takeover) | Wingman |
|---|---|---|
| Hours flown | 2.2 | 65.5 |
| Share of it below 1,000 m | 49% | 9% |
| Crashes (no kill card) | 6.7 (15) | 12.4 (811) |
| Crashes per hour flown below 1,000 m | 6.3 (7) | 47.2 (287) |
| Shoot-downs | 41.6 (93) | 43.2 (2,826) |

Shoot-downs come at the same rate. Crashes come twice as often for wingman overall, and about seven
times as often per hour spent low, though wingman spends a fifth as much of its time there. The
operator's low-altitude figure rests on 7 crashes (a Poisson interval of about 2.5 to 13 an hour).

## What differs, and what does not

**Not the pull-out (measured).** Every pull-out from a dive of -20 deg or steeper to level within 6 s,
its fastest nose-up rate between two readings (`pullouts.py`, the 2026-11-04 logs):

| | Pull-outs | Fastest nose-up rate, median | p25 to p75 | Time to level, median |
|---|---|---|---|---|
| Operator | 24 | 21.7 deg/s | 16.8 to 27.8 | 4.5 s |
| Wingman | 296 | 23.9 deg/s | 18.1 to 30.6 | 4.5 s |

One operator pull-out out of a canyon reached about 40 deg/s (-38 to +22 deg in about 2 s at
875 KPH); it is in the upper quartile, not typical.

**Not the afterburner in dives (measured).** With fuel draining between two readings taken as the burner
lit (`burner_in_dives.py`, 2026-11-03 and 11-04 logs), the burner was lit in 47% of the operator's
readings at -30 deg or steeper (149) and in 35% of wingman's (2,190). An early count from the
operator's held keys, 8% of 25 readings, was too small a sample and is withdrawn.

**Where and when (measured).** On 2026-11-04 the operator flew at a median 797 m (p10 358, p90 1,321)
and 907 KPH, 94% of the time below 1,500 m; wingman, that session, at a median 1,925 m and 776 KPH,
23% below 1,500 m. The operator's nose stayed between -23 and +26 deg (p10 to p90); wingman's reached
+54 deg in the climbs to 3,000 m after each spawn.

**Wingman's terrain alarms, judged by the operator's flying (measured).** While the operator flew (about
13 minutes over three rounds, no crash):

- The live pull-out trigger (floor 600 m, reaction 2 s) logged `PULL-OUT WOULD NOT CLEAR` 42 times.
  Re-run offline on every reading of the first round, it fires on 35% of them (37 of 106).
- The shadow warnings fired 51 times in 394 s (SHAPE 12, LOOM 12, the sky fraction 8, two agreeing
  10, the pull-out 9). The operator pulled up within 2 s of about half of them. A roll within 2 s
  followed most of them, but the operator holds a roll key 66% of the time, so that says nothing.

At the operator's profile, then, wingman's triggers forbid the flying that scores. Its pull-out model
assumes 5 to 8 deg/s above 800 KPH (`wingman/pullout.py`, whole-climb medians from Code Review 019
cycle 21) where both fliers' fastest per-pull rates run near 22 deg/s (inferred: the two measures are not
the same quantity, so this says the model is pessimistic, not by how much).

## Wingman's crashes (measured, logs 2026-11-01 to 11-04)

485 of 1,871 deaths (26%) left no kill card. "No card" is a crash or self-inflicted, with the
exception Design 001 records for the J-20, whose altitude read freezes before some shoot-downs.

- **What came before (`crash_context.py`):** a resupply climb-out in the 12 s before 11%; an objective
  flown through or steered to 9%; neither, 79%.
- **Attitude at the last reading (`crash_attitude.py`, 483):** a steep dive (-30 deg or steeper) 31%,
  a dive (-30 to -10) 23%, level 27%, climbing 19%. Shoot-downs: 6%, 18%, 49%, 27%. 93% of crashes came
  inside a pursuit.

About half the crashes come out of a dive, and half level or climbing: into rising ground or a wall,
which a pull-up alone does not clear.

**A wall (black box `20261104_151718_death_*`).** The SU-57 flew through the resupply ring
toward an ice cliff, which filled the right half of the view 1.4 s before the impact. The scripted climb-out began
one second before the impact; the hard emergency climb came 1.6 s after it,
on stale telemetry. The two other crashes that session also came after a resupply
climb-out on the same snow map.

## How the operator flies (measured, 2026-11-04)

From a passive XRecord recorder on display `:3` (keys, buttons and pointer, plus frames: 1 a second,
5 a second in a takeover), set against the log's telemetry:

- **Keyboard only.** The pointer did not leave the centre.
- **Held time, 189 s of the first round:** afterburner 61%, roll right 38%, roll left 28%, nose up 22%,
  nose down 15%, gun (`a`) 11%, yaw left 10%, airbrake 4%. The fire key was tapped 193 times, about once
  a second. Pitch inputs are short: a median 0.6 s, both up and down, and as many pushes (24) as pulls (21).
- **Where the pulls start:** a median 727 m (lowest 302), from a median -5 deg nose and -70 m/s.
- **Into a canyon, about 7 s:** airbrake with roll and nose down to enter, under a rock arch,
  down to control point B on the canyon floor, the gun on an enemy there with the brake out, the pull
  begun with the brake still out (`+12 CAPTURED Point B`), then the burner for the climb-out. The pull-out
  trigger had called the dive unrecoverable 3 s in (lowest point -767 m at 836 m, 1,077 KPH, -48 deg).

**Two more rounds, the operator's own session (Mirage F1, scores 1,140 and 437):**

- **The same line, again and again.** Three runs at Point B on the canyon map took the same route: level
  at about 1,250 m toward the pillars, nose down under one rock arch, along the canyon floor through B, a
  pull-out into open sky. Part of the operator's terrain avoidance is
  knowing the map (inferred from three runs).
- **Braked steep entries.** Of 12 steep entries (a first reading at -45 deg or steeper, misread readings
  left out), the airbrake was held in the 3 s before 7 (58%), against 21% for any manual reading; the burner
  comes on for the pull-out. On one resupply run: brake and nose down into a -80 deg entry at 686 KPH,
  along the canyon to the ring, out into open sky. Wingman reaches the resupply ring from wherever it is.
- **Along, not over.** In the canyon the operator banks to follow the channel and steers off a wall with
  the roll keys, staying low inside the terrain, where wingman's only answer to terrain is a climb.
- **A yo-yo.** Over a minute: climb at +57 deg to about 1,650 m, push over, dive at -40 to -54
  deg to 250 to 300 m, pull at +40 deg or more, about 25 s a cycle.
- **The operator's three crashes all came in close combat.** Two, each with `AVOIDING
  CANNON FIRE` on the screen: a roll and pull, then a push, beside a wall and over a mesa top. The third, at
  220 to 250 m among a pipeline's girders, holding the gun on an Su-22 with the airbrake out, into the
  structure. None came in plain low flying. Wingman is not like that: 19% of its crashes (93 of 485,
  2026-11-01 to 11-04) had a flare roll or a pursuit evade 4 to 14 s before the respawn was seen, against
  90% of its shoot-downs. Most of wingman's crashes come in ordinary flight.
- **A hand-back low among structures.** The operator handed back at low altitude after a double
  kill among the pipelines, with 3% fuel. Wingman climbed to about 700 m, steered for the resupply marker
  through the pipeline, and flew into one of its arches 9 s later. SHAPE (`time to contact 1.9s, way
  out UP`) and the sky fraction (0.09) both warned 2 to 3 s ahead, in shadow; the
  pursuit owned the airframe (`climb suppressed — the pursuit owns the airframe`). Black box
  `20261104_162100_death_*`. One labelled true warning, not a precision figure.
- **The pull-out model's rates, by speed** (fastest reading-to-reading rate per pull-out; the caveat
  above holds): operator 23.7 deg/s under 800 KPH, 23.2 at 800 to 1,000 and 13.9 over 1,000 (38 pull-outs);
  wingman 25.0, 19.7 and 22.9 (296); the model 8, 7, and 5 to 3. In one pull-out the operator came out of -27 deg
  at 1,615 KPH, bottoming at about 667 m, where the model predicted -542 m.

The score popups that day name what pays: `DESTROYED`, `CAPTURED Point A`, `HOLDING Point A`,
`DEFENDED Point A`. The operator's first full manual round (Tornado ADV) scored 1,285 with 4 kills, about
500 more than the kills, deaths and assists predict (score = 194 + 147 a kill - 50 a death + 61 an
assist over 1,257 rounds). Control points sit low among terrain, so flying low is where those points are.

## Next (recommended)

1. **Record every takeover as a demonstration.** On entering `GAME_BATTLE_MANUAL`, wingman writes the
   game's frames at 5 a second, the operator's key state and the telemetry, until the takeover ends,
   into a dataset folder nothing prunes. Wingman already logs its terrain alarms through a takeover. The
   recorder here does this from outside; built in, every round the operator flies becomes labelled data
   without anyone starting a script.
2. **Score "what the operator would do" as a terrain warning, offline.** A small model from the picture
   to the operator's pitch and roll in the next half second, trained on the demonstrations and scored as
   Research 017 scored its candidates: the 2 to 6 s before wingman's crashes against ordinary flight,
   against the live pull-out trigger. The 2026-11-04 recording holds about 14 minutes of the operator's
   flying, 11 of them at 5 frames a second: a seed and no more.
3. **Re-measure the pull-out model's nose-up rates** after ADR 151 D6, as Design 001 already asks, before
   any change to the trigger's floor or reaction time.
4. **Taken: [ADR 172](../adr/172-agreeing-terrain-warnings-leave-the-resupply-run-in-half-the-rounds.md).**
   After an operator screenshot (a clear path to the resupply marker, a nose-up, the rearm
   missed), half the rounds keep two agreeing warnings from breaking off a resupply approach, so the
   climb's saves and its missed rearms can be set against each other.

## Data and tools

- Recording: `test_screenshots/demonstrations/20261104/` (untracked): `input.csv` (time, FSM state,
  event, key or button, pointer x, y) and `frames/HHMMSS_mmm[_T].jpg` at half scale, `T` being `M` for a
  takeover, `B` for battle and `P` for pursuit (the earliest frames carry no tag).
- Tools: `test_screenshots/demonstrations/tools/` (untracked): `record_manual.py` (the recorder: record
  only, injects nothing), `exposure.py`, `pullouts.py`, `pull_rates.py`, `crash_context.py`,
  `crash_attitude.py`, `burner_in_dives.py`, `flight_compare.py`, `terrain_study.py`, `sheet_keys.py`
  (a contact sheet with the keys held), `popups.py` (EasyOCR of the score popup; 8 minutes of CPU on
  every core for 6 frames, the model load included, so offline only).
- Logs: `logs/wingman_20261104_154646.log` (the session with three takeovers), with the archives back
  to 2026-10-20.

## Caveats

- Telemetry arrives every 1.5 to 3 s, so rates between readings are coarse, and the maximum of
  reading-to-reading rates favours noise.
- A death's altitude is the last reading, which lags the impact and freezes on some jets.
- Jets and maps differ: on 2026-11-04 the operator flew the Tornado ADV and the F-20, wingman the SU-57.
- The operator's manual flying in the older logs is counted by FSM state alone; how much of it was low
  flying by choice is not known.

## References

- [Design 001 — Terrain Avoidance](../hldd/001-terrain-avoidance-hldd.md): where terrain avoidance
  stands, and why the ground ahead is the missing input.
- [Research 017 — Camera Vision for Terrain Avoidance](017-camera-vision-for-terrain-avoidance.md): the
  scoring method for warning candidates.
- [Design 015 — Target Tracking Pursuit Mode](../hldd/015-target-tracking-pursuit-mode-hldd.md): the
  control points, flown through rather than held.
