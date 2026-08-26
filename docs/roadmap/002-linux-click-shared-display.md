# Roadmap 002 — Share the XTest Display for Mouse Clicks

| Status | Date       | Wingman Version |
|--------|------------|-----------------|
| Draft  | 2026-08-25 | 1.8.6           |

## Why this exists as its own entry

ADR 091 fixed the per-call `Xlib.display.Display()` construction in the **key**
path and deliberately left the **click** path alone. That was the right call at
the time: clicks number in the low hundreds per session against roughly 80,000
key events, and the click sequence contains multi-hundred-millisecond sleeps
that must not be held under the injection lock.

ADR 091 is now `Accepted`, and an Accepted ADR is a historical record rather
than a work list. A to-do left only in its "Not done" section would quietly stop
being one, which is exactly how the original defect survived two months.

## The defect

`_linux_click` (`wingman/input_linux.py:114`) opens a fresh `Display` per call:

```python
d = _xdisplay.Display(display_name)
_xtest.fake_input(d, _X.MotionNotify, x=x, y=y)
...
```

Every construction retains ~16.2 KB permanently — `close()` releases the socket
but cannot un-create the resource classes `Display.__init__` builds. Measured
2026-08-23 over 400 open/close cycles, surviving `gc.collect()`.

## Scale — why it is a roadmap item and not a bug

| | constructions per session | retained |
|---|---|---|
| key path (fixed by ADR 091) | ~80,000 | 1,277 MB in 105 min |
| click path (this entry) | low hundreds | **~1–2 MB per session** |

Negligible against a 2.7 GB footprint, and invisible beside the game's ~165 MB/h
(Anomaly 002). This is correctness tidying, not a performance problem.

## What makes it non-trivial

The click sequence is not a single injection. It moves the pointer, syncs,
sleeps 50 ms, then presses and releases with further sleeps and a 500 ms gap
between repeats. Reusing the shared connection means deciding how the injection
lock interacts with that:

- **Holding the lock for the whole sequence** blocks key injection for up to
  several hundred milliseconds — unacceptable on a 1.5 s tick that also has to
  fire flares.
- **Taking the lock per `fake_input`** preserves today's concurrency exactly
  (the separate connections already allowed interleaving), and is the likely
  answer, but it is a real interleaving decision rather than a mechanical
  substitution.

That is why ADR 091 declined to fold it into a change to the safety-critical key
path, and why it wants its own small ADR rather than a drive-by edit.

## Guard already in place

`tests/test_handle_construction_sites.py` (ADR 092) pins `_linux_click` as an
**approved** construction site with its justification. If this is fixed, that
registry entry must be removed in the same change, or the guard's
`test_registry_has_no_stale_entries` will fail — deliberately, so the two cannot
drift apart.

## Done when

- `_linux_click` uses `_shared_xtest_display`, with per-operation locking.
- Its entry is removed from `_APPROVED_SITES`.
- A test asserts N clicks open at most one connection, mirroring
  `test_repeated_key_events_open_exactly_one_display`.
- Existing click behaviour is unchanged — the lobby PLAY click path is how
  every mission starts.

## References

- ADR 091 — the key-path fix, its evidence, and the "Not done" section this
  entry carries forward
- ADR 092 — the source-site guard that currently records this site as approved
- Research 009 — the generalised defect class
