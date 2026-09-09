# ADR 124 — The Watchdog Must Not Bound a Deliberate Wait

| Status | Date       | Wingman Version |
|--------|------------|-----------------|
| Draft  | 2026-09-05 | 1.8.8           |

## Context

ADR 121 armed a shutdown watchdog: if cleanup has not finished in 90 s, dump
every thread's stack and force an exit. It fired on its first day:

```
10:43:46  Backspace — ending wingman; MetalStorm stays up for manual control
10:43:47  STANDBY: wingman has stopped — MetalStorm is still running and is
          yours to fly. Press Backspace again to close MetalStorm and exit.
10:45:17  SHUTDOWN WATCHDOG: cleanup still running after 90s — dumping all
          thread stacks and forcing exit
```

**Nothing was hung.** The dump proves it:

```
Thread ...  input_linux.py:647 in _stop_watcher     <- self._stop.wait(timeout=0.5)
Thread ...  input_linux.py:647 in _stop_watcher
Thread ...  record.py:234 in enable_context         <- the event pump, blocking by design
Thread ...  record.py:234 in enable_context
```

Line 647 is the stop-watcher's poll loop, idle and healthy. The listener threads
sit in `record_enable_context`, which blocks until the context is disabled —
that is what an X event pump does. The hotkey listener was deliberately still
alive, because STANDBY exists so the operator can fly manually and press
Backspace a second time.

STANDBY is `while not ctrl.wait_for_close_all(timeout=1.0): pass` — an
**unbounded wait by design**, for as long as the operator wants. And it lives
inside the same `finally:` block where ADR 121 armed the watchdog at the top.

So the watchdog killed the SAF-010 flyable handback after 90 seconds. ADR 121's
own D4 said "it must not keep a healthy interpreter alive, which would turn the
guard into the bug" — the guard became the bug in the other direction: it
*ended* a healthy interpreter.

The mechanism worked exactly as specified. It was armed over the wrong region.

## Decision

**D1. Disarm the watchdog before the standby wait.** A watchdog bounds a stall.
Standby is not a stall, and no timeout can tell them apart from the inside.

**D2. Re-arm for the close after the second Backspace.** That close IS bounded,
and a hang there is exactly what ADR 121 exists to catch. Disarming for standby
must not disarm shutdown altogether.

**D3. Arming replaces any previous timer.** The standby path arms twice; a
second timer that left the first running would fire on the old deadline.

**D4. Cancelling is idempotent.** It is called on paths that may never have
armed.

## Consequences

Standby works again, and a stalled close is still caught.

The watchdog now covers the bounded parts of shutdown and not the operator wait
in the middle of it. The `finally:` block does both jobs, which is why arming at
its top looked right and was not: **the region a timeout protects has to be
chosen by what the code is waiting FOR, not by where the block begins.**

ADR 121's V1 asserted that a healthy shutdown is not forced — and it passed,
because its fixture returned promptly. The standby path never returns. A test
whose "healthy" case is always fast cannot distinguish "finishes normally" from
"waits forever on purpose".

## What ADR 121 still bought

The dump was correct and immediately readable, and it named four frames in
enough detail to settle the question in one pass. Without it this would have
been another SIGKILL and another session with no explanation.

It also, for the first time, gives a concrete answer for a class of hang that
had been guessed at all week: the XKey listener blocks in `enable_context` and
is released only when a watcher calls `record_disable_context`. If `_stop` is
never set, the listener never unblocks. That is a real dependency worth knowing,
and it was invisible before this dump.

The overnight 01:10 hang remains unexplained — it wrote **no** summary and no
stats, where this standby wrote both before parking, so they are not the same
event.

## Validation

- **V1.** The watchdog can be cancelled, and a cancelled one does not fire.
- **V2.** Cancelling twice, or with nothing armed, is harmless.
- **V3.** Arming twice leaves exactly one live timer.
- **V4 — live.** A standby survives well past the watchdog timeout, and the
  close after a second Backspace is still bounded. Not yet observed.

## References

- ADR 121 — the watchdog, correct in mechanism and wrongly scoped
- ADR 099 — STANDBY, the first-Backspace handback
- SAF-010 — the flyable handback this restores
- `wingman/main.py` — `_arm_shutdown_watchdog`, `_cancel_shutdown_watchdog`
- `tests/test_shutdown_watchdog.py` — V1-V3
