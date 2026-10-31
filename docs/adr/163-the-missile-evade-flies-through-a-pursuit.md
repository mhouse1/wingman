# ADR 163 — The Missile Evade Flies Through a Pursuit

| Status | Date       | Wingman Version |
|--------|------------|-----------------|
| Draft  | 2026-10-26 | 1.9.3           |

**2026-10-29: [ADR 165](165-the-evade-trial-draws-its-arm-per-round.md) replaces D4's draw per engagement
with a draw per round, and runs the trial again at share 0.5.**

Operator, 2026-10-26: `/proceed` on the recommendation of
[Research 019](../research/019-shoot-downs-and-the-missile-defence.md): let the missile evade take over during
a pursuit, record it in a new ADR, then compare live, evade on against off, over 40 missions or more.

## Decision

**D1. Inside a pursuit, the missile evade may start on a missile alert.** It is
[ADR 070](070-missile-evade-tactic.md)'s hold, unchanged: afterburner, roll right and yaw left until the alert
has been quiet for `clear_seconds`, at most `max_manoeuvre_s`. The pursuit's controller starts it, on the alert
edge: the flare handler, after the flares, calls `Controller.pursuit_evade_on_alert`. The behavior tree does
not, for two reasons. Inside a pursuit Idle holds `GAME_BATTLE_EJECT` and the tree does not act, by design
(`make_idle_condition`). And the pursuit acts within a cycle of about 0.17 s, where the tree ticks every
1.5 s, with a median 3.9 s between the alert and the hit. In `GAME_BATTLE` the tree's evade is unchanged.

**D2. The pursuit yields pitch and roll to it**, the way it yields to the dive recovery
([ADR 148](148-a-dive-recovery-flies-through-a-pursuit.md)). It releases its tracking holds and writes
neither axis while the evade flies; tracking and firing carry on, and steering resumes when the evade ends.
The dive recovery comes first: no evade starts while it flies. Once an evade flies, a climb that is not a
hard emergency is refused until it ends; a hard emergency ends it (D5).

