# ADR 121 — A Hung Shutdown Must Leave Evidence

| Status | Date       | Wingman Version |
|--------|------------|-----------------|
| Draft  | 2026-09-05 | 1.8.8           |

## Context

Wingman took SIGTERM on 2026-09-05 at 08:02 and did not exit:

```
08:02:03  last log line of any kind
08:07:31  still alive, 5.5 minutes later, no further output
```

It never wrote `Exit requested, shutting down`. It had to be SIGKILLed, so no
session summary and no stats JSON were written, and the next start rotated its
log away.

**The overnight 01:10 session left exactly that signature** — no archived log,
no stats artifact, nothing to review. Roughly six hours of the soak data ADR 106
needs, gone without a trace of why.

A hung shutdown is worse than a crash. A crash leaves a traceback. This leaves
nothing at all, and it destroys the session's record on the way out.

It is **not reproducible on demand**: a fresh session SIGTERMs cleanly in about
3 seconds, writing its summary and stats. Two attempts to attribute it failed:

- **A thread leak.** 325 OS threads looked damning against the healthy session's
  "threads 4->24" — until the metric was checked. The resource monitor reports
  `threading.active_count()`, which counts PYTHON threads; `/proc/<pid>/task`
  counts OS threads, including the native pools that 13 EasyOCR readers bring.
  325 is normal and it was stable, not growing. **The comparison was between two
  different quantities, which is not a comparison.**
- **A stack dump.** `py-spy dump` needs elevated permissions this session does
  not have, and the process was already stuck.

So the cause is unknown, and guessing at it would be exactly the mistake this
project keeps recording. Instrument instead.

## Decision

**D1. A shutdown watchdog armed as cleanup begins.** If cleanup has not finished
within `SHUTDOWN_WATCHDOG_S` (90 s), the process dumps every thread's stack and
exits.

**D2. Dump to the LOG FILE, not just stderr.** An unattended soak has no
terminal. The whole failure is that nothing was recorded; writing the evidence
somewhere that is also lost would repeat it.

**D3. `os._exit`, not `sys.exit`.** Normal shutdown is by definition already
stuck at this point, so anything that runs atexit handlers or joins threads
would stick in the same place.

**D4. A daemon timer.** It must not keep a healthy interpreter alive, which
would turn the guard into the bug.

**D5. 90 seconds, not 5.** Cleanup legitimately shuts down an OCR pool, writes
stats and closes the display. The watchdog is for a hang, not for slowness, and
a false force-exit would cost the artifacts it exists to protect.

## Consequences

A hung shutdown now costs 90 seconds and produces a full thread dump naming the
stuck frame, instead of an unbounded hang that must be SIGKILLed.

**This does not fix the hang.** It converts an invisible failure into a
diagnosable one, and guarantees the process actually exits. The next occurrence
should carry the stack that explains it, and the fix belongs in its own ADR.

It does not recover the artifacts of a session that hangs: the summary and stats
are written *during* cleanup, so a stall before them still loses them. Bounding
the stall is the prerequisite for fixing that, not a substitute.

An exit code of 2 now distinguishes "forced after a stalled shutdown" from a
clean 0. Anything reading exit codes should treat 2 as "the session ran, but its
teardown failed".

## The stop that provoked it

The hang followed a **mid-round SIGTERM**. ADR 094 already provides the correct
stop — `z` finishes the round, exits at `GAME_LOBBY`, then closes MetalStorm —
and it was not being used: every stop in this investigation was a signal sent
wherever the aircraft happened to be.

That is now the documented default in the `iterate` skill, with signals demoted
to a fallback for "no round to finish" or an unresponsive process. It also
leaves the game in a state the next session can enter cleanly, rather than one
it has to recover from.

**This is a correlation, not a cause.** A fresh session SIGTERMs cleanly in
about 3 s, so a mid-round signal is not sufficient on its own to produce the
hang. The watchdog stays because the cause is still unknown; using the right
stop reduces how often the question comes up, and does not answer it.

## Fired on its first day — on a healthy standby (see ADR 124)

2026-09-05 10:45:17. Nothing was hung: the dump showed the stop-watchers idle in
their poll loop and the listeners in `record_enable_context`, which blocks by
design. It was STANDBY — an unbounded wait the operator had asked for — and the
watchdog was armed at the top of the same `finally` block. It force-exited the
SAF-010 handback.

The mechanism was right and the SCOPE was wrong; ADR 124 disarms it around
standby and re-arms it for the bounded close. The dump itself did its job: four
frames, immediately readable, question settled in one pass.

