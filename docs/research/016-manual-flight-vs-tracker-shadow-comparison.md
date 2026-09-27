# Research 016 — Manual Flight vs Tracker Shadow Comparison

| Status | Date       | Wingman Version |
|--------|------------|-----------------|
| Draft  | 2026-09-25 | 1.8.11          |

## Question

Can the operator's manual flying be used to tune pursuit-mode target tracking
(Design 005, Design 015)? For each tick of manual flight, compare the keys the
tracker *would* have pressed against the keys the operator *did* press, then
use the difference to fit the steering control law.

Scope is deliberately narrow: **roll and pitch steering toward a visible
target, with terrain assumed not to be a concern.** Terrain perception, target
selection policy and learned (neural) policies are out of scope. See "Why not
full behaviour cloning" below.

## Background

### Why not full behaviour cloning

Cloning the operator's actions from wingman's observations fails whenever the
operator reacts to something wingman cannot observe. Terrain is the main case.
Wingman's only terrain signal is the forward sky fraction in the
`TERRAIN_FORWARD` crop (`analyzer.detect_terrain_ahead`, Design 001, shadow).
That is a single scalar for a fixed box. It cannot say whether terrain lies
along the line to the target. If the operator breaks off a pursuit because of
a ridge, the observation is identical to a tick where they kept chasing, and a
cloned policy learns noise.

Restricting the comparison to steering on a visible target, and excluding
terrain-driven ticks, keeps the operator's decisions inside what wingman
observes.

### What already exists

| Piece | Where | State |
|-------|-------|-------|
| Tracker runs during manual flight (sensing only, no keys) | `tick_handlers.py` tracking handler `tick()`, gate on `GAME_BATTLE` and `GAME_BATTLE_MANUAL` | Working |
| Roll control law | `Controller.orient_nose_to_target` (`controller.py`) | Computes and presses in one call |
| Pitch control law | `Controller.orient_pitch_to_target` | Computes and presses in one call |
| Pitch "would fire" log | `PITCH[shadow]` INFO line, rate-limited 1st/10th/100th/every 500th | Too sparse for analysis |
| Operator key observation on the nested display | `input_linux.py` XRecord listener; i/j/k/l and arrows pass through as `TAKEOVER_KEYS` | **KeyPress only** (see below) |
| Session video and BT trace | `make rd v`, `session_recording.py` (Design 012) | Working |
| Per-tick trajectory record | Design 007 `TrajectoryWriter` | Designed, not built |

### Gap found: key releases are not observed

The XRecord context registers `device_events: (KeyPress, KeyPress)`
(`input_linux.py:797`), and the event loop skips anything that is not a
KeyPress (`input_linux.py:854`). Operator hold durations cannot be measured
until KeyRelease is added to the range. The takeover logic only needs presses,
so the release events must feed a separate recorder and never reach the
hotkey handler.

## Method

### Per-tick record

During `GAME_BATTLE_MANUAL`, write one JSONL line per main-loop tick:

- **Session and time:** session id (`PerformanceTracker.run_id`), tick index,
  monotonic timestamp.
- **Tracker observation:** mode, visible, `error_norm`, `error_norm_y`, the
  chosen cluster's position, and every candidate cluster found that tick.
- **Shadow command:** roll direction and hold seconds, pitch direction and
  hold seconds, computed by the same control law with the current
  `tracking` config, without pressing anything.
- **Operator input:** net roll-left and roll-right seconds, and net nose-up and
  nose-down seconds, held within the tick window. The raw press and release
  events are kept at full timing resolution.
- **Context:** altitude, altitude rate, speed, forward sky fraction, health,
  incoming.

### Required code changes (none actuate)

1. Extract the direction and hold calculation from `orient_nose_to_target` and
   `orient_pitch_to_target` into a pure function. The controller calls it and
   its live behaviour is unchanged. The recorder calls it too.
