# ADR 132 — Fly the Spawn Heading First

| Status | Date       | Wingman Version |
|--------|------------|-----------------|
| Accepted | 2026-09-07 | 1.8.8         |

## Context

Reported by the operator: on respawn and at battle entry the aircraft flies in
circles instead of joining the fight.

The mechanism is a collision between two things that are each behaving as
designed. `BoundaryTurn` banks and pulls whenever the detector reads an edge
inside `turn_frac` (0.50R), and a fresh spawn is frequently close enough to an
arena edge to satisfy that on the first ticks of a life. The turn then holds,
releases, and re-arms, and the aircraft orbits near its spawn point rather than
flying into the arena.

ADR 106 already recorded a symptom of the same thing from the other direction:
"32 turns in 12 minutes, one at round start with the aircraft nowhere near an
edge."

The operator supplies the domain fact that makes this cheap to fix: **spawn
points face into the arena.** For the first seconds of a life, the correct
heading is simply the one the aircraft spawned on — it leads away from the
boundary and toward the middle of the map. No perception is required to know
that, which matters, because ADR 117 measured the boundary detector missing one
confirmed-crossing frame in eight and returning None on 90% of frames where the
edge is actually in view. A tactic driven by that signal should not be the first
thing to command the aircraft in a new life.

## Decision

**D1. `mission_j20` suppresses commanded left/right turns for
`mission.j20_turn_guard_s` (10 s) from the moment it starts.** The aircraft
flies the heading it spawned on. Pitch, throttle, weapons and the search-and-
destroy loops are untouched.

**D2. Armed in `mission_j20`, not on a respawn event.** Battle entry and every
respawn restart both run through `mission_j20` — `restart_last_mission` calls it
once the new life reads alive — so one arm point covers both cases the operator
named and cannot drift apart from them.

**D3. A deadline, not a flag.** `_turn_guard_until` is a `time.monotonic()`
target. A boolean that some path forgets to clear leaves the aircraft unable to
turn for the rest of the round, which is a far worse failure than the circling
this prevents. The window ends by the passage of time and by nothing else.

**D4. Gated at three call sites, because there is no single choke point.**
`roll_left`/`roll_right` go through `_execute_key_press`, but `boundary_turn_mode`
presses its roll key through `_climb_key` and `disengage_roll_right` presses it
directly. Gating only the roll methods would have missed **the very tactic that
causes the circling**. Each suppression is logged: a turn that is silently
dropped looks exactly like a tactic that failed to fire.

**D5. Missile evade is deliberately exempt.** It holds `ROLL_RIGHT` as part of a
survival response (ADR 070, SAF-001) and presses its keys directly rather than
through any gated method, so it bypasses the guard by construction. Ten seconds
of suppressed evade at the start of every life could cost the aircraft this ADR
is trying to protect. A test asserts the exemption, so a future refactor that
routes evade through `roll_right()` fails loudly instead of silently disarming
it.

**D6. The guard fails OPEN.** An unreadable guard reports "not guarded" and the
turn proceeds. It is consulted on the tactic threads, so an exception would take
`BoundaryTurn` down entirely — an aircraft that cannot turn at all is worse than
one that circles for a few seconds.

## Consequences

For the first ten seconds of each life the aircraft holds its spawn heading and
climbs, engages and fires as normal. `BoundaryTurn` is delayed, not disabled: it
runs the moment the window lapses, and a genuine edge approach later in the life
is handled exactly as before.

The cost is that a spawn which really does point at a nearby edge gets ten
seconds of no turn. That is the case D1 trades away deliberately, on the
operator's domain claim that spawns face inward. If it proves wrong the symptom
is specific and easy to spot — crossings clustered in the first seconds of a
life — and the window is one config value.

This does not fix the detector. ADR 117's finding stands, and this guard makes
the first ten seconds of a life independent of it rather than making it better.

## Validation

- V1. Unit: arming blocks `roll_left` and `roll_right`.
- V2. Unit: the window expires on its own and turning resumes.
- V3. Unit: a zero window disables the guard entirely.
- V4. Unit: `boundary_turn_mode` is blocked while guarded, and runs once the
  window lapses — the tactic is delayed, not disabled.
- V5. Unit: `disengage_roll_right` is blocked, and does not cancel the mission
  on its way out.
- V6. Unit: the default window is read from config, not hardcoded.
- V7. Unit: missile evade routes through none of the gated methods (D5).
- V8. Unit: `mission_j20` arms the guard.
- V9. Live: **satisfied 2026-09-07** on a 10h55m, 113-mission unattended soak.

| Signal | Count |
|--------|------:|
| Guard armed (one per mission start) | **449** |
| `boundary turn suppressed` | **184** |
| `roll_right suppressed` | 416 |
| `roll_left suppressed` | 335 |
| `disengage roll suppressed` | 0 |
| `[ERROR]` across the whole soak | **0** |

184 boundary turns that would previously have banked in the first seconds of a
life were suppressed — the circling this ADR was written for, occurring on
roughly 41% of spawns. The guard armed on every mission start and the soak ran
eleven hours without an error.

The crossing rate did **not** improve: 0.115 per mission against a post-update
pooled 0.134, which is mid-pack among the large-sample rows. That is the expected
result and not a failure of this ADR — D1 delays a tactic that ADR 107 measured
as break-even, so removing it for ten seconds should not change crossings. This
guard buys a clean start to each life, nothing more.

Covered by `tests/test_turn_guard.py` (11 tests).

## References

- ADR 107 — `BoundaryTurn`, the tactic this delays
- ADR 117 — the detector's miss rate, the reason not to trust it at spawn
- ADR 076 — the existing respawn nose-up guard, the same idea for pitch
- ADR 070 / SAF-001 — missile evade, exempt under D5
