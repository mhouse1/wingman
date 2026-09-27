# Design 013 — Center-Seeking Fallback Steering During mission_j20

| Status | Date       | Wingman Version |
|--------|------------|-----------------|
| Draft  | 2026-09-21 | 1.8.11          |

## The problem, measured

`mission_j20` is "fully adaptive" (ADR 075) — the mission thread only keeps
the search-and-destroy loops running; the behavior tree owns every in-battle
steering decision (`wingman/controller.py:mission_j20` docstring). Horizontal
steering during combat comes from `EngageNavigator` (`wingman/engage_nav.py`),
whose own docstring states the implicit theory this document challenges:

> "Continuously steering toward detected enemies is what keeps the aircraft
> inside the battle arena — enemies only render inside it."

That theory has a measured hole, in the same file's own comment: enemy
contacts render on only **43% of battle ticks** (11,383-tick session). The
other **57%** issue no steering command at all, and the aircraft holds
whatever heading it already had — "which is how it reaches the map edge"
(`EngageNavigator.update`, ADR 028 revision 4 comment). Regroup (steer
toward the friendly-icon centroid when idle) was built to give some of that
57% a command instead of none, but it answers "where is the fight," not
"where is safe" — friendly icons cluster wherever the battle is, which is
not guaranteed to be near the arena's own center, and Regroup is off by
default (`regroup_enabled: false`).

**The tactic that actually owns this gap already exists and is already a
no-op.** `TACTIC_ATTACK_SUPPORT` sits at the bottom of `_PRIORITY_ORDER`
(`wingman/behavior_tree.py`) with an unconditional `always` selection
condition — it is what the tree selects when nothing else (Idle,
RespawnWait, Eject, MissileEvade, BoundaryTurn, Evade, Disengage, Climb,
Engage, Regroup) applies. `BehaviorTreeHandler.tick()`
(`wingman/tick_handlers.py`) actuates `TACTIC_ENGAGE`/`TACTIC_REGROUP` via
`_actuate_engage` and `TACTIC_CLIMB` via the same method in `steer_only`
mode — there is no corresponding branch for `TACTIC_ATTACK_SUPPORT` at all.
Selecting it presses no key. This was already found and named, not
guessed: ADR 137's Fourth Live Trial section documents "the `AttackSupport`
fallback tactic having no enemy *or* friendly icon to steer toward right
after a fresh respawn... issued no control input at all" as the explanation
for an aircraft observed flying straight after respawn.

**The sensing this design needs already exists and was substantially
hardened the same week this document was written.** `detect_map_boundary`
(`wingman/analyzer.py`) returns `(dist, forward, lateral)` — the nearest
boundary point's distance and bearing, in units of the minimap radius,
already median-filtered (ADR 113) and corroborated against the
out-of-bounds void for short fragments (ADR 133). `BoundaryPerceptionHandler
.perceive()` (`wingman/tick_handlers.py`) computes this every tick
regardless of which tactic is selected — `TACTIC_ATTACK_SUPPORT`'s selection
does not prevent the reading from existing, it only prevents anything from
acting on it. ADR 117 D8-D11 (2026-09-20/21) spent one session finding and
closing four distinct false-positive sources in the *diagnostic* capture
this reading feeds (compass-rim decoration, terrain color, a compass letter,
a UI marker icon, a flight-path trail) — the same underlying reading this
design would consume for steering is the one that work hardened.

## What "center" means here, and what it doesn't

The minimap is egocentric — the aircraft is drawn at the crop's own center
by the game itself, every tick, regardless of where it actually is on the
arena. There is no signal anywhere in this codebase for "absolute distance
from the arena's true center." What `detect_map_boundary` gives instead is
local and directional: **nearest edge, and which way it is.** Steering away
from that bearing moves the aircraft toward the interior for exactly as long
as that remains the nearest edge — it is not a claim about reaching any
particular point, and this document does not propose computing one.
`BoundaryTurn` (ADR 107/108/122/133/141) already reacts to the same signal
when it gets close and urgent; this design is the earlier, gentler,
lower-priority use of the identical reading, active only when nothing else
is happening at all.

## Why not other approaches