2. Extend the XRecord range to include KeyRelease and route press and release
   events for flight keys to an operator-input recorder while in
   `GAME_BATTLE_MANUAL`. The hotkey path stays press-only.
3. Add a comparison writer, config-gated and off by default, writing under
   `logs/` with the session id.
4. Add an analysis script and a make target that produce the report below.

### Exclusions

Drop a tick from the comparison when any of these holds. Thresholds are to be
set from the first sessions' data, not guessed here.

- The target is not visible, or the tracker is not in `TRACKING` mode.
- Terrain is plausibly driving the input: low altitude with a falling altitude
  rate, a low forward sky fraction, or a nose-up input while the target is
  below the nose.
- The operator is defensive: incoming alert active, or flares deployed.

### Target attribution

If the operator chases a different enemy than the tracker picked, every such
tick scores as a disagreement for the wrong reason. Attribute the chased
target as the candidate cluster whose error shrinks over the following ticks
under the operator's input. Ticks where that is not the tracker's pick are
reported separately and not counted against the control law.

## Measures

| Measure | Parameter it informs |
|---------|----------------------|
| Direction agreement per axis (left, right, none), as a confusion matrix | Sign errors, `deadband` |
| Operator net hold seconds against error, fitted per axis | `kp`, `min_hold_sec`, `max_hold_sec` |
| Operator hold style: continuous holds against taps | `sustained_hold` |
| Latency from first visible target to first operator input, against the tracker's | Cooldown, and whether the 1.5 s tick is too slow |
| Error trend in the ticks after operator input | Whether the tracker's aim point matches the operator's |
| Share of ticks where the operator chased a different target | Target selection, a separate follow-up |

## Limits

- **Open loop.** The operator's inputs decide the next state, so the tracker's
  counterfactual outcome is never observed. Agreement is evidence for tuning,
  not proof of better pursuit. Validate any parameter change in live pursuit
  runs.
- **Resolution mismatch.** The operator steers continuously. The tracker
  decides once per tick. Compare net held seconds per tick, and keep the raw
  events for any later faster-tick study.
- **The operator is not the ground truth.** Disagreement may mean the operator
  is leading the target or managing energy. Read large disagreements in the
  session video (Design 012) before fitting through them.

## Data Collection Protocol

- Fly deliberate pursuits in manual, with a target visible, at safe altitude.
- Use `make rd v` so each session has video for spot checks.
- Aim for several sessions across at least two maps before fitting.

## Results

None yet.

| Session | Map | Manual ticks | Ticks kept | Roll agreement | Pitch agreement | Notes |
|---------|-----|--------------|------------|----------------|-----------------|-------|

## Open Questions

1. Does the operator mostly hold or mostly tap? The answer decides whether the
   fitted `kp` is meaningful or `sustained_hold` is the real lever.
2. Is the 1.5 s tick the dominant disagreement? If operator latency is far
   lower, the finding is about cadence, not gains.
3. How often does the operator pick a different target from the tracker's
   nearest-to-centre rule?
4. Is the forward sky fraction good enough to exclude terrain-driven ticks, or
   does the exclusion need hand review from video?

## Next Steps

1. Write an ADR for the code changes above. The next free number was 150 when
   this was written; recheck before creating it.
2. Implement changes 1 and 2 together, then 3 and 4.
3. Collect sessions and fill in Results.

## References

- Design 001 — Terrain Avoidance (`docs/hldd/001-terrain-avoidance-hldd.md`)
- Design 005 — Target Tracking (`docs/hldd/005-target-tracking-hldd.md`)
- Design 007 — Telemetry and Data Collection (`docs/hldd/007-telemetry-data-collection-hldd.md`)
- Design 012 — Session Recording and BT Trace (`docs/hldd/012-session-recording-and-bt-trace-hldd.md`)
- Design 015 — Target-Tracking Pursuit Mode (`docs/hldd/015-target-tracking-pursuit-mode-hldd.md`)
- ADR 099 — Nested Display Lane
- ADR 142 — Gate Terrain-Ahead on Padlock State