**D3. The roll keys are leased**, as the pitch keys were on 2026-10-24
([ADR 151](151-one-actuator-and-throttle-leases.md)). The pursuit's yield releases its own tracking roll, and
when that is roll-right the release must not lift the evade's roll-right. Every roll press and release names
the same owner on both sides (audited: `tracking_roll`, `disengage_roll`, `missile_evade`, `boundary`, and the
mission pulses' label), and the takeover and shutdown sweep lifts every key whoever holds it. A roll lease has
no yields: its owners give the axis up by releasing.

**D4. It runs as a trial.** Each engagement inside a pursuit draws its arm on its first alert: `evade` with
probability `behavior_tree.missile_evade.pursuit_share`, `control` otherwise. An engagement is the session
summary's own (alerts no more than 3 s apart), so each one the summary counts carries one arm. The share
shipped at 0.5 for the trial, and is 0 since its verdict (V3, 2026-10-27). Its schema default, 0, is the
behaviour before this ADR, and 1 makes the evade always run.
Each draw is logged (`MISSILE EVADE[pursuit]: engagement N — <arm> arm`). The session summary prints
engagement survival by arm (`pursuit trial: evade arm ..., control arm ...`), counted by the arm drawn,
whether or not the evade could start.

**D5. A hard emergency ends a flying evade** (added 2026-10-27, after the first trial session: 3 of 78
evades began in a descent and crashed while the emergency climb was refused 3 or 4 times; V3 below). A climb
asked for with the tree's hard emergency (predicted time to ground, terrain ahead, the pull-out alarm;
[ADR 141](141-phase1-altitude-floor-stall-prevention-and-emergency-yields.md) D2) ends the evade the way an
eject does ([ADR 070](070-missile-evade-tactic.md) d11), from the climb's side. `climb_mode` runs its other
checks first (in a pursuit, ADR 148's objective hold-off and level-path rule), then stops the evade, waits up
to 0.5 s for its keys to come up, and climbs; the evade's exit line reads `missile_evade complete
(dive_emergency, ...)`. While that climb flies no evade starts, from the tree or the pursuit; once the tree no
longer calls it an emergency, an evade may pre-empt it as before
([ADR 073](073-climb-tactic-shadow-first.md)). A climb that is not a hard emergency, the altitude floor's,
still waits for the evade. The tree already ranks the emergency above the evade (ADR 141 D3: the evade's
condition yields to it); the controller now acts on that for an evade already flying. For the hard emergency
only, this replaces ADR 073's "suppressed while an eject or missile evade owns the airframe".

## Consequences

- The chase loses its steering for up to `max_manoeuvre_s` (6 s) an evade. What that costs is part of the
  trial: the lock share, objectives a mission.
- A hard emergency during an evade ends it (D5), in the tree's evades in `GAME_BATTLE` as well as the
  pursuit's. The first trial session ran without D5; the results table keeps each session's code, and V3
  reports the sessions with D5 on their own as well as pooled.
- Leasing the roll keys reaches every roll writer: a release lifts the key only when no other owner still
  holds it. Before, any owner's release lifted it for all.
- [FR-006](../requirements/002-functional.md) states the evade "during GAME_BATTLE". It is extended to the
  pursuit only if the trial confirms the evade: a requirement states the baselined property.
- [ADR 070](070-missile-evade-tactic.md) is Accepted and stays as it is; this ADR adds the pursuit beside it.
- From [ADR 164](164-roll-right-through-the-flares.md) (2026-10-27), every alert also rolls right through its
  flares, in both arms: the control arm is flares, the afterburner and the flare roll. The results table
  keeps sessions before and after it apart by code.

## Why

Measured in Research 019 over 193 missions of 2026-10-23 and 10-24:

- Shoot-downs outnumber crashes: 93 to 13 in the recorded session, 417 to 88 among the labelled black-box
  deaths. 556 of the 559 shoot-downs came inside a pursuit (`GAME_BATTLE_EJECT`).
- 434 of the 447 missile kills (97%) had a missile alert within 10 s, a median 3.9 s before the impact.
  Flares followed every alert, the afterburner evade 430 of 434. The missile evade ran in 4.
- Missile engagements without the evade survived 10 s in 501 of 792 (63%), from the session summaries.
- ADR 070's soak of 2026-08-14 measured 90% with the evade and 68% without, over 122 engagements. That was
  observational, outside the pursuit; its own caveat is that the no-evade engagements clustered in eject and
  respawn states. Whether the evade helps inside a pursuit is what D4 measures.

## Alternatives considered

- **Let the tree select the evade in `GAME_BATTLE_EJECT`** (Idle stepping aside for it, as for Climb's
  emergency). Rejected: Eject sits between Idle and MissileEvade and its condition, missiles confirmed empty,
  is often true in a pursuit; the tree acts once every 1.5 s; and the pursuit, not the tree, owns the airframe
  there and already hands it over to the recovery.
- **Switch the evade on for whole sessions and compare sessions.** Rejected for the trial: sessions differ in
  maps, opponents and airframes. Drawing per engagement compares the two arms inside the same missions.
- **Hand the roll-right over at the yield instead of leasing**: the pursuit forgetting its roll-right without
  releasing it. Rejected: the pursuit's timed holds release from timers, and a press inside the 0.17 s before
  the pursuit notices the evade still meets the release. The lease covers every order.
- **Let any climb end the evade, the altitude floor's too** (D5). Rejected: the floor is preventive
  (ADR 141 D2) and stays true for a whole climb back from a low respawn, so it would end most evades flown
  low. The three crashes were dives under a hard emergency.
- **End the evade first, then run the climb's other checks** (D5). Rejected: a recovery the pursuit then
  holds off would hand the airframe back to the chase, not to a climb.

## Validation

- **V1, tests.** `tests/test_evade_trial.py` (the draw, the grouping equal to the summary's, the share's
  ends); `tests/test_pursuit_missile_evade.py` (a real Controller and pursuit: the evade starts for the evade
  arm only, not outside a pursuit, at share 0 or under the dive recovery; one draw an engagement; the chase
  writes neither axis while it flies and steers again after; the roll-right survives the chase's release);
  `tests/test_tick_handlers.py` (the alert edge, after the flares, and a failure costing neither);
  `tests/test_mission_stats.py` (survival by arm, and the summary line); `tests/test_actuator.py` (the
  production lease set). Passed 2026-10-26: `make lint` clean, `make test` 3,372 passed. D5, in
  `tests/test_pursuit_missile_evade.py`: a hard emergency ends the evade and climbs, the evade's keys up
  before the climb's first press; a climb that is not an emergency waits; no evade starts while the
  emergency climb flies, and one may once the emergency clears; in a pursuit the recovery takes the airframe
  from the evade and the chase yields to it; a recovery the pursuit holds off leaves the evade flying.
  Passed 2026-10-27: `make lint` clean, `make test` 3,411 passed.
- **V2, the first live session.** `MISSILE EVADE[pursuit]` lines in both arms; `yielding pitch and roll to the
  missile evade` and `missile evade over, steering resumes` around each evade; no manual takeover from the
  evade's roll-right (ADR 070 d4's bracket); no `pursuit_evade failed`; no stuck roll key. With D5:
  `hard emergency ends the missile evade` followed by `missile_evade complete (dive_emergency` and a
  `CLIMB — holding` line; no `climb suppressed — missile evade in progress` before a crash.
- **V3, the verdict**, over 40 missions or more: engagement survival 10 s after the alert, by arm, from the
  session summaries; and the cost: the `PURSUIT SUMMARY` lock share, objectives and deaths a mission, crashes
  within 10 s of an evade. If the evade arm survives clearly better at a cost the operator accepts, the share
  goes to 1 and FR-006 is extended; if not, the share goes to 0.

## Results

| Session | Code | Missions | Engagements, evade arm / control arm | Survived 10 s, evade arm / control arm | Notes |
|---|---|---|---|---|---|
| 2026-10-26 14:46 to 18:19, the operator's `make r3`, `logs/wingman_20261026_182201.log` | before this change | 37 | no trial | no evade: 87 of 152 (57%) | Same-day baseline. 0 missile evades |
| 2026-10-26 18:22 to 22:07, `make r3` (account 3), stopped with `z` after mission 40 | this ADR, uncommitted, on `f286e2a` | 40 | 82 / 78 | 57 of 82 (70%) / 47 of 78 (60%) | First trial session; V2 and V3 below |
| 2026-10-27 05:45 to 06:04, the operator's `make r3` (account 3), stopped with `z` between rounds | with D5, uncommitted, on `f286e2a` | 3 | 3 / 7 | 1 of 3 / 6 of 7 | First D5 session. One evade ended by a hard emergency (05:53:03, after 1.6 s: the climb had been held off for the target in view until then); the aircraft hit terrain 3.3 s later, no kill card. 6 climbs that were not emergencies waited for an evade, none followed by a crash. No takeover, no error |
| 2026-10-27 06:07 to 16:33, the operator's `make r3` (account 3); matchmaking stalled at 14:50, and the liveness guard ended the session at 16:33 | with D5, uncommitted, on `f286e2a` | 94 | 214 / 200 | 131 of 214 (61%) / 106 of 200 (53%) | D5 live: 22 evades ended by a hard emergency, each followed by its `CLIMB — holding` line within 4 ms; no `still releasing`. Evades followed by a crash within 10 s: 8 of 215 (3 of 78 without D5). Of those, 2 had a pull-out already lost when the alarm fired, 1 hit before the hand-over, 1 had the recovery held off for the resupply marker in view, and 4 had no hard emergency. No takeover, no traceback |
| 2026-10-27 18:32 to 23:22, the operator's session on account 2, stopped with Backspace mid-round | with D5 and [ADR 164](164-roll-right-through-the-flares.md)'s flare roll in both arms, uncommitted, on `f286e2a` | 51 | 82 / 106 | 50 of 82 (61%) / 90 of 106 (85%) | First session with the flare roll. D5: 11 hand-overs, all `dive_emergency`. Evades followed by a crash within 10 s: 2 of 80. No takeover, no error |

**V2, the first live session (measured).** All 82 evade-arm engagements had the evade flying: 75 started it
on their first alert, 4 opened while one was still flying, and 3 found the dive recovery flying, by design.
The pursuit yielded 74 times and resumed 74 times. No manual takeover, no `pursuit_evade failed`, no
traceback, no error. Evades ended on the manoeuvre limit 43 times and on the alert clearing 35 times.

**V3 after one session (measured; 40 missions, so not a verdict):**

- **Survival, by arm**, two ways. Session summary (a death counts when its respawn is detected within 10 s of
  the alert): evade arm 70%, control 60%, +9 points; z = 1.23, a 95% range of -5 to +24 points. Impact
  time (each death to the latest engagement within 10 s): 46% against 36%, +10 points; z = 1.34, -5 to +26.
  Both lean to the evade, and neither excludes no effect.
- **No cost found in the chase.** An objective was flown through within 30 s of the alert in 6 of 82
  evade-arm engagements and 3 of 78 control ones. Against the same day's baseline session (other matches,
  so not attributable): deaths 3.05 a mission against 3.22, crashes 0.53 against 0.54, the pursuit's lock
  share 40% against 42%. Objectives flown fell from 75 to 48, but not around the evades, by the measure
  above.
- **A crash pattern, 3 of 78 evades.** Each began in a descent of -19 to -35 degrees at 1,200 to 1,600 m.
  The evade's afterburner and roll-right, with no pitch input, steepened it to -58 and -90 degrees while
  the emergency climb was refused 3 or 4 times (`climb suppressed — missile evade in progress`). Twice the
  aircraft hit before the evade's 6 s limit; once the climb started 0.8 s before the impact. The control arm
  had 1 crash within 10 s of its alert, the evade arm 3.

To see a 10-point gap with the usual 80% chance takes about 350 engagements an arm, some 175 missions at
this session's rate of 4 engagements a mission.

**V3, the verdict (measured, 2026-10-27): the share goes to 0.**

- **Without the flare roll** (account 3: the three trial sessions above, 137 missions), the evade arm leaned
  ahead without reaching significance: 189 of 299 (63%) against 159 of 285 (56%), z = 1.83. The D5 sessions
  alone: 132 of 217 (61%) against 112 of 207 (54%), z = 1.40.
- **With [ADR 164](164-roll-right-through-the-flares.md)'s flare roll in both arms** (account 2, 51
  missions), the evade arm survived clearly worse: 50 of 82 (61%) against 90 of 106 (85%), -24 points;
  z = -3.73, a 95% range of -36 to -11 points. Impact time agrees: 30% against 54%, z = -3.19. Missile and
  other kills ended 48 of 82 evade-arm engagements and 42 of 106 control ones; crashes, 3 and 1.
- The flare roll ships with no switch (ADR 164 D2), so the second comparison is the one that holds from now
  on. By V3's rule the share goes to 0 in `wingman/config.yaml`, and FR-006 is not extended. At 0 the pursuit
  draws no arm and starts no evade; the tree's evade in `GAME_BATTLE` and D5 are unchanged.
- What the evade adds to the roll is yaw-left and the hold until the alert clears (D1). Inferred, not
  measured: the turn spoils what the roll alone does. One session on one account; the sessions without the
  flare roll flew account 3.

## References

- [Research 019 — Shoot-downs and the Missile Defence](../research/019-shoot-downs-and-the-missile-defence.md)
- [ADR 070 — MISSILE_EVADE_MODE Behavior Tactic](070-missile-evade-tactic.md)
- [ADR 128 — Afterburner During an Incoming Alert](128-afterburner-during-an-incoming-alert.md)
- [ADR 148 — A Dive Recovery Flies Through a Pursuit](148-a-dive-recovery-flies-through-a-pursuit.md)
- [ADR 151 — One Actuator, and Leases for the Throttle](151-one-actuator-and-throttle-leases.md)
- `docs/architecture.md`, "Flight-input precedence"