- **Widen `BoundaryTurn`'s trigger radius (`turn_frac`) instead.** Rejected:
  `BoundaryTurn` is a hard emergency-adjacent maneuver — ADR 107 D4's own
  reasoning is "hitting the ground is certain, the boundary is a countdown."
  Moving its trigger outward makes *more* ticks urgent; it does not add a
  gentle background bias for the ticks that are not urgent yet, and a wider
  band was already flagged (ADR 137 D4/Non-Goal) as a tradeoff against more
  false-urgent turns during ordinary combat dives.
- **Always-on Regroup.** Rejected as a substitute, not as a companion:
  Regroup answers a different question (where the fight is) and both this
  design and Regroup only apply in the same idle window in practice
  (`TACTIC_REGROUP` outranks `TACTIC_ATTACK_SUPPORT`), so Regroup already
  wins whenever friendlies are visible — this design only ever fires in the
  window Regroup itself leaves empty.
- **A new dedicated tactic (e.g. `TACTIC_SEEK_CENTER`) inserted into
  `_PRIORITY_ORDER`.** Considered, not chosen for Phase 1. It would need a
  new priority slot, a new condition function, and a new actuator wiring
  branch — three moving parts — to reach exactly the same tick window
  `TACTIC_ATTACK_SUPPORT`'s existing, unconditional, currently-empty slot
  already owns. Revisit only if `AttackSupport`'s name/semantics are
  claimed by a real, different future feature (see Phase 2+).

## Phase 1 — wire the existing fallback slot to the existing boundary reading

### Detection: nothing new

`BoundaryPerceptionHandler.reading` (`wingman/tick_handlers.py`) already
holds this tick's `(dist, forward[, lateral])`, or `None`. No new capture,
no new config-gated detector, no new false-positive surface to characterize
— this reuses the exact signal `BoundaryTurn` and the ADR 117 D8-D11
diagnostic already consume today.

### Actuation: roll-only, through the existing primitive, gated to true idle

Add an `elif _may_fly and not snap.survival_hold and selection ==
TACTIC_ATTACK_SUPPORT:` branch beside the existing
`TACTIC_ENGAGE`/`TACTIC_REGROUP`/`TACTIC_CLIMB` branches in
`BehaviorTreeHandler.tick()`, calling a new `_actuate_seek_center` method.
Uses the `_b_dist, _b_fwd, _b_lat` locals `tick()` already destructures from
`self._boundary.perceive(...)` earlier in the same call — not a fresh
`self._boundary.reading` property fetch, which would redo the None/2-tuple
handling `perceive()` already resolved for this tick.

