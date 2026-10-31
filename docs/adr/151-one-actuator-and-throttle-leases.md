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

**D6. The pitch keys, `NOSE_UP_KEY` and `NOSE_DOWN_KEY`, are leased (2026-10-24, 1.9.3).**
The second step of D5. A release by one owner no longer lifts the key while another
still holds it. The D3 yields stay the throttle's: `_owner_may_hold` answers True for any
other key, because pitch owners give the axis up by releasing it (ADR 148), not by a
policy turning. Measured before D6 on 62 missions (2026-10-23 19:02 to 2026-10-24
01:08, two logs): 87 places where the pursuit's ADR 148 hand-over released `i` while the
climb held it, 41 where the spawn guard's hand-over did, and 14 of 228 deaths within 8 s
of a pursuit one. One read by hand: at 00:54:22.808 an emergency pull-out pressed `i`;
the hand-over lifted it at .892, so the first 1.5 s pulse lasted 84 ms, the next
came 1.75 s later, and the aircraft hit a hillside at 631 m still 16 deg nose-down.
Roll stays on Phase 1 behaviour.

D6 requires every pitch owner to release its own lease. The spawn guard skipped its
release whenever a climb was running (ADR 076 d2), which under Phase 1 kept it from
lifting the climb's key; under D6 it left the guard's lease behind. The first live run
latched nose-up that way (2026-10-24 02:10:25 to the round's end, the aircraft looping
from +16 to -90 deg), and the guard now always releases.

## Consequences

- A climb that ends no longer drops the burn cruise is holding. Before Phase 2, in the
  first 30 minutes of the 2026-09-27 09:15 session, `climb` dropped cruise's
  `e` 18 times and `stall_prevention` and `cruise` dropped each other's 11 times; each
  drop left the burner off until the holder's next tick (up to 1.5 s).
- The Actuator's DEBUG line changes for the throttle: `<owner> released 'e'; still
  held for <owners>` replaces `<owner> released 'e' while <owner> held it`.
- D6: a pitch key stays down while any lease on it remains, so an owner that pressed
  `i` or `k` and never released it would now hold the key until a takeover or
  shutdown, where before any other release lifted it. Every pitch press site in
  `controller.py` releases under the owner name it pressed with (checked 2026-10-24).
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

- **V4, live, D6.** In a session after D6: no `released 'i' while climb held it` lines;
  `still held for climb` lines instead. No pitch key held through a round end, and no
  rise in deaths per mission against the baseline row below.

### Live evidence for D2 and D4

| Session | Code state | Game UI | Battle-min | Throttle drops (`<owner> released 'e' while <owner> held it`) | Emergency windows / evade holds inside | Verdict |
|---------|------------|---------|-----------:|------|------|---------|
| 2026-09-27 11:41-15:17, `logs/wingman_20260927_151759.log`, operator's `make rd`, 36 missions | before D2 | post-update | 168.9 | **300** (1.78 per battle-min): climb over cruise 139, evade over cruise 27, eject over cruise 25, cruise over evade 23, stall prevention over cruise 21 | 1 window, 8 s / 0 | Baseline |
| 2026-09-27 15:54-18:33, `make rd`, pid 2844217, 29 rounds | D1-D5 (uncommitted) | post-update | 125.7 | **0**, where the baseline rate predicts about 224. `still held for` 1.45 per battle-min instead. Presses 162.3 per battle-min (baseline 166.9), flares per incoming 0.50 (0.50), deaths 0.80 per battle-min (0.88), 0 errors | 0 / 0 | **D2 passed**, no regression visible (29 rounds: supporting, not proof). D4 not exercised: no emergency climb |

### Live evidence for D6

| Session | Code state | Game UI | Missions | Climb pitch lifts (pursuit / spawn guard) | Deaths within 8 s of a pursuit lift | Verdict |
|---------|------------|---------|---------:|------|------|---------|
| 2026-10-23 19:02-23:49 (`logs/wingman_20261023_234917.log`, 49 missions) and the operator's acct2 session from 23:49 (`wingman.log` to 01:08, 13 missions) | before D6 | 2026-10-23 | 62 | 87 / 41 | 14 of 228 | Baseline |
| 2026-10-24 02:00:20-02:14:49, `make r1` (acct1), first D6 run, stopped with `z` | D6 without the spawn-guard release | 2026-10-23 | 2 (9 deaths) | 0 / 0. 4 releases kept the climb's `i` (`still held for climb`), one a pursuit hand-over at -51 deg that recovered (02:11:59) | 0 of 9 | **Regression**: 5 guard ends skipped their release, and 28 releases left `i` held for `spawn_guard` alone (2 of them legitimate, at a respawn while the guard ran). The aircraft looped from +16 to -90 deg (02:10:27-02:10:37). Fixed: the guard always releases |
| 2026-10-24 02:25:10-03:19, `make r1` (acct1), pid 3862620, stopped with `z` at 03:19:35 | D6 with the spawn guard always releasing | 2026-10-23 | 9 (31 deaths) | **0** / 0, where the baseline rate predicts about 13. 18 pursuit hand-overs kept the climb's `i` (`still held for climb`); of the 4 traced, 3 recovered (entries at -13 to -90 deg) and 1 was shot down mid-recovery. 0 leftover guard leases. The boundary turn's `i` held through climb releases and ended with the turn | 4 deaths within 8 s of a hand-over: AIM-54, M39A2, R-73, and 1 unknown (incoming missile 9 s before, telemetry blind for the last 6 s) | **V4 lease half passed.** Deaths per mission 3.4 against 3.7: 9 missions, not evidence either way |
| 2026-10-24 03:40:42-06:12, `make r1` (acct1), pid 3935103, soak, stopped with `z` at 06:11:50 | D6 as above, unchanged | 2026-10-23 | 25 | **0** / 0. 49 pursuit hand-overs kept the climb's `i` (`still held for climb`); 0 leftover guard leases | — | Crashes (death card `no kill card`) a mission over all three D6 runs: 18 in 34, 0.53, against 37 in 70, 0.53, before D6 (acct2; the account differs). The lifts touched about 6% of deaths before D6, a change of the order of 0.05 crashes a mission, which no run of this length resolves: **V4's lease half passed; its deaths half is not measurable at this size** |

## References

- Code review 018: CR-018-09 (`docs/code-review/018-2026-09.md`)
- ADR 139 D4 (the arbitration point this makes data), ADR 134 D9 (cruise), ADR 137
  (the emergency airbrake), ADR 128 D7 and D8 (the afterburner evade)
