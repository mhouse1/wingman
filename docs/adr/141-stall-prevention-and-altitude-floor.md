# ADR 141 — Stall Prevention and a Hard Altitude Floor

| Status | Date       | Wingman Version |
|--------|------------|-----------------|
| Draft  | 2026-09-19 | 1.8.10          |

## Context

Live session, 2026-09-19 02:26:38-50: a `MissileEvade` hold ran for ~6
seconds (02:26:38.957-44.939) with nothing watching the airframe underneath
it. Telemetry during that window showed `Speed: 3` (02:26:41.930) and,
moments later in a screenshot the operator captured specifically to pin the
timestamp (`screenshot_20260919_022644.png`), `27 KPH` — a near-stall, not
a dive. Under a second after MissileEvade released control, telemetry
showed a ~745m altitude loss and a jump to 679 KPH. The aircraft crashed at
02:26:50.938 with 3 missiles unused.

Neither of this project's two existing emergency mechanisms could have
caught this in time:

- **The ttg (time-to-ground) trigger** (ADR 086 d2, `recover_below_time_s`)
  needs a *negative altitude rate* to compute anything at all. A
  near-stalled aircraft can show a near-zero or even briefly positive rate
  right up until it isn't — the trigger has nothing to key off until the
  fall is already underway.
- **`MissileEvade` itself has no yield mechanism.** Unlike `BoundaryTurn`
  (ADR 107 D4, `yields_to_fn`), which was given exactly this kind of escape
  hatch to Climb's emergency verdict, `make_missile_evade_condition` never
  had one — despite outranking Climb in the priority selector. Climb's own
  emergency verdict only got a chance to matter once MissileEvade released
  the airframe on its own schedule, by which point it was too late.

Operator directive, verbatim: *"we should implement an stall prevention
mechanism, if speed below 300KPH it should deactivate break and activate
afterburner, during evade manuvers and mission_j20 if altitude below 3000
it should automatically fly up."*

This landed the same night as two other, narrower fixes to the same general
"Climb doesn't get a fair chance to act in time" failure class — ADR 086's
`_climb_exit_push` overshoot bug and ADR 107's BoundaryTurn/Climb handback
race. Those are misfires in mechanisms that already existed. This ADR is
different: it adds two mechanisms that did not exist before tonight.

## Decision

### D1 — A third, deliberately simple emergency OR-term: the altitude floor

`ClimbCondition.update_emergency` (`wingman/behavior_tree.py`) gains
`alt_floor_m`, configured via `behavior_tree.climb.alt_floor_m` (3000 in
production `config.yaml`). No rate, no confirm-reads debounce beyond the
already-immediate single check, no per-map tuning — `snapshot.altitude <
alt_floor_m` is the entire condition. `emergency = ttg_emergency or
terrain_ahead or alt_floor_emergency`. This is deliberately the crudest
possible backstop: a hard floor that does not care whether the aircraft
got there diving, level, or climbing too slowly — exactly the coverage gap
the ttg trigger's rate requirement leaves open.

Unset (`None`) disables it, same opt-in shape every other threshold in this
class already uses. Edge-triggered `BT: ALTITUDE FLOOR` WARNING log,
matching `DIVE RECOVERY`/`TERRAIN AHEAD`'s own logging shape.

### D2 — `MissileEvade` gains a `yields_to_fn`, identical to BoundaryTurn's

`make_missile_evade_condition(is_running_fn=None, yields_to_fn=None)` —
checked first, unconditionally, before the incoming-detection/stickiness
logic: `if yields_to_fn is not None and yields_to_fn(): return False`.
Wired in `_build_missile_evade_slot` to `ctx.climb_emergency_fn`, the exact
same closure BoundaryTurn already reads. This required moving
`_build_missile_evade_slot`'s call in `_build_slots` to *after*
`_build_climb_slot`'s (it read `ctx.climb_emergency_fn` before Climb's slot
builder had set it) — the same "build order satisfies data dependencies,
independent of `_PRIORITY_ORDER`" pattern `_build_boundary_slot` already
established (ADR 139).

Yielding does not strand the evade actuator thread — `_run_missile_evade_hold`
has its own independent fuel/state gating (SAF-013) that does not depend on
the condition re-selecting it; yielding only stops the *selector* from
choosing it again next tick. This is the same reasoning BoundaryTurn's own
D4 decision already used.