## Validation

- **V1.** A healthy shutdown is not forced.
- **V2.** A stalled shutdown is forced, with exit code 2.
- **V3.** Thread stacks are dumped before exiting, for **all** threads — the
  hang is in whichever thread is stuck, and a single-thread dump would likely
  miss it.
- **V4.** The timer is a daemon and cannot itself block exit.
- **V5.** The dump reaches the log file, not only stderr.
- **V6 — live. MET 2026-09-05**, though on a wait that was not a hang: the dump
  named four frames and settled the question immediately. See ADR 124.

## References

- ADR 106 — the soak data the overnight hang destroyed
- ADR 094 — `z`, the finish-the-round stop that should have been used
- ADR 119 — the same day's other unbounded wait, in the display probe
- `wingman/main.py` — `_arm_shutdown_watchdog`
- `tests/test_shutdown_watchdog.py` — V1-V5

## An ERROR on every clean shutdown (2026-09-06)

This ADR's premise is that a shutdown must leave evidence. The inverse also has to
hold: **evidence that always appears carries none.**

Measured across every session that tore the nested display down — 2026-09-05
22:35, 2026-09-06 04:28 and 05:13 — the only `[ERROR]` in an otherwise clean log
arrived one millisecond after wingman's own teardown line:

```
04:28:13,803 [INFO]  Nested display: closing Xwayland for :3 (pid(s): 1367144)
04:28:13,804 [ERROR] XKey listener thread died: Display connection closed by server
04:28:13,804 [DEBUG] XKey: d_rec.close() failed during reconnect
04:28:13,804 [INFO]  XKey: reconnecting display in 3s (attempt 1)
```

The hotkey listener blocks on an XRecord connection to `:3`. Closing that display
drops the connection, and the listener could not distinguish it from a crash — so
it logged ERROR and scheduled a reconnect to the display wingman was in the middle
of killing. The 05:06 session, which exited without closing `:3`, logged **zero**
errors. The correlation is exact.

Nothing broke: the process exits before the 3 s timer fires. The cost is that
`XKey listener thread died` fires on every clean shutdown and therefore cannot be
used to notice the listener dying for a reason that matters — which is precisely
the signal this ADR exists to protect.

**Decision. The teardown declares itself, per display.** `close_nested_display`
calls `input_linux.expect_display_close(display)` **before** the SIGTERM that
causes the disconnection; the listener then logs the exit at INFO and does not
reconnect.

Per display, deliberately, not one global "shutting down" flag: the operator's
`:0` listener dying during shutdown is still a real failure, and a blanket flag
would suppress exactly the case worth keeping. The declaration is also wrapped so
that a failure to record it cannot block the shutdown it describes — instrumentation
inside a shutdown path must never be able to hang that path.

Nothing is declared when no server was found, so a display that was never torn
down cannot have a later genuine failure excused.

### Validation

- V1. Unit: a declared display is expected; an undeclared one is not.
- V2. Unit: the declaration lands **before** the SIGTERM, sampled inside a fake
  `os.kill` — driven through the real `close_nested_display`, since the ordering
  is the entire fix and a re-implementation would assert only itself.
- V3. Unit: a display with no server running is not declared.
- V4. Unit: a raising declaration does not break the shutdown.
- V5. Live: **satisfied 2026-09-07.** The 1h52m session ending 09:32 tore `:3`
  down and logged the teardown at INFO with no error anywhere in the run:

```
09:32:48,545 [INFO] Nested display: closing Xwayland for :3 (pid(s): 3098978)
09:32:48,546 [INFO] XKey: :3 closed as expected — listener stopping
09:32:48,796 [INFO] Nested display: :3 closed
```

  Total `[ERROR]` count for the session: **0**. The same three lines previously
  produced `[ERROR] XKey listener thread died` one millisecond after the first,
  followed by a reconnect scheduled against the display being destroyed. ERROR is
  a usable signal again.

Covered by `tests/test_expected_display_close.py` (7 tests).

## SIGHUP was never handled (2026-09-08)

This ADR's premise — a shutdown must leave evidence — assumed the process
would at least *reach* the shutdown path. `wingman/main.py` caught `SIGTERM`
and routed it through `exit_requested` so cleanup runs; it never caught
`SIGHUP`. SIGHUP's default action is immediate termination: no exception, no
`Exit requested` log line, nothing `_arm_shutdown_watchdog` could ever see,
because the watchdog only arms once `exit_requested` is already set.

