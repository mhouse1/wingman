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

Add an `elif _may_fly and selection == TACTIC_ATTACK_SUPPORT:` branch beside
the existing `TACTIC_ENGAGE`/`TACTIC_REGROUP`/`TACTIC_CLIMB` branches in
`BehaviorTreeHandler.tick()`. On a tick where:

1. the boundary reading is not `None`, and
2. `dist <= seek_center_trigger_frac` (a new, deliberately *wider-than-BoundaryTurn* config threshold — see Config below),

compute `error_norm` from the reading's `forward`/`lateral` components
rotated 180° (steer away from the boundary bearing, not toward it) and call
`self._ctrl.orient_nose_to_target(error_norm, **self._ctl_cfg)` — the
identical roll-only primitive `_actuate_engage` already calls for Engage and
Regroup. No new actuator, no new key binding, no new `Controller` method.

A small deadzone (`seek_center_deadzone_deg`, mirroring
`EngageNavigator.bearing_deadzone_deg`) suppresses correction when the
aircraft is already headed away from the boundary, so this does not hunt on
noise once it has done its job — same shape as the existing engage deadzone,
new instance of it, not new logic.

**Shadow first.** Matching this codebase's standing convention (ADR 070's
missile-evade shadow trial, HLDD 001 Phase 1's terrain-ahead shadow, ADR 028
revision 4's own "off unless config turns it on" framing for Regroup): Phase
1 ships with `seek_center_enabled: false` and, while disabled, logs what it
*would* command (`SEEK CENTER[shadow]: would roll err=%.2f dist=%.2f`) on
every tick where the trigger condition is met but actuation is suppressed —
the same `self._dry_run`-style branch `_actuate_engage` already has for
Engage. A live session's shadow log is what decides whether Phase 1 goes
live, not a guess.

### Priority and safety — nothing to add, only to confirm

`TACTIC_ATTACK_SUPPORT` is already last in `_PRIORITY_ORDER`. Every existing
higher-priority tactic — most importantly `TACTIC_BOUNDARY_TURN` itself,
`TACTIC_MISSILE_EVADE`, `TACTIC_EJECT`, `TACTIC_CLIMB` — continues to
pre-empt this one exactly as it pre-empts Engage and Regroup today. This
design adds no new priority comparison and cannot introduce a conflict
`_PRIORITY_ORDER` does not already resolve. The `_combat_ok` /
`snap.survival_hold` gate `_actuate_engage`'s callers already apply to
Engage/Regroup/Climb applies here identically — a survival hold still owns
the flight path uncontested.

### Config

Proposed, under a new `behavior_tree.attack_support` block (mirroring
`behavior_tree.climb.terrain_avoidance`'s pattern of nesting a sub-feature
under its owning tactic):

```yaml
behavior_tree:
  attack_support:
    seek_center_enabled: false       # Phase 1 default — shadow only
    # Deliberately wider than boundary_near_frac (0.35, BoundaryTurn's own
    # trigger) — this is the gentle, early bias; BoundaryTurn stays the
    # last-resort emergency layer with its own, tighter threshold.
    seek_center_trigger_frac: 0.55
    seek_center_deadzone_deg: 15.0
```

`seek_center_trigger_frac` starting above `boundary_near_frac` (0.35) is a
deliberate, named guess — Phase 1's shadow log is what should confirm or
correct it, not this document.

### Testing plan

- Unit: a pure-logic test analogous to `EngageNavigator`'s own tests —
  given a boundary reading and existing selection state, does the new branch
  compute the correct reciprocal `error_norm`, respect the deadzone, and
  leave every other tactic's actuation untouched? No frames, no analyzer,
  same style as `tests/test_engage_nav.py`.
- Unit: confirm the new branch never fires when `selection !=
  TACTIC_ATTACK_SUPPORT`, when `_combat_ok` is false, or when the boundary
  reading is `None` — mirrors the existing `_may_fly`/`survival_hold` gate
  tests already covering Engage/Regroup/Climb.
- Shadow live trial (required before `seek_center_enabled: true` ships):
  one full session, `seek_center_enabled: false`, reading the `SEEK
  CENTER[shadow]` log line count and the commanded direction against the
  actual boundary trace already recorded by
  `BoundaryPerceptionHandler._boundary_trace` — same evidence class ADR 117
  and ADR 108 both used to validate detection changes before trusting them.
- Live trial (after shadow validates): compare `total_rtb_with_missiles`,
  confirmed-crossing rate, and `BoundaryTurn` trigger frequency
  session-over-session with the feature on vs. off — this design's success
  criterion is fewer ticks reaching `BoundaryTurn`'s emergency threshold at
  all, not a new metric of its own.

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
  actuation dispatch), reads `self._boundary.reading`
  (`BoundaryPerceptionHandler`, already constructed and ticked every cycle).
- `wingman/controller.py` — `orient_nose_to_target` (existing, unmodified;
  same call signature `_actuate_engage` already uses).
- `wingman/config.yaml` / `wingman/config_schema.py` — new
  `behavior_tree.attack_support` section.
- No changes to `wingman/behavior_tree.py`'s tree structure, priority order,
  or `TACTIC_ATTACK_SUPPORT`'s selection condition (`always`, unchanged) —
  this is an actuation-side addition only.

## Open Questions

1. **Readability ceiling.** ADR 117 measured in-battle boundary readability
   at 56%; ADR 133's corroborated-span fix and this week's D8-D11 shape
   checks improve the diagnostic's precision but do not by themselves raise
   how often a reading exists at all. This design can only correct on the
   ticks a reading exists — on the rest, it is exactly as idle as
   `TACTIC_ATTACK_SUPPORT` is today. Not a blocker (BoundaryTurn has the
   same ceiling and is still worth having), but the shadow trial should
   report what fraction of true-idle ticks actually got a reading to act on.
2. **Should this also actuate during `TACTIC_CLIMB`**, the way Engage and
   Regroup already do via `_actuate_engage(..., steer_only=True)` (ADR 028
   revision 5)? Climb takes roughly 43% of battle ticks per that ADR's own
   measurement, and an idle-roll-axis climb is exactly the kind of tick this
   design exists for. Leaning yes, deferred to the shadow trial rather than
   decided here, since Climb's own pitch-axis emergency logic (ADR 137) has
   not been evaluated against a concurrent roll correction.
3. **Non-convex arenas.** Steering away from the single nearest edge point
   is not guaranteed to be "toward the center" for a non-convex arena
   shape — no evidence either way exists yet on whether MetalStorm's arenas
   are ever non-convex enough for this to matter in practice.

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
