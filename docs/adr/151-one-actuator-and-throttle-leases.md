# ADR 151 — One Actuator, and Leases for the Throttle

| Status | Date       | Wingman Version |
|--------|------------|-----------------|
| Draft  | 2026-09-27 | 1.8.12          |

## Decision

**D1. Every key press and release goes through `wingman/actuator.py`.** Code review
018 (CR-018-09) found five press paths in `controller.py`, each applying its own
subset of the guarantees a press needs. Phase 1 moved every raw keyboard call behind
one `Actuator` without changing what any of them does: each call site passes the
focus gate it had, and errors propagate as before. Every press names its owner.
`tests/test_actuator.py` fails when any other module calls `keyboard_module.press`
or `.release`.

**D2. The throttle, `AFTERBURNER_KEY`, is leased (Phase 2, first key).** The X server
keeps one state per key, so one writer's release used to drop every other writer's
hold, and cruise and the afterburner evade won only by re-pressing. Now a release
ends that owner's lease, and the key goes up only when no remaining holder may hold
it right now.

**D3. Who may hold the throttle is data.** `Controller._THROTTLE_YIELDS` lists, per
owner, the conditions it yields to: cruise and the evade yield to a manual takeover
and an emergency climb; climb, missile evade, eject and stall prevention yield to
nothing at this level. `_may_hold_key` (ADR 139 D4) is a lookup into that table, and
the Actuator's leases read the same table. `docs/architecture.md` "Flight-input
precedence" is its prose.

**D4. A condition change lifts the key at once.** The climb calls
`Actuator.reevaluate(AFTERBURNER_KEY)` where it raises the emergency flag, so a
cruise or evade lease does not keep the burner on over the emergency airbrake until
its holder's next tick. Without this, D2 would have regressed ADR 137: the climb
releases the burner a few lines before it raises the flag.

**D5. The other contested keys follow one at a time,** each with its own live check:
pitch, then roll. Until then they keep Phase 1 behaviour.

## Consequences

- A climb that ends no longer drops the burn cruise is holding. Before Phase 2, in the
  first 30 minutes of the 2026-09-27 09:15 session, `climb` dropped cruise's
  `e` 18 times and `stall_prevention` and `cruise` dropped each other's 11 times; each
  drop left the burner off until the holder's next tick (up to 1.5 s).
- The Actuator's DEBUG line changes for the throttle: `<owner> released 'e'; still
  held for <owners>` replaces `<owner> released 'e' while <owner> held it`.
- A takeover and shutdown still release every key and clear every lease
  (`release_all`).
- The module-level Actuator is shared by every Controller in a process. Tests clear its
  lease record between tests (`tests/conftest.py`).

## Validation

- **V1.** Phase 1 changes no behaviour: live 2026-09-27 09:15-10:21, 11 missions,
  presses reaching the backend 187.5 per battle-minute against 176.5 and 181.0 before,
  flares per incoming detection unchanged, 0 errors (code review 018's evidence table).
- **V2.** Lease semantics, the policy table and the emergency re-check:
  `tests/test_actuator.py`, `tests/test_may_hold_key.py`.
- **V3, live.** In a session after D2: no `climb released 'e' while cruise held it`
  lines, `still held for cruise` lines instead, and no afterburner inside
  emergency-climb windows (ADR 128 V12). The lease half passed on 2026-09-27 (below);
  the emergency half is not yet observed.

### Live evidence for D2 and D4

| Session | Code state | Game UI | Battle-min | Throttle drops (`<owner> released 'e' while <owner> held it`) | Emergency windows / evade holds inside | Verdict |
|---------|------------|---------|-----------:|------|------|---------|
| 2026-09-27 11:41-15:17, `logs/wingman_20260927_151759.log`, operator's `make rd`, 36 missions | before D2 | post-update | 168.9 | **300** (1.78 per battle-min): climb over cruise 139, evade over cruise 27, eject over cruise 25, cruise over evade 23, stall prevention over cruise 21 | 1 window, 8 s / 0 | Baseline |
| 2026-09-27 from 15:54, `make rd`, pid 2844217; at 16:19, 5 rounds | D1-D5 (uncommitted) | post-update | 18.7 | **0**, where the baseline rate predicts about 33. 27 `still held for` lines instead: climb over cruise 18, eject over cruise 3, the rest single | 0 so far | **D2 passed.** D4 not yet exercised: no emergency climb in the run so far |

## References

- Code review 018: CR-018-09 (`docs/code-review/018-2026-09.md`)
- ADR 139 D4 (the arbitration point this makes data), ADR 134 D9 (cruise), ADR 137
  (the emergency airbrake), ADR 128 D7 and D8 (the afterburner evade)
