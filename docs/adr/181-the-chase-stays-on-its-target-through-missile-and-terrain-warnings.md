# ADR 181 — The chase stays on its target through missile and terrain warnings

| Status   | Date       | Wingman Version |
|----------|------------|-----------------|
| Draft    | 2026-11-15 | 1.9.4           |

## Decision

Operator, 2026-11-15: "I suspect that while in pursuit when incoming missile is
detected or terrain is detected, abandoning the pursuit to evade is costing more
score since turning away from target requires more time re-engage and it gives up
missile lock time and boresight. also the terrain evade's performance is not very
good so turning away may cause collision. update wingman so when pursuing a target
it stays on pursuit, regardless of terrain signal or incoming missile. once hldd017
is mature we'll re-enable advanced evade".

- **D1. The chase holds its target.** While a pursuit has had its target with the
  steering within `pursuit_mode.hold_target.view_s` (1.0 s), nothing takes the
  airframe from it:

  | What used to take it | Now, with the target in view |
  |---|---|
  | The flare roll on a missile alert ([ADR 164](164-roll-right-through-the-flares.md)) | Not started. One in progress ends when the target comes into view |
  | The missile evade inside a pursuit ([ADR 163](163-the-missile-evade-flies-through-a-pursuit.md)) | Not started. Its share was already 0 ([ADR 165](165-the-evade-trial-draws-its-arm-per-round.md)) |
  | The crash recovery when two terrain warnings agree (Code Review 019 cycle 25) | Not started |
  | The crash recovery on a severe pull-out alarm (Code Review 019 cycle 36) | Not started |
  | A crash recovery already flying ([ADR 148](148-a-dive-recovery-flies-through-a-pursuit.md)) | Handed back to the chase when the target comes into view, whatever the warnings say |
  | The terrain turn ([ADR 174](174-the-map-warning-starts-the-emergency-climb-in-half-the-rounds.md)) | Not started |

- **D2. Only a target counts.** A resupply marker, a control point or the crown
  with the steering keeps every protection it had. So does the search, with
  nothing in view.
- **D3. What still acts with the target in view.** The flares themselves and the
  jet's special weapon (a flare burst, a decoy) go out. The afterburner on an
  alert (FR-008), the arena-edge turn, stall prevention, and the airbrake rules
  (closing, approach and dive) are unchanged. None of them turns the aircraft
  off its target.
- **D4. Drawn per round.** `pursuit_mode.hold_target.share` is the share of rounds
  that hold. Each round's arm (`hold` or `evade`) is drawn at the round start,
  logged, and tagged `hold_target`. It ships at **1.0**, the operator's rule in
  every round. `0` turns the rule off.
- **D5. Every warning the rule sets aside is logged**, at most once in 5 s for
  each kind: `PURSUIT HOLD: <what> withheld — the chase stays on its target`.
- **D6. To be revisited.** When
  [Design 017](../hldd/017-terrain-map-from-flight-footage-hldd.md) gives a way
  out that can be trusted, an evade that keeps or quickly regains the target
  replaces this rule, in a new ADR.

This ADR changes parts of ADR 148, 163, 164 and 174 for the case of a target in
view. Those ADRs are not edited.

## Consequences

- **The chase keeps its lock.** Today a flare roll that starts with the target in
  view loses it almost every time ("Evidence").
- **More aircraft will be lost with a target in view**, to the ground and to
  missiles. That is the trade the operator chose. The recoveries that broke off
  a chase on 2026-11-15 were 97 in 14.7 hours.
- **At share 1.0 there is no control arm.** Round score has a standard deviation
  of about 200 and moves between sessions on unchanged code, so a before-and-after
  comparison will not settle whether the rule pays. Setting the share to 0.5 for
  a few sessions would: the arms are already tagged.
- **FR-011 is amended**: no flare roll while the pursuit holds its target. The roll
  still flies in every other case.
- The dive guard already stood aside for a visible target (operator, 2026-09-26).
  This rule extends the same idea to the rest.

## Evidence

Measured on the sixteen session logs of 2026-11-15 (14.7 hours, 157 rounds).
"In view" is a target tracked with a range reading (a `TARGETPROJ:` line) within
1 s. It undercounts: a tracked target whose range was not read is not counted.

| | Flare roll | Dive recovery |
|---|---|---|
| Times the chase yielded | 541 (37 an hour) | 414 (28 an hour) |
| Median length | 4.4 s | 3.0 s |
| Began with the target in view | 297 | 106 |
| of those, target kept in view throughout | 1 | 12 |
| of those, target back within 20 s | 151, a median 8.0 s after it was lost | 59, a median 9.3 s |
| of those, target not back within 20 s | 145 | 35 |

- The crash recovery started with the target in view 97 times: 76 on two
  agreeing warnings and 21 on a pull-out alarm. A crash followed within 15 s
  after 4 of them. How many crashes the other 93 prevented is not known.
- The missile evade inside a pursuit was already tried and closed (ADR 165). Over
  848 rounds its arm trailed on every per-round measure (score 382 against 398)
  and survived 64% of engagements against 69%.

One flare roll, target at 7.3 km when the roll began, next seen 8.5 s later:

```
13:21:59,515 [DEBUG] TARGETPROJ: at=(1136,514) marker=(1361,882) v=(-64,-138)px/s km=7.30
13:21:59,515 [INFO] Controller: pursue_and_engage — yielding pitch and roll to the flare roll (ADR 164)
13:22:01,060 [DEBUG] TARGETPROJ: at=(-296,1290) marker=(180,940) v=(-394,+383)px/s km=6.90
13:22:09,578 [DEBUG] TARGETPROJ: at=(1131,902) marker=(1717,922) v=(-655,-36)px/s km=9.10
```

One recovery that broke off a chase, 6.1 s:

```
13:31:20,194 [INFO] Controller: crash recovery starts with the target in view — two terrain warnings agree
13:31:20,447 [INFO] Controller: climb — hard emergency inside a pursuit: flying through GAME_BATTLE_EJECT and the chase yields (ADR 148, cap 30s)
13:31:26,523 [INFO] Controller: climb — crash no longer predicted after 6.1s (path +20 deg), handing the airframe back to the chase
```

## Implementation

- `wingman/pursuit_hold.py`: `PursuitHoldTrial`, the round's arm.
- `Controller.pursuit_holds_target()` is the one test. It is asked by
  `climb_mode` (at the start and while a recovery flies), `_flare_roll_refusal`,
  `missile_evade_mode` and the tree's terrain-turn start.
- `pursuit_mode.hold_target` in `wingman/config.yaml`; defaults in the schema
  (share 0, so a config without the block behaves as before).
- `tests/test_pursuit_hold.py`.

## Validation

- [ ] Live: `PURSUIT HOLD: round N — hold arm` at each round start.
- [ ] Live: `flare roll not started — the chase stays on its target (ADR 181)` on
      alerts with a target in view, and no `yielding pitch and roll to the flare
      roll` line that begins with a target in view.
- [ ] Live: `PURSUIT HOLD: crash recovery (...) withheld` lines, and no `crash
      recovery starts with the target in view` line in a hold round.
- [ ] Deaths per round by kill card (crash against shot down), against the
      2026-11-15 sessions.
- [ ] Score, kills and assists per round by `hold_target` arm, once the share
      leaves rounds to compare.
