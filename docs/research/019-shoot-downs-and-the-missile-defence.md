# Research 019 — Shoot-downs and the Missile Defence

| Status | Date       | Wingman Version |
|--------|------------|-----------------|
| Draft  | 2026-10-26 | 1.9.3           |

## Question

What happens in the seconds before wingman is shot down, and is there a wingman-side failure in its defence
to fix? This is the review [Action Item 004](../action-item/004-terrain-avoidance-levers-after-the-camera-study.md)
leaves open as AI-004-5. Operator, 2026-10-26: "whats next", then `/proceed` on the recommendation to review
the shoot-downs offline, after [Research 018](018-sky-and-terrain-from-flown-paths.md) found that neither of
its picture cues could lift the terrain warning above the pull-out trigger.

## Answer

Shoot-downs outnumber crashes: 93 to 13 in the recorded session, 417 to 88 among the labelled black-box
deaths; 559 shoot-downs in the 193 missions reviewed, 2.9 a mission. Wingman sees the missile and answers at
once, but with flares and the afterburner only. The manoeuvre that measured
best when it was introduced, [ADR 070](../adr/070-missile-evade-tactic.md)'s missile evade, is locked out of
the pursuit, and the pursuit is where nearly every shoot-down now happens (measured unless marked):

- **The alert is seen.** 434 of 447 missile kills (97%) had a missile alert within 10 s of the impact, a
  median 3.9 s before it.
- **Flares and the afterburner fire every time.** Flares after the alert in all 434, the first press a median
  0.02 s after it; the afterburner evade ([ADR 128](../adr/128-afterburner-during-an-incoming-alert.md)) in
  430. Flares had run out before 10 of 418 (2%).
- **The evade manoeuvre almost never runs.** It was selected 11 times in 193 missions, all outside the
  pursuit, and ran in 4 of the 434 alerted missile kills.
- **About a third of missile engagements end in a death.** Wingman's own session summaries (ADR 070's
  `missile_engagements`): 501 of 792 engagements without the evade survived 10 s (63%), and 3 of 3 with it.
  ADR 070's soak of 2026-08-14 measured 90% with the evade and 68% without (54 missions, 122 engagements;
  observational, its own caveat below).
- **Why the evade cannot run in a pursuit (from the code).** The pursuit runs in `GAME_BATTLE_EJECT`. Idle is
  first in the tactic order, and `make_idle_condition` (`wingman/behavior_tree.py`) holds every state but
  `GAME_BATTLE`, stepping aside in `GAME_BATTLE_EJECT` only for Climb's emergency verdict. The missile-evade
  condition yields to that same verdict, so in `GAME_BATTLE_EJECT` the selector settles on Idle or Climb,
  never on MissileEvade. The rule's docstring says this is meant ("ordinary tactics get no new access to
  GAME_BATTLE_EJECT"), and describes that state as the eject dive. Inferred: the rule was written for a short
  dive, and its reach grew when the whole fight moved into that state.

Whether the evade helps inside a pursuit is not known. ADR 070's arms were measured in August, outside the
pursuit, and observationally: its no-evade engagements clustered in eject and respawn states. Running it in a
pursuit also breaks the chase. So the change it points to, letting the evade interrupt the pursuit, is the
operator's decision, and its effect has to be measured live.

## Data and method

- **Logs.** The recorded session of 2026-10-24 18:18 to 21:20 (`logs/wingman_20261024_212050.log`, 32
  missions), and the eight logs behind the labelled black-box deaths of Research 017
  (`test_screenshots/terrain_dataset/labels.csv`: 2026-10-23 23:49 to 2026-10-24 15:10, 161 missions). Not
  every death in those logs was labelled, so all shoot-downs in them are counted. The code changed across
  them; the defence behaved the same in both sets (flares and the afterburner evade after 99% or more of the
  alerted missile kills).
- **Tool.** `test_screenshots/terrain_dataset/tools/shootdown_review.py`, one row a shoot-down in
  `test_screenshots/shootdown_review/<set>/shootdowns.csv`. Deaths, their impact times and crash or
  shoot-down come from the dataset tools' `score_video.scan`. The impact is the last HUD reading plus 0.75 s,
  so leads are good to about a second.
- **Alerts.** `INCOMING MISSILE DETECTED (source=...` lines. An alert episode is alerts no more than 5 s
  apart; a shoot-down is alerted when an episode's last alert is within 10 s of the impact, and its lead is
  the time from that episode's first alert to the impact. Wingman does not read the game's LOCK WARNING or
  GUNS WARNING (no log line).
- **Weapons.** From the kill card (`DEATH CARD: killed by ... with ...`), OCR'd, sorted by the weapon's
  real-world guidance: gun, rocket, heat-seeking missile, radar missile, unclear (R.530, R-40). Whether the
  game's flares decoy radar missiles is not known here.
- **Engagements.** Taken as wingman's own session summary reports them (`MissionStatsTracker`: one
  engagement a volley, alerts within 3 s; died when a respawn lands within 10 s). `wingman_live_1510.log` is a
  copy taken mid-session and has no summary.