**Observed 2026-09-08 04:31.** An 8h57m overnight soak — 92 missions, the
largest single session ADR 106 has recorded — ended with `wingman.log`
stopping mid-stream, one line after a routine periodic resource-summary
write. No traceback, no `Exit requested`, no session-summary block, no
`Nested display: closing Xwayland for :3` line. The process was simply gone;
`ps` showed no `wingman.main`, and the orphaned Xwayland `:3` window it had
been hosting outlived it by roughly 17 minutes until the operator noticed and
asked why the auto-close (ADR 105) hadn't fired. It hadn't fired because
nothing was left alive to fire it — a controlling-terminal hangup (the most
common SIGHUP source) is consistent with the log's exact stop-mid-write
signature and the absence of any OOM-kill or crash trace in the system
journal for that window.

This is a narrower, more severe sibling of the SIGTERM-hang this ADR already
covers: a SIGTERM-hang at least leaves the watchdog's 90-second window to
dump thread stacks; an uncaught SIGHUP leaves nothing at all, not even that.

**Decision.** `SIGHUP` gets the identical handler `SIGTERM` already has —
`signal.signal(signal.SIGHUP, lambda _sig, _frm: exit_requested.set())`,
registered in the same `try` block right beside it (`wingman/main.py:494`).
A hangup now takes the same graceful path SIGTERM does: `exit_requested` is
set, cleanup runs, the ADR 121 watchdog is available if cleanup itself
stalls, and ADR 099/105's nested-display teardown gets to run instead of
being skipped by a process that no longer exists to run it.

No new test was added — there is no existing test that exercises the
SIGTERM handler's *registration* either (the existing SIGTERM-referencing
tests in `tests/test_shutdown_watchdog.py` and `tests/test_expected_display_close.py`
cover what happens once `exit_requested` is set, which is unchanged and
already covered regardless of which signal set it); SIGHUP reuses that exact
code path.

### Validation

- V1 — live, still open. The next terminal-closed / SIGHUP-source session
  should show a normal shutdown sequence (`Exit requested`, session summary,
  nested-display teardown) instead of the log stopping mid-write. Not yet
  observed, since the fix landed after the incident that motivated it.

## A Ctrl-C racing the second Backspace silently dropped the close (2026-09-08)

A third variant, found within the hour of the SIGHUP fix above — and this one
is not a silent death. The log is complete and clean throughout; the bug is
in which *documented* path it took.

**Observed 2026-09-08 05:02:24.** Standby's wait loop
(`wingman/main.py:1614-1623`, `while not ctrl.wait_for_close_all(...): pass`)
caught a `KeyboardInterrupt` one millisecond *before* the hotkey callback's
own "second Backspace" log line landed:

```
05:02:24,060  STANDBY: interrupted — leaving MetalStorm running
05:02:24,060  Controller: all keyboard hooks deregistered
05:02:24,061  Controller: Backspace again — closing MetalStorm and the nested display
05:02:24,062  XKey: stop-watcher could not disable the record context: 'NoneType' object is not subscriptable
```

The interrupt branch treats "leave everything up" as unconditional — it
never checks whether the close request had, in fact, also arrived. Here it
had, by the time the `except` block ran, but the code did not look. Wingman
then exited normally (this is not a hang or a crash — it is the *documented*
Ctrl-C behavior, "leaves everything up," working exactly as specified) having
never called `_close_session()`. The operator closed MetalStorm by hand some
minutes later, per the standby message's own instructions; nothing was left
running to apply ADR 105's "the game died, close the display anyway" rule.
The DEBUG line at 05:02:24,062 is a second-order effect of the same race —
`release_hotkeys()` (the `finally` block) tearing down the XRecord context
concurrently with the callback's own daemon `_stop_watcher` thread still
running on it — not itself the cause.

**Decision.** Check `ctrl.close_all_requested()` inside the `except
KeyboardInterrupt` handler. If the second Backspace had already landed by the
time the interrupt is handled, close down anyway rather than discarding the
request:

```python
except KeyboardInterrupt:
    if ctrl.close_all_requested():
        logger.info("STANDBY: interrupted, but the second Backspace "
                    "had already arrived — closing down anyway")
        _arm_shutdown_watchdog()
        _close_session()
    else:
        logger.info("STANDBY: interrupted — leaving MetalStorm running")
```

This does not require the two events to be ordered correctly — it only needs
`close_all_requested()`'s answer at the moment the interrupt is handled,
which is unambiguous regardless of which one the wait loop observed first.

No new test: same reasoning as the SIGHUP fix above — this is inline in
`main()`, not an extracted, mockable function, and no existing test exercises
this block directly.

