# ADR 119 — The Display Probe Must Be Bounded

| Status | Date       | Wingman Version |
|--------|------------|-----------------|
| Draft  | 2026-09-05 | 1.8.8           |

## Context

`make rd` hung on 2026-09-05 07:09 with **no output at all** — not a failure
message, not a timeout, nothing. Twice, reproducibly.

The stall was in `nested-display.py setup`, before its first `print`:

```
pid=325629 state=Sl elapsed=02:04
wchan: futex_do_wait
```

`start()` opens with `if display_is_up(display)`, and `display_is_up` was:

```python
d = xdisplay.Display(display)   # NO timeout
d.close()
```

`Xlib.display.Display()` blocks until the X handshake completes. Xwayland `:3`
was alive — pid 324709, the game still on it — and accepting connections
without answering. A direct probe confirmed it: an 8-second `timeout` had to
kill the connect.

So the harness had three states and code for two. A display can be **up**,
**down**, or **wedged**, and a wedged one is indistinguishable from a healthy
one at connect time — right up until the caller never returns.

`start()`'s own comments show the author had thought about a server that
"does not answer" and even prints that message for the stale-lock case. The
probe that runs first could not reach it, because it could not return.

This is the failure mode that matters most for an unattended soak: not a crash,
which is loud, but a hang, which looks exactly like work in progress. It cost
the session that ADR 106 needed.

## Decision

**D1. `probe_display` returns "up", "down" or "wedged".** Three states, because
there are three, and the caller's correct response differs for each.

**D2. Bounded by a thread join (`DISPLAY_PROBE_TIMEOUT_S`, 5 s).** The connect
runs on a daemon thread and the caller joins with a timeout.

**D3. The stuck thread is allowed to leak, deliberately.** It is blocked in a
syscall and cannot be cancelled from Python. `daemon=True` keeps it from
holding up interpreter exit, and this runs in a short-lived CLI process. The
alternative — a subprocess per probe — costs an interpreter start on every
`make` invocation to tidy up a thread that dies with the process anyway.

**D4. `display_is_up` reports a wedged server as NOT up.** It answers "can I use
this display", and a wedged one cannot be used. Calling it usable is what
produced the hang.

**D5. A wedged display fails fast and non-zero, naming the owner pids.** It
cannot be used and cannot be replaced by starting a second server on the same
display, so there is nothing to do but tell the operator to kill it.

## Consequences

A wedged display now costs 5 seconds and an actionable message instead of an
unbounded hang.

`make rd` will now **fail** in a situation where it previously appeared to be
working. That is the point, and it is the better failure: exit 1 with the owner
pid beats a process that make waits on forever.

The probe adds up to 5 s to a `make` prerequisite in the wedged case only —
healthy and absent displays return at connection speed, as before.

This does not explain why Xwayland wedged. The server was killed and the lane
restarted cleanly; if it recurs, the Xwayland log is the next place to look and
the cause belongs in its own ADR.

## Validation

- **V1.** A connect that never returns yields "wedged" and returns promptly.
- **V2.** A live display reads as "up"; `display_is_up` is True.
- **V3.** An absent display reads as "down"; `display_is_up` is False.
- **V4.** A wedged display is not reported as up.
- **V5.** `start()` on a wedged display returns 1 and spawns nothing.
- **V6.** `start()` on a running display still spawns nothing — the original
  idempotence guarantee, re-pointed at the new seam.
- **V7 — live. MET 2026-09-05.** `nested-display.py status` returned promptly
  against the killed display, where it previously hung.

## Also fixed, found on the way

`test_start_leaves_a_running_display_alone` patched `display_is_up`, which
`start()` no longer calls — so the patch stopped intercepting and the test
failed by spawning a real `Popen`. The guarantee it protects is unchanged; the
test now patches `probe_display`. **A test coupled to an internal seam breaks
when the seam moves, even when the behaviour it describes is untouched** — the
failure was worth reading rather than silencing.

## References

- ADR 099 — the nested display lane this guards
- ADR 105 — closing the display when the game exits
- ADR 106 — the soak data the hung session cost
- `scripts/nested-display.py` — `probe_display`, `display_is_up`, `start`
- `tests/test_nested_display_probe.py` — V1-V4
- `tests/test_nested_display.py` — V5, V6
