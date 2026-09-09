# ADR 111 — The Hold Pre-empts the Running Mission

| Status | Date       | Wingman Version |
|--------|------------|-----------------|
| Draft  | 2026-09-04 | 1.8.8           |

> Implemented 2026-09-04. V1-V4 covered by tests; V5 and V6 CONFIRMED LIVE in
> the 18:43 session — see *Live confirmation*.

## Context

ADR 109 and ADR 110 gave the survival hold authority over the tactics. They did
not give it authority over the **mission**, and that turned out to be the larger
hole.

`GAME_BATTLE` starts `mission_j20` automatically. `mission_loiter` is started by
the operator pressing a key. Both take `_mission_lock`, so whichever holds it
wins — and the automatic one is almost always already holding it when the
operator asks for a hold. Observed 2026-09-04: the operator pressed the key, and
the j20 sequence kept flying the aircraft.

The same shape appeared with `GAME_BATTLE_EJECT`. ADR 109 stopped the *tactic*
from selecting Eject during a hold, but an eject already **in flight** when the
key was pressed carried on diving. Suppressing a future decision does nothing
about a thread already committed to one.

A third defect sat inside the orbit itself. The direction was reversed whenever
the boundary read closer than the last tick — but range to a fixed line
naturally oscillates while flying a circle, so the reading alternated and the
orbit reversed 6 times in 30 s, which is a wobble, not a circle.

## Decision

**D1. Starting a hold cancels the running mission.** `mission_loiter` calls
`cancel_mission()` when the lock is held, then acquires with a timeout. A key
the operator pressed deliberately outranks a mission wingman started on its own.

**D2. Acquire with a timeout, and give up cleanly.** If the cancelled mission
does not release within `lock_timeout_s` (5 s), the hold logs and returns rather
than blocking the hotkey thread. Per the lock rule in CLAUDE.md — a bare
`with lock:` here would hang the operator's only override.

**D3. An eject in flight is cancelled, not merely out-voted.** The hold sets
`_eject_stop` with reason `survival_hold`. ADR 109's condition gate stops the
*next* eject; this stops the *current* one. Both are needed, and the log names
which mechanism acted.

**D4. Orbit direction compares CLOSEST APPROACH across windows, not
tick-to-tick range.** Each ~15 s window records its minimum distance; a reversal
needs two completed windows whose minima differ by more than `closing_margin`.
A circle that is genuinely drifting toward the edge shows a falling minimum; a
circle that is merely circling does not.

**D5. Seed the next window with the current sample.** The first version left the
new window's minimum at `None` and hit `TypeError: min(None, float)` on the
first sample after rollover — inside the loiter thread, which ends the hold.

## Consequences

`y` now means what it says: whatever wingman was doing stops, and the aircraft
holds. This is the first mechanism here where an operator action pre-empts a
running mission rather than queueing behind it.

Cancellation is not instant. `cancel_mission()` sets an event that the running
mission notices at its next check, so there is a sub-second window where the old
mission still owns the aircraft. D2 bounds it; it does not remove it.

A hold started during an eject that has already dived below recovery altitude
will still lose the aircraft. Cancelling the eject is not the same as recovering
from it.

## Validation

- **V1.** With the mission lock held, `mission_loiter` calls `cancel_mission()`
  before acquiring.
- **V2.** A lock never released produces a warning and a return, not a hang.
- **V3.** With `_ejecting` set, the hold sets `_eject_stop` with reason
  `survival_hold`.
- **V4.** Ordinary oscillation does not reverse the orbit; two windows with a
  materially nearer minimum do; one window is not enough.
- **V5 — live. MET 2026-09-04.** Eject cancellation, 5 occurrences.
- **V6 — live. MET 2026-09-04.** Zero orbit reversals across 9 holds.

## Live confirmation (2026-09-04, 18:43 session)

| Signal | Count |
|--------|------:|
| Loiter runs | 9 |
| Ejects cancelled by the hold | 5 |
| Missions pre-empted | 1 |
| Orbit reversals | 0 |
| Missions started (100% click-to-finish) | 10 |

Five ejects cancelled is the ADR 109 gap closed — each one is an aircraft that
the previous build would have dived into the ground. Zero reversals across nine
holds, where the tick-to-tick comparison produced six in a single 30 s window.

The session did **not** meet ADR 110 V5: the holds still left their altitude
band, upward. That is ADR 112.

## References

- ADR 109 — Eject yielding to the hold; D3 closes the gap it left
- ADR 110 — the tactics yielding to the hold; this extends that to the mission
- ADR 112 — the altitude band the holds in this session failed to keep
- ADR 107 — BoundaryTurn, whose readings D4 consumes
- `wingman/controller.py` — `mission_loiter`, `_loiter_pick_orbit_direction`