### Validation

- V1 — the race itself still unexercised; the surrounding path confirmed
  clean. The 13h05m session immediately following this fix (2026-09-08
  06:12-19:18, 134 missions) ended via a normal, non-racing second Backspace
  and showed the complete expected sequence with no regression:
  `STANDBY: second Backspace — closing down` → `Game shutdown: closing
  Metalstorm.exe` → `Nested display: closing Xwayland for :3` → `XKey: :3
  closed as expected` → `Nested display: :3 closed`, zero `[ERROR]` lines.
  That confirms the `except KeyboardInterrupt` change didn't disturb the
  ordinary path. It does **not** validate the fix itself — no Ctrl-C raced
  the Backspace in this session, so the new `close_all_requested()` branch
  was never entered. Still open: a session where the two actually race.

## SIGTERM had no effect at all on a live, ticking process (2026-09-11)

A fifth variant, and the first where the loop itself was never in doubt.

**Observed 2026-09-11 06:44-06:52.** The nested Xwayland server on `:3` died
out from under a live session — gone from `/proc` and from `/tmp/.X11-unix`
between two ticks, with no OOM-kill or segfault in `dmesg`/`journalctl` and no
exit trace in the server's own log (`/tmp/wingman-nested-display.log`, last
written at session start). Cause unknown; not this ADR's concern. What
followed is.

Wingman kept running — the tick loop, `Controller.fire_active_weapon`,
`padlock_camera`, and the XKey listener's own reconnect-every-3s loop all
continued firing normally, once a second, for the next several minutes,
`Linux key event for 'f' failed after retry` on every attempt since the
display no longer existed. This is measured, not inferred: `wingman.log` shows
continuous, evenly-spaced output throughout, unlike the original 2026-09-05
incident's signature ("stopped logging on the same second") and unlike the
2026-09-08 SIGHUP incident (log stops mid-write). The process was never stuck
— it was doing useless work, correctly, forever.

`kill -TERM` was sent directly to the interpreter PID (confirmed via `ps`,
not the `uv run` shim). `exit_requested.is_set()` sits unconditionally at the
top of `main.py`'s `while True:` (line 1060), checked once per tick, before
anything else. The tick kept running at its normal ~1 s cadence for 75+
seconds afterward. It never logged `Exit requested, shutting down`. No
`SHUTDOWN WATCHDOG` line appeared either, for the structural reason below.
`kill -KILL` was sent after that wait; the process died immediately and
cleanly (confirmed by the `uv`/`make rd` shim reporting exit 137 one second
later), so nothing downstream of the signal was itself hung — the interpreter
was fully responsive to SIGKILL throughout.

**What is not known.** Whether the SIGTERM was delivered to the process at
all, delivered but masked for the main thread, or delivered and handled (the
lambda ran, `exit_requested.set()` executed) yet the *same* `threading.Event`
object somehow read `False` from the loop — cannot be distinguished
post-mortem with the process already gone. Guessing at which would repeat
the mistake ADR 121's original incident already recorded. Instrumentation
belongs in the signal handler itself, not in a theory about it.

**The structural gap this exposes.** `_arm_shutdown_watchdog()` is called
from the `finally:` block wrapping the main loop (`main.py:1459`) — reached
only once the loop actually breaks out of `while True:`, which requires
`exit_requested.is_set()` to have been true at the top of some tick. Every
variant this ADR has recorded so far — the original hang, the SIGHUP gap, the
Ctrl-C race — is a failure *after* that point, or a signal that bypassed
`exit_requested` entirely (SIGHUP's default disposition). This is the first
one where the signal that is supposed to *set* `exit_requested` produced no
observable effect while the loop kept running in full health. The 90 s
watchdog cannot fire for a failure that never reaches the code that arms it.

**Decision.** Not yet fixed. Recorded here per this ADR's own rule — the next
occurrence should carry more than a correlation. The candidate fix is an
independent timer armed in the signal handler itself (or immediately after
`signal.signal(...)` registration), separate from `_arm_shutdown_watchdog`,
that forces exit if `exit_requested` is still unset some bounded time after
the OS delivered the signal — closing the one path left where a termination
request can be silently absorbed. See SAF-015 (`docs/requirements/001-safety.sdoc`)
for the property this gap violates.

### Validation

- V1 — open. Next SIGTERM-during-XKey-reconnect-storm session should either
  reproduce this (and get a signal-handler-level instrument added) or show
  clean acknowledgment, narrowing whether the reconnect storm is a factor or
  coincidental to this specific occurrence.