**The survival-hold exclusion is not optional here — every existing
roll-axis consumer already has it, one of them because a live bug required
it.** `TACTIC_ATTACK_SUPPORT` is lower priority than `TACTIC_CLIMB`, and the
`TACTIC_CLIMB` branch's own comment documents exactly this failure mode
already occurring live: "9 EngageNav commands reached a loitering aircraft
on 2026-09-04 15:31 because loiter climbs, so Climb is the selection for
most of a hold" (ADR 110). During a hold where altitude is already stable
and no contacts are visible — a large fraction of any real hold —
`TACTIC_ATTACK_SUPPORT`'s `always` condition would win by elimination the
same way `TACTIC_CLIMB` did before that fix. Using bare `_may_fly` here
(matching only `TACTIC_CLIMB`'s outer condition, not its own inner `if not
snap.survival_hold:` guard) would silently reopen that exact bug — a second,
uncoordinated writer on the roll axis the hold's own orbit logic already
owns. `not snap.survival_hold` belongs in this branch's own condition, not
as a separate inner check, since (unlike Climb) this branch has no other
reason to run during a hold at all.

On a tick where:

1. `_b_lat is not None` (a 2-tuple reading — `lateral is None` — is treated
   exactly like no reading at all: skip the tick. This is already a rare,
   degenerate case per ADR 122's own comment, and doing nothing is the safe
   default rather than importing `BoundaryTurn`'s fixed-direction fallback
   into a second consumer), and
2. `_b_dist <= seek_center_trigger_frac` (a new, deliberately
   *wider-than-BoundaryTurn* config threshold — see Config below),

compute the reciprocal steering error directly in vector space — negate
both components, then `atan2`, rather than computing a bearing and adding
180° with manual wrap-around handling (the same reason `MinimapEma` smooths
in `(x, y)` space rather than on the angle: "a bearing cannot be averaged
across the ±180° wrap"):

```python
error_norm = max(-1.0, min(1.0,
    math.degrees(math.atan2(-_b_lat, -_b_fwd)) / 90.0))
```

**Rear-sector commitment, reused, not reinvented.** The reciprocal bearing
sits near ±180° exactly when the aircraft is already heading toward the
edge — the single most important case for this feature to handle well, and
the exact unstable-sign condition `EngageNavigator._steer_intent`'s
`rear_commit_deg`/`rear_release_deg` commitment already exists to solve
(discovered live, 2026-08-08: flipping roll direction every sample never
brings a rear-sector target forward). `_actuate_seek_center` carries its own
instance of the same commit/release state machine — copied, not shared with
`EngageNavigator`'s, since the two must not reset each other on an
unrelated mode switch.

**Own tuning, not `self._ctl_cfg`.** `self._ctl_cfg["deadband"]` is
hardcoded to `self._nav.deadband_norm` (`EngageNavigator`'s own combat-tuned
12° deadzone) — splatting `**self._ctl_cfg` into `orient_nose_to_target`
would silently ignore a new `seek_center_deadzone_deg` config value
entirely. `_actuate_seek_center` builds its own config dict
(`seek_center_deadzone_deg`, `seek_center_kp`, `seek_center_min_hold_s`,
`seek_center_max_hold_s`, `seek_center_cooldown_s` — see Config) and passes
that to `orient_nose_to_target` instead. This is a deliberately gentler,
independently-tunable maneuver, not combat steering wearing a different
name.

**No new temporal smoothing in Phase 1.** `EngageNavigator` smooths twice
(ADR 113's median filter upstream, then its own `MinimapEma` over time) —
whether the median filter alone is enough for a steering consumer, as
opposed to `BoundaryTurn`'s threshold-crossing consumer which cares less
about smoothness, is unmeasured. Phase 1 adds no second smoothing layer;
the shadow log (below) already records the commanded direction every
qualifying tick, which is exactly the evidence needed to decide whether an
EMA is warranted before spending the effort — matching how ADR 117 D8-D11
measured each heuristic before adding it, this same week. See Open Question
4.

**Shadow first.** Matching this codebase's standing convention (ADR 070's
missile-evade shadow trial, HLDD 001 Phase 1's terrain-ahead shadow, ADR 028
revision 4's own "off unless config turns it on" framing for Regroup): Phase
1 ships with `seek_center_enabled: false` and, while disabled, logs what it
*would* command — rate-limited the same way every other diagnostic added
this week is (ADR 117 D9's `_blind_no_boundary_line_skips` pattern: log at
the 1st, 10th, 100th occurrence, then every 500th) rather than once per
qualifying tick, which could otherwise log continuously for as long as the
aircraft sits idle near an edge:

```python
logger.info("SEEK CENTER[shadow]: would roll err=%.2f dist=%.2f (%d so far)",
            error_norm, _b_dist, self._seek_center_shadow_count)
```

A live session's shadow log is what decides whether Phase 1 goes live, not
a guess.

### Priority and safety — one gate to add, confirmed by precedent

`TACTIC_ATTACK_SUPPORT` is already last in `_PRIORITY_ORDER`. Every existing
higher-priority tactic — most importantly `TACTIC_BOUNDARY_TURN` itself,
`TACTIC_MISSILE_EVADE`, `TACTIC_EJECT`, `TACTIC_CLIMB` — continues to
pre-empt this one exactly as it pre-empts Engage and Regroup today. This
design adds no new priority comparison and cannot introduce a conflict
`_PRIORITY_ORDER` does not already resolve.

The survival-hold exclusion, however, is a real gate this branch must carry
itself — not something it inherits for free. `TACTIC_ENGAGE`/`TACTIC_REGROUP`
get it via `_combat_ok = _may_fly and not snap.survival_hold`;
`TACTIC_CLIMB` gets it via its own explicit inner check, added after a live
incident (ADR 110, 2026-09-04: "9 EngageNav commands reached a loitering
aircraft... because loiter climbs"). `TACTIC_ATTACK_SUPPORT` needs the same
treatment for the same reason — see Actuation above for why bare `_may_fly`
would reopen that exact bug for a lower-priority tactic than the one it was
first found on.

### Config

Proposed, under a new `behavior_tree.attack_support` block (mirroring
`behavior_tree.climb.terrain_avoidance`'s pattern of nesting a sub-feature
under its owning tactic). Own gain/hold-time knobs, independent of
`j20_mission`'s `coarse_kp`/`coarse_min_hold_s`/etc. — see Actuation above
for why this must not resolve to `self._ctl_cfg`'s combat tuning:

```yaml
behavior_tree:
  attack_support:
    seek_center_enabled: false       # Phase 1 default — shadow only
    # Deliberately wider than boundary_near_frac (0.35, BoundaryTurn's own
    # trigger) — this is the gentle, early bias; BoundaryTurn stays the
    # last-resort emergency layer with its own, tighter threshold.
    seek_center_trigger_frac: 0.55
    seek_center_deadzone_deg: 15.0
    # Deliberately gentler than j20_mission's coarse_kp (0.5) / hold-time
    # defaults — a background bias should correct less aggressively than
    # active target tracking. Starting values, not measured; the shadow
    # trial validates the trigger and direction, not roll aggressiveness
    # (Phase 1 never actuates), so these three specifically stay a named
    # guess into the live trial.
    seek_center_kp: 0.3
    seek_center_min_hold_s: 0.15
    seek_center_max_hold_s: 0.6
    seek_center_cooldown_s: 2.0
```

`seek_center_trigger_frac` starting above `boundary_near_frac` (0.35) is a
deliberate, named guess — Phase 1's shadow log is what should confirm or
correct it, not this document.

`config_schema.py` additions (no new `Leaf` patterns — every type below
already exists for a sibling key elsewhere in the schema):

```python
"attack_support": Section(children={
    "seek_center_enabled": BOOL,
    "seek_center_trigger_frac": FRACTION,       # matches boundary_near_frac
    "seek_center_deadzone_deg": _num(0, 180),   # matches bearing_deadzone_deg's ACTUAL range
    "seek_center_kp": _num(0),
    "seek_center_min_hold_s": SECONDS,
    "seek_center_max_hold_s": SECONDS,
    "seek_center_cooldown_s": SECONDS,
}),
```

(As shipped: `bearing_deadzone_deg` itself is `_num(0, 180)` in `config_schema.py`,
not the 90 this document originally guessed — corrected to actually match the
sibling key it cites.)

### Testing plan

- Unit: a pure-logic test analogous to `EngageNavigator`'s own tests —
  given a boundary reading and existing selection state, does the new branch
  compute the correct reciprocal `error_norm` (vector-negate-then-`atan2`,
  not bearing-plus-180), respect its own deadzone/gain (not
  `self._ctl_cfg`'s), and leave every other tactic's actuation untouched?
  No frames, no analyzer, same style as `tests/test_engage_nav.py`.
- Unit: `lateral is None` (a 2-tuple reading) is treated as no reading at
  all — the branch must not call `atan2(None, ...)` or otherwise raise.
- Unit: the rear-sector commit/release state machine, ported directly from
  `EngageNavigator._steer_intent`'s own test shape — a reciprocal bearing
  crossing `rear_commit_deg` latches a sign, a later read below
  `rear_release_deg` releases it, matching the existing tests for the
  mechanism this reuses.
- Unit: confirm the new branch never fires when `selection !=
  TACTIC_ATTACK_SUPPORT`, when `_may_fly` is false, or when the boundary
  reading is `None` — mirrors the existing gate tests already covering
  Engage/Regroup/Climb.
- Unit: confirm the new branch never fires when `snap.survival_hold` is
  true, even when `selection == TACTIC_ATTACK_SUPPORT` and a triggering
  boundary reading is present — this is the specific regression class ADR
  110 already fixed once for `TACTIC_CLIMB`; the test should exist for the
  same reason that fix has its own test coverage.
- Shadow live trial (required before `seek_center_enabled: true` ships):
  one full session, `seek_center_enabled: false`, reading the rate-limited
  `SEEK CENTER[shadow]` log line count and the commanded direction
  directly — not `BoundaryPerceptionHandler._boundary_trace`, which is a
  20-tick (~30s) rolling lookback window built for context around one
  specific event, not session-long aggregation. Cross-check the logged
  direction against the same session's `BOUNDARY: dist=...` lines for
  sanity, and use this same log to answer Open Question 4 (temporal
  smoothing) before deciding whether it needs an answer.
  **Run 2026-09-21 — see Shadow trial results below.**
- Live trial (after shadow validates): compare `total_rtb_with_missiles`,
  confirmed-crossing rate, and `BoundaryTurn` trigger frequency
  session-over-session with the feature on vs. off — this design's success
  criterion is fewer ticks reaching `BoundaryTurn`'s emergency threshold at
  all, not a new metric of its own. **Blocked on the shadow trial actually
  firing — see below.**

### Shadow trial results (2026-09-21)

Phase 1 shipped 2026-09-21 (`seek_center_enabled: false`) and was checked
against six sessions the same day (04:32-09:11, roughly 4.5h combined
play). The actuation-side code is confirmed safe: the new branch executed
on every one of 70 ticks across 13 separate `TACTIC_ATTACK_SUPPORT`
selection windows, with zero crashes and zero spurious actuation,
correctly no-op'ing whenever `_b_lat` was `None`. But `SEEK
CENTER[shadow]` — the line that means "a qualifying boundary reading
existed and the branch computed a command" — fired **zero times** across
all six sessions.

That is not a small-sample gap. Cross-referencing the boundary detector's
own instrumentation (`MAP BOUNDARY: ahead at %.2fR (approach N this
session)`, from `instrument_boundary`, which runs unconditionally every
tick regardless of the selected tactic) against what was actually selected
at each of those moments shows the near-boundary condition itself is
common — 44-54 occurrences in a single session — but a *higher-priority*
tactic claimed the tick every single time: mostly `BoundaryTurn` and
`TACTIC_CLIMB`, with the remainder outside `_may_fly` entirely
(`mission_running=False`, e.g. a killcam/spectate minimap). Zero of
roughly 120 real near-boundary observations across the six sessions
coincided with `TACTIC_ATTACK_SUPPORT` specifically. Open Question 1's
original "readability ceiling" framing undersold the actual bottleneck —
the reading exists often enough; it is the *tactic-selection* coincidence
that turns out to be rare.

Because the live gate has not fired even once, the reciprocal-bearing
formula itself was validated offline instead, against real sensor data
rather than only the synthetic vectors in `tests/test_tick_handlers.py`:
`detect_map_boundary` was re-run against the 10 archived `approach_*.png`
frames from one session (`test_screenshots/unknown_anomalies/`, each
already inside `boundary_near_frac` at capture time — ADR 108's approach
capture, unconditional on tactic), and each recovered `(dist, forward,
lateral)` was fed through the exact reciprocal-`atan2` and rear-commit
formula this design specifies. All 10 produced a direction-correct escape
command (steer away from the recovered edge bearing), and the rear-commit
state machine correctly held its prior sign on the one case whose raw
bearing would otherwise have flipped it — the anti-thrash behavior working
as designed, on real captured frames, independent of the live gate ever
having exercised it.

**Conclusion:** the code is correct and safe, and the formula is
directionally sound on real data. The design's trigger condition — fire
only when `TACTIC_ATTACK_SUPPORT` is selected — is too narrow to exercise
under this operator's normal play. Open Question 2 (extend to
`TACTIC_CLIMB`) is now the load-bearing next step, not a deferred
nice-to-have; see below.

## Phase 2+ (not designed here, explicitly deferred)

- **Blending with Regroup instead of strict priority exclusion.** Today
  Regroup and this design can never both apply on the same tick (Regroup
  outranks AttackSupport), so a friendly cluster near the edge is chased
  with no center bias at all until it's BoundaryTurn's problem. Worth
  revisiting once Phase 1 has live data on how often that actually happens.
- **A genuine "distance from center" estimate**, e.g. integrating the
  boundary bearing over time to build a rough occupancy sense of the arena
  interior, rather than reacting to the single nearest edge point each tick.
  Meaningfully more machinery (state carried across ticks, more failure
  modes to characterize) for a benefit not yet shown to be needed — Phase 1
  reacting to the nearest edge, the same primitive `BoundaryTurn` already
  trusts, is the minimal thing to try first.
- **Promoting this to a dedicated `TACTIC_SEEK_CENTER` slot**, if
  `AttackSupport` ever grows a real, distinct meaning of its own that this
  feature would then be squatting on.

## Integration Points (Phase 1)

- `wingman/tick_handlers.py` — `BehaviorTreeHandler.tick()` (new `elif`
  branch beside the existing `TACTIC_ENGAGE`/`TACTIC_REGROUP`/`TACTIC_CLIMB`
  actuation dispatch, calling a new `_actuate_seek_center` method), reading
  the `_b_dist`/`_b_fwd`/`_b_lat` locals `tick()` already destructures from
  `self._boundary.perceive(...)` earlier in the same call — not a fresh
  `self._boundary.reading` property fetch (see Detection/Actuation above).
- `wingman/controller.py` — `orient_nose_to_target` (existing, unmodified;
  same call signature `_actuate_engage` already uses).
- `wingman/config.yaml` / `wingman/config_schema.py` — new
  `behavior_tree.attack_support` section.
- No changes to `wingman/behavior_tree.py`'s tree structure, priority order,
  or `TACTIC_ATTACK_SUPPORT`'s selection condition (`always`, unchanged) —
  this is an actuation-side addition only.

## Open Questions

1. **Readability ceiling — measured, and not actually the bottleneck.**
   ADR 117 measured in-battle boundary readability at 56%; ADR 133's
   corroborated-span fix and D8-D11 shape checks improve the diagnostic's
   precision but do not by themselves raise how often a reading exists at
   all. **Resolved differently than expected by the 2026-09-21 shadow trial
   (see Shadow trial results above): readability was never the limiting
   factor** — 44-54 real readings occurred in a single session alone. The
   actual bottleneck is that those readings consistently coincide with a
   higher-priority tactic (`BoundaryTurn`, `TACTIC_CLIMB`) rather than with
   `TACTIC_ATTACK_SUPPORT`'s own selection window — confirmed directly by
   zero `SEEK CENTER[shadow]` lines across six full sessions.
2. **Should this also actuate during `TACTIC_CLIMB`** — now the recommended
   next step, not just a lean. Climb takes roughly 43% of battle ticks per
   ADR 028's own measurement, the way Engage and Regroup already do via
   `_actuate_engage(..., steer_only=True)` (ADR 028 revision 5), and the
   2026-09-21 shadow trial's corpus check found real near-boundary readings
   landing under Climb specifically (3 of 10 archived approach frames from
   one session). Combined with zero `SEEK CENTER[shadow]` firings under
   AttackSupport across six sessions, Climb is where this design's target
   ticks are actually occurring in practice. Still needs Climb's own
   pitch-axis emergency logic (ADR 137) evaluated against a concurrent roll
   correction before wiring it — the same open concern as before — but the
   evidence for prioritizing this now exists where it previously didn't.
3. **Non-convex arenas.** Steering away from the single nearest edge point
   is not guaranteed to be "toward the center" for a non-convex arena
   shape — no evidence either way exists yet on whether MetalStorm's arenas
   are ever non-convex enough for this to matter in practice.
4. **Temporal smoothing.** Deliberately not added in Phase 1 (see
   Actuation) — the shadow trial's per-tick logged direction is the
   evidence that decides whether the existing ADR 113 median filter alone
   is enough, or whether a dedicated `MinimapEma` instance is needed the
   way `EngageNavigator` already has one. Resolved by the first shadow
   trial's data, not by this document.

## References

- ADR 028 (revisions 3-5) — ring-engage geometry, Regroup, Climb's
  concurrent roll actuation
- ADR 075 — `mission_j20`'s fully-adaptive design
- ADR 076 — the spawn-attitude guard, a different fixed-timer answer to a
  related "nothing is steering" gap
- ADR 107, 108, 113, 122, 125, 133, 141 — the boundary detector and
  `BoundaryTurn`'s own use of the same reading this design consumes
- ADR 117 (D8-D11, 2026-09-20/21) — the same-week hardening of the
  diagnostic that consumes this exact reading
- ADR 137 — Fourth Live Trial section, naming `AttackSupport`'s no-op as the
  explanation for uncommanded flight after respawn
- `wingman/engage_nav.py` — `EngageNavigator`, the 43%/57% finding this
  document opens with
- `wingman/tick_handlers.py` — `BehaviorTreeHandler.tick()`,
  `_actuate_engage`, `BoundaryPerceptionHandler`
- `wingman/behavior_tree.py` — `_PRIORITY_ORDER`, `_build_attack_support_slot`
