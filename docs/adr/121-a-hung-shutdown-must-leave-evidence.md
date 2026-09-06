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
