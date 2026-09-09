# ADR 130 — Standby Restart Killed by Its Own Exit Flag

| Status | Date       | Wingman Version |
|--------|------------|-----------------|
| Accepted | 2026-09-06 | 1.8.8         |

## Context

ADR 099 put wingman into standby after the first Backspace specifically so the
operator can keep flying — including restarting the automated mission with
`'u'` (J20) or `'y'` (loiter) — while MetalStorm stays up. `2026-09-06 04:25` to
`04:27`, `wingman.log` (archived as `logs/wingman_20260906_042814.log`) shows
that restart never actually worked:

```
04:26:33,677 Controller: 'u' key pressed - starting J20 mission (state=GAME_BATTLE)
04:26:33,677 Controller: mission 'j20' started → GAME_BATTLE
04:26:33,678 Controller: mission_j20 - starting mission sequence (lock acquired)
04:26:33,678 Controller: search_and_destroy padlock loop started
...
04:26:33,728 Controller: exit requested, aborting mission wait
04:26:33,729 Controller: mission_j20 - cancelled, stopping loops
04:26:33,730 Controller: search_and_destroy_loop stopped
```

51 milliseconds from "started" to "cancelled". The FSM reached `GAME_BATTLE` —
that half of the requirement held — but the mission behind it never ran a
single 0.5 s cycle. This repeated on essentially every `'u'` press for the rest
of the standby session (measured: 9 separate `mission_j20` starts between
`04:25:50` and `04:27:18`, none surviving past ~50-100 ms).

## Root cause

`mission_j20` and `mission_loiter` treat `_exit_event` (`exit_requested`) as
"stop now" in four places — the mission-runner thread's poll loop and the
caller's wait loop, for each mission. That is correct for a genuine shutdown
(SIGTERM, the startup-stall exit), but `_exit_event` is also what the FIRST
Backspace sets to break the main loop out into standby (`main.py`), and
**nothing ever clears it** afterward — by design, since the process is not
actually exiting, it is parking in the standby wait for a second Backspace.

So standby's own entry condition is indistinguishable, at the mission's abort
check, from "the process is tearing down right now." Any mission started by a
hotkey during standby saw the stale flag on its very first poll and
self-cancelled, defeating the reason hotkeys are kept alive during standby in
the first place.

`_operator_stop_event` already exists to make exactly this distinction
elsewhere — it is set only by Backspace (ADR 099), and the constructor comment
for it says so in as many words: "`exit_requested` cannot stand in for this —
SIGTERM and the startup-stall exit set that too." The four mission-abort checks
had not been written against it.

## Decision

**D1. A mission abort checks for a real exit, not just a set flag.**
`Controller._mission_exit_requested()` returns true only when `_exit_event` is
set **and** `_operator_stop_event` is not — i.e. `exit_requested` fired for a
reason other than "we already entered standby."

**D2. All four mission-wait sites use it.** `mission_j20`'s runner-thread poll,
`mission_j20`'s caller-side wait, and the equivalent pair in `mission_loiter`
replace their raw `self._exit_event.is_set()` check with
`self._mission_exit_requested()`.

**D3. A genuine exit still aborts the mission.** SIGTERM and the startup-stall
path set `_exit_event` without ever setting `_operator_stop_event`, so
`_mission_exit_requested()` still returns true for them — the pre-existing
teardown guarantee (SAF-007's "no injected key survives process termination"
family) is unaffected.

## Consequences

A mission started by `'u'`/`'y'` during standby now runs until the operator
cancels it, it completes on its own, or a real exit happens — not until its
first 50 ms poll tick. No other mission-abort behavior changes: outside
standby, `_operator_stop_event` is unset and `_mission_exit_requested()`
behaves exactly as the old check did.

## Validation

- V1. Unit: a mission started with `_operator_stop_event` set and `_exit_event`
  set (simulating standby) stays running well past the old ~50 ms failure
  window.
- V2. Unit: a mission started with only `_exit_event` set (no
  `_operator_stop_event`, simulating SIGTERM/stall) still aborts on its own,
  with no explicit `cancel_mission()` call needed.

Covered by `tests/test_mission_cancel.py`
(`test_standby_hotkey_mission_survives_stale_exit_event`,
`test_real_exit_still_aborts_the_mission`). The `mission_loiter` test stub in
`tests/test_mission_loiter.py` (`_loiter_ctrl`, built via `Controller.__new__`)
gained `_operator_stop_event` so its existing tests keep constructing a
Controller shape the new check can actually read.

- V3. Live: reproduced the exact incident on the pre-fix code by reaching
  `GAME_BATTLE`, pressing Backspace once, and confirming standby's log line
  (`"wingman has stopped — MetalStorm is still running and is yours to fly"`).
  Relaunched on the patched code, repeated the same sequence, and pressed
  `'u'`. Result (`logs/preserved_20260906_0513_unarchived.log`):

  ```
  05:08:46,583 STANDBY: wingman has stopped — MetalStorm is still running and is yours to fly...
  05:08:54,157 Controller: 'u' key pressed - starting J20 mission (state=GAME_BATTLE)
  05:08:54,158 Controller: fire_active_weapon - pressing 'f' key for 0.1 seconds
  05:08:55,310 Controller: fire_active_weapon - pressing 'f' key for 0.1 seconds
  05:08:56,422 Controller: fire_active_weapon - pressing 'f' key for 0.1 seconds
  ...
  05:09:13,161 Controller: fire_active_weapon - pressing 'f' key for 0.1 seconds
  ```

  The weapon loop kept firing on its ~1.1 s cadence for 19+ seconds with zero
  occurrences of `"exit requested, aborting mission wait"` or `"mission_j20 -
  cancelled"` — the exact signature that appeared at 50 ms in every pre-fix
  attempt. V1-V3 all satisfied; status Accepted.

## References

- ADR 099 — the nested display lane and standby's Backspace/Backspace-again
  design; the source of `_operator_stop_event` and the reason `_exit_event`
  cannot be cleared on entry to standby
- ADR 094 — the deferred finish-round-then-exit ('z') hotkey; unaffected by
  this change, though see the loose end below
- SAF-007 — "no injected key survives process termination"; D3 preserves this
  for the genuine-exit case
- Evidence: `logs/wingman_20260906_042814.log` (the incident), preserved
  `logs/preserved_20260906_0513_unarchived.log` (the fix, live-validated)

## Loose end observed while validating

`'z'` (finish-round-then-exit) pressed *during standby* logs its normal
"requested — wingman will stop at the next lobby" message but never actually
stops anything: the main loop that reads `_finish_round_event` has already
exited into the standby wait-for-second-Backspace loop by the time standby is
reached, and nothing in that loop consults the flag. The deferred stop is
silently inert for the rest of the session. Not addressed here — standby's
exit path is Backspace-only by design (ADR 099) — but recorded so a future
"I pressed z during standby and nothing happened" report is not rediscovered
as a new fault.