Together, D1 and D2 are what closes "during evade maneuvers... automatically
fly up" from the operator's directive: the altitude floor gives Climb a
real emergency signal that doesn't need a rate, and the yield gives that
signal somewhere to land even while MissileEvade currently outranks Climb.
"mission_j20" (the operator's second named context) needed no extra
tactic-level wiring — every tactic ranked *below* Climb in the priority
selector already loses to it automatically the moment `emergency_active`
is true; MissileEvade and BoundaryTurn were the only two ranked *above* it,
and BoundaryTurn already had this since ADR 107 D4. `Eject` (ranked above
both) deliberately does **not** yield — seeded self-destruct dive, discussed
in Non-Goals.

### D3 — Stall prevention: `note_stall_prevention`, tree-independent

New `Controller.note_stall_prevention(game_state)`, called every tick from
`tick_handlers.py` immediately after `note_afterburner_cruise` — the exact
same tree-independent, once-per-tick shape ADR 134 D9 already established,
for the same reason: a stall is an airframe state, not a tactic, and must
not wait for the tree to select something that happens to care about
airspeed.

Below `stall_prevention.min_speed_kph` (300 in production config, raw
*last-accepted* speed reading — not the smoothed `stable_value`, same ADR
069 d6 reasoning `TelemetrySnapshot._ratio_speed` already documents: a ~9s
smoothing window lags a fast real change, and a stall is exactly that) for
`confirm_reads` consecutive ticks (2 in production, debouncing entry the
same way cruise's low-fuel exit does): release `AIRBRAKE_KEY`, hold
`AFTERBURNER_KEY`. Re-asserted every tick while active (D9's own shape —
wins the key back within one tick of anything else re-pressing airbrake
behind it). Exit is immediate on the first good reading — there is nothing
held to debounce releasing, and the airbrake release is a one-shot action
each tick, not a state to unwind.

**Deliberately overrides Climb's own emergency airbrake hold when both are
true at once.** Climb's `EMERGENCY` mode holds `AIRBRAKE_KEY` on purpose
(ADR 137) — bleeding energy out of a *fast* dive. Stall prevention releases
it on purpose — a stalled airframe needs energy *back*. These are opposite
prescriptions for opposite problems that happen to touch the same key, and
they only conflict when both are true simultaneously (a fast dive that has
also dropped below the speed floor). Stall prevention wins: a stalled
aircraft has degraded control authority regardless of what the pitch axis
is commanding, so holding drag on does not help it climb out — this is
standard unstall procedure (reduce drag, add power), not a judgment call
specific to this codebase. Routed through the existing `_may_hold_key`
arbiter (ADR 139 D4) for the afterburner side, as a new `"stall_prevention"`
requester (unconditionally `True` — the one real gate, manual takeover, is
already checked once before either key is touched); the airbrake release
is not gated through `_may_hold_key` at all, since that arbiter answers
"may I *hold*," not "must I *not release*."

Only real gate: `SAF-001` manual takeover — checked once, matching cruise's
own single real deferral.

## Non-Goals

1. **Not touching `Eject`'s priority or its own dive.** `eject_and_dive` is
   a deliberate, controlled self-destruct (ADR 136/137) — a low-speed or
   low-altitude reading during an intentional dive is not a stall or a
   terrain emergency, it is the dive working as designed. Neither D1's
   floor nor D2's yield reaches `Eject`; D3's stall prevention is not
   tactic-gated at all and *does* still apply during `GAME_BATTLE_EJECT`
   (a real stall can happen mid-eject too, independent of the intentional
   descent `eject_and_dive` itself controls), but does not touch
   `eject_and_dive`'s own nose-down/afterburner sequencing directly — it
   only ever touches `AIRBRAKE_KEY`, which `eject_and_dive` does not use.
2. **Not resolving the still-open "stale telemetry during critical
   recovery" pattern** flagged the same night across at least four separate
   crash traces (documented in ADR 086 and ADR 107's live-trial sections).
   That is a suspected telemetry/OCR-cadence issue, not something either of
   tonight's three fixes (this ADR, ADR 086's exit-push fix, ADR 107's
   handback-race fix) directly addresses — it may turn out to be partly
   mitigated by D1's floor (a hard altitude check doesn't need a fresh
   *rate* the way ttg does) but that is a hypothesis for the next live
   trial to test, not a claim made here.
3. **Not a general MissileEvade redesign.** D2 adds exactly one escape
   hatch (the climb emergency verdict) — MissileEvade's own fuel gating,
   manoeuvre cap, and pitch-down option (ADR 070) are all unchanged.
4. **Not consolidating stall prevention into `_run_climb_hold` itself.**
   D3 lives as an independent, tree-external check specifically so it
   applies regardless of which tactic (or none) currently holds the
   airframe — folding it into Climb's own hold loop would only cover the
   case where Climb is already selected, missing exactly the MissileEvade
   case that motivated this ADR.

## Testing plan

- `tests/test_behavior_tree.py`: `TestAltitudeFloor` (5 tests — fires below
  the floor regardless of rate, does not fire above it, disabled when
  unconfigured, the exact live near-stall shape ttg cannot catch, missing
  altitude draws no conclusion); `test_missile_evade_yields_to_the_climb_
  emergency` / `test_missile_evade_stays_sticky_when_not_yielding` (unit
  level, mirroring `test_it_yields_to_the_climb_emergency_band`);
  `test_missile_evade_yields_to_climb_through_the_real_tree` /
  `test_missile_evade_still_wins_without_an_emergency` (full-tree
  integration, mirroring the Anomaly 007 BoundaryTurn pair — proves the
  `_build_slots` reorder actually wires end to end, not just the isolated
  condition).
- `tests/test_stall_prevention.py` (new file, 11 tests, mirroring
  `tests/test_afterburner_cruise.py`'s exact shape): speed above the floor
  does nothing; a single low reading does not trigger (debounce); confirm-
  reads consecutive readings do; the exact live 27 KPH shape; recovery
  releases immediately; a stale reading does nothing; manual takeover
  blocks it entirely; outside battle states does nothing; `GAME_BATTLE_
  EJECT` still applies; disabled does nothing; re-asserts every tick while
  active.
- Full gate (`make lint && make test`) green, 1636 passed / 2 skipped, zero
  changes needed to any pre-existing test.
- **Not yet live-validated.** All three of D1/D2/D3 are new code paths that
  did not exist before tonight — needs its own live trial watching
  specifically for: an `ALTITUDE FLOOR` firing, a `STALL PREVENTION`
  firing, and confirmation that MissileEvade correctly cedes to Climb when
  both are true together, none of them causing an unexpected side effect
  (e.g. stall prevention's airbrake release fighting a *legitimate*,
  still-needed emergency airbrake hold in a dive that is fast but not
  actually stalled).

## Open Questions

1. **Is 300 KPH / 3000m the right pair of thresholds?** Both are the
   operator's own directive, not derived from a live-measured false-positive
   rate the way e.g. ADR 086's `recover_below_time_s` was tuned. Watch the
   first live session for `ALTITUDE FLOOR`/`STALL PREVENTION` firing during
   ordinary, non-dangerous flight (e.g. a deliberate low pass, or normal
   post-respawn climb-out before altitude has built up) — if either fires
   on genuinely safe flight, the thresholds need revisiting, not the
   mechanism.
2. **Does stall prevention's airbrake override ever fight a genuinely
   necessary emergency airbrake hold?** D3's own reasoning argues no (a
   stalled airframe cannot benefit from held drag regardless of dive
   state) — but this is reasoned from first-principles aerodynamics, not
   yet measured against a real simultaneous fast-dive-plus-stall case
   live. No such case has been observed yet in this project's logs.
3. **Should `_run_climb_hold`'s own airbrake-hold logic become aware of
   the stall condition directly**, rather than relying on D3's external
   override to win the race every tick? The current design accepts the
   same "may oscillate slightly, net-correct" tradeoff ADR 134 D9 already
   accepts for cruise-afterburner vs. other tactics. Revisit if live data
   shows this oscillation is worse in practice than assumed.

## References

- ADR 086 — the ttg emergency trigger this adds a third OR-term alongside,
  and the exit-push overshoot fix landing the same night.
- ADR 107 D4 — `BoundaryTurn`'s `yields_to_fn`, the exact pattern D2 copies
  for MissileEvade, and the handback-race fix landing the same night.
- ADR 134 D9 — `note_afterburner_cruise`, the tree-independent every-tick
  shape D3 copies, and the `_may_hold_key` arbiter (ADR 139 D4) D3 extends
  with a new requester.
- ADR 070 — MissileEvade's own design; D2 adds one escape hatch without
  otherwise changing it.
- ADR 136/137 — `eject_and_dive` and the emergency climb airbrake hold,
  both referenced in Non-Goals/D3's precedence reasoning.
- `screenshot_20260919_022644.png`, `test_screenshots/crash_with_missiles/
  crash_20260919_022650_14.png` — the live evidence this ADR is written
  from.