## Results

| | Recorded session | Black-box logs | All |
|---|---|---|---|
| Missions | 32 | 161 | 193 |
| Shoot-downs (a mission) | 93 (2.91) | 466 (2.89) | 559 (2.90) |
| Crashes | 13 | not counted | |
| Radar missile / heat missile / unclear / gun / rocket | 68 / 10 / 0 / 15 / 0 | 303 / 58 / 8 / 95 / 2 | 371 / 68 / 8 / 110 / 2 |
| Missile kills alerted within 10 s | 76 of 78 | 358 of 369 | 434 of 447 (97%) |
| Alert lead, median (quartiles) | 3.8 s (3.7 to 5.3) | 3.9 s (2.5 to 5.4) | |
| Flares after the alert | 76 of 76, 0.02 s | 358 of 358, 0.02 s | 100% |
| Afterburner evade after the alert | 75 of 76 | 355 of 358 | 430 of 434 (99%) |
| Missile-evade tactic | 0 of 76 | 4 of 358 | 4 of 434 (1%) |
| Out of flares | 2 of 74 | 8 of 344 | 10 of 418 (2%) |
| In the pursuit (`GAME_BATTLE_EJECT`) | 93 of 93 | 463 of 466 | 556 of 559 |
| Missiles still aboard (last count read in that life) | 68 of 81 | 303 of 419 | 371 of 500 (74%) |

Missile engagements from the session summaries:

| Log | Engagements | Survived 10 s without the evade | With the evade |
|---|---|---|---|
| 2026-10-23 23:49 | 214 | 125 of 213 (59%) | 1 of 1 |
| 2026-10-24 02:00 | 77 | 47 of 77 (61%) | none |
| 2026-10-24 02:14 | 9 | 4 of 9 | none |
| 2026-10-24 03:21 | 47 | 29 of 47 (62%) | none |
| 2026-10-24 06:14 | 98 | 68 of 98 (69%) | none |
| 2026-10-24 08:20 | 6 | 6 of 6 | none |
| 2026-10-24 13:38 | 213 | 148 of 211 (70%) | 2 of 2 |
| 2026-10-24 21:20 (recorded) | 131 | 74 of 131 (56%) | none |
| All | 795 | 501 of 792 (63%) | 3 of 3 |

Also seen:

- **Guns kill one shoot-down in five** (110 of 559). Wingman has no warning for them. 47 of the 95 in the
  black-box logs had a missile alert within 10 s, from another attacker.
- **The defence has about four seconds.** Three quarters of the alerted missile kills came 2.5 s or more
  after the first alert, so the evade, if it ran, would have time to act.

## Limits

- The evade's effect inside a pursuit is unmeasured: ADR 070's 90% and 68% come from another context, and
  the 3 engagements with an evade here are too few for anything.
- Engagement survival is a 10 s window: an engagement survived and a death 12 s later count as a success.
- Weapon types are real-world guidance, not the game's model.
- The black-box logs' code varied across the two days (ADR 151 D6 landed on 2026-10-24); the defence path
  did not change.

## Open Questions

- Does the evade raise engagement survival inside the pursuit, and what does it cost the chase (lock share,
  objectives a mission)?
- How long does the game's LOCK WARNING lead the INCOMING alert? Reading it could start the defence earlier
  than the 3.9 s the alert leaves.
- Do flares decoy radar missiles in this game? Two kills in three are by radar missiles.

## Next Steps

1. The operator decides whether the missile evade may interrupt the pursuit. Decided 2026-10-26: yes, as a
   trial, in [ADR 163](../adr/163-the-missile-evade-flies-through-a-pursuit.md).
2. If so, a new ADR for the rule (ADR 070 is Accepted and stays as it is): let MissileEvade past Idle in
   `GAME_BATTLE_EJECT` during an alert, with the pursuit yielding its steering keys to it through
   [ADR 151](../adr/151-one-actuator-and-throttle-leases.md)'s leases. Then a live comparison, evade in the
   pursuit on against off, over 40 missions or more, judged on engagement survival and the lock share.
3. On the recorded video: the LOCK WARNING's lead over the INCOMING alert.

## References

- [Action Item 004 — Terrain Avoidance: the Levers Left After the Camera Study](../action-item/004-terrain-avoidance-levers-after-the-camera-study.md), AI-004-5
- [ADR 070 — MISSILE_EVADE_MODE Behavior Tactic](../adr/070-missile-evade-tactic.md), V5
- [ADR 128 — Afterburner During an Incoming Alert](../adr/128-afterburner-during-an-incoming-alert.md)
- [ADR 151 — One Actuator, and Leases for the Throttle](../adr/151-one-actuator-and-throttle-leases.md)
- [Research 017 — Camera Vision for Terrain Avoidance](017-camera-vision-for-terrain-avoidance.md), "Step 3 results"
- [Research 018 — Sky and Terrain from the Paths Actually Flown](018-sky-and-terrain-from-flown-paths.md)
