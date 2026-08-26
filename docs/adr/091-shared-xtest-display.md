# ADR 091 — Shared XTest Display for Key Injection

| Status   | Date       | Wingman Version |
|----------|------------|-----------------|
| Accepted | 2026-08-25 | 1.8.5           |

**Accepted 2026-08-25** after six post-fix sessions spanning 3.0 to 9.0 hours.
See "Validation" below.

## Context

Performance 008 tracked a long-session memory leak for three days. It
established that the growth is **live allocation, not fragmentation**, refuted
two hypotheses (glibc arena fragmentation, EasyOCR reader churn), and left one
fork open: Python-side retention or native retention below the Python object
graph. It insisted the fork be measured rather than guessed.

The heap census added for that measurement answered it on the first run.

`_linux_key_event` opened a **throwaway `Xlib.display.Display` for every key
press and every key release**:

```python
for attempt in (1, 2):
    d = _xdisplay.Display(display_name)     # new X11 connection, per event
    try:
        _xtest.fake_input(d, event_type, keycode)
        d.sync()
    finally:
        d.close()
```

Every `Display.__init__` rebuilds the Xlib resource classes:

```python
# Xlib/display.py:121
self.display.resource_classes[type_] = type(origcls.__name__, (origcls, object), dict)
```

`close()` closes the socket. It cannot undo the class construction, and the
retained class objects survive `gc.collect()`.

## Evidence

**Live session, 2026-08-23 10:55-12:41 (1h 46m, 17 missions).** Retained bytes
attributed to `Xlib/display.py:121` by tracemalloc:

| min | 5 | 15 | 30 | 45 | 60 | 75 | 90 | 105 |
|-----|---|----|----|----|----|----|----|-----|
| MB | 4.4 | 30.9 | 122.0 | 229.6 | 433.5 | 621.8 | 934.6 | **1277.2** |

- 307,144 live blocks added and never released.
- **728 MB/h from this one site**, which is **96% of all post-warm-up live-heap
  growth** that session.
- Post-warm-up, tracemalloc's delta tracks `mallinfo2`'s almost exactly
  (+82/+82, +87/+88, +118/+118, +109/+109 MB) — so the retention is Python-side
  and the native branch of the Performance 008 fork is closed.

**Controlled measurement**, 400 open/close cycles with `gc.collect()` before
every reading: **~16.2 KB retained per `Display()` construction**, top site
`Xlib/display.py:121`. 1,277 MB ÷ 16.2 KB implies ~80,700 constructions, which
matches the session's logged control activity (1,538 `fire_active_weapon`, 212
`climb`, 158 `eject_and_dive`, 120 `deploy_flares`, each a press *and* a
release).

**Not a latency problem.** `Display()` open+close measures 0.83 ms median /
3.29 ms p95, roughly 0.4% of wall clock. The cost is memory, not speed, and the
incoming-to-flare regression should not be attributed to it.

**Frames are not the leak.** `capture.py:303` (the 6.9 MB frame grab) plateaued
around 70 MB and left the top table. The capture buffer is bounded.

## Decision

Open **one** XTest display per process and reuse it.

- `_shared_xtest_display(display_name)` opens on first use and caches.
- `_drop_shared_display()` closes and forgets, so the next call reconnects.
- Any injection failure drops the connection before the retry. A half-dead
  connection must never carry the release half of a press/release pair — that
  is the failure that leaves a key logically held in the X server for the rest
  of the session, since XTest key state is server-side and does not die with
  the client.
- A module-level `RLock` serialises injection. **This is newly required**: the
  per-call Displays were providing thread isolation for free, and injection
  comes from the main loop, the behaviour tree, and hotkey callbacks. Xlib
  `Display` objects are not safe for concurrent use.

Measured with real Xlib, 400 iterations, `gc.collect()` either side:

| | retained | per event | top site |
|---|---|---|---|
| before | 5.84 MB | 14.9 KB | `Xlib/display.py:121` |
| after | 0.31 MB | 0.79 KB | tracemalloc's own overhead |

**94.7% reduction**, and the offending site leaves the table.

## Validation — accepted 2026-08-25

Every condition this ADR set for itself is met.

| condition (from Consequences, below) | outcome |
|---|---|
| "the remaining ~4% is unattributed and needs its own session to characterise" | **characterised: it is not 4%, it is zero.** The predicted residual was ~38 MB/h; six sessions measure −4 to +3 |
| "limits should not be relaxed until a long session confirms the new curve" | confirmed at a 9.02h window; ADR 090's guard left untouched |
| "contention is not expected to be observable" | not observed across ~30h of real sessions — 0 errors, OCR flat, reaction latency normal |

| | sessions | window | post-warm-up `mi_use` |
|---|---|---|---|
| pre-fix | 6 | 2.1–6.8 h | **+952 to +1,666 MB/h** |
| post-fix | 6 | 3.0–9.0 h | **−4 to +3 MB/h** |

The decisive session is 2026-08-25 08:14, 9h 12m — the same duration band as
Performance 008's founding evidence, which recorded respawn-crop OCR rising to
**4.85 s by hour nine** and RSS reaching 13.2 GB with 14.8 GB of system swap:

| | pre-fix, 2026-08-20 (8h12m) | post-fix, 2026-08-25 (9h12m) |
|---|---|---|
| peak RSS | ~13,200 MB | **2,915 MB** |
| system swap | 4.3 → 14.8 GB | 2,366 → 2,365 MB |
| OCR median | 0.24 → 4.85 s | 0.23 → **0.25 s** |
| OCR p95 | 0.38 → 16.9 s | 0.35 → **0.44 s** |

That the OCR symptom disappears too matters: it is an independent measurement
path from `mallinfo2`, and Performance 008 was opened because of the OCR
degradation rather than because of memory.

### Known at acceptance

- **The drop-and-reconnect path has never fired in production.** No display has
  died mid-session, so that branch is covered by unit tests only. It is an error
  path that cannot be forced, so waiting for it would mean waiting indefinitely.
- **`_linux_click` still constructs per call** — see "Not done". Tracked in
  Roadmap 002 so it survives this ADR becoming a historical record.

## Consequences

- The dominant term in the Performance 008 leak is removed. The remaining ~4%
  is unattributed and needs its own session to characterise; this ADR does not
  claim the leak is finished.
- ADR 090's memory guard stays. It is now a backstop rather than the only
  defence, and its limits should not be relaxed until a long session confirms
  the new curve.
- Injection is serialised where it previously ran concurrently per-thread. Each
  injection is sub-millisecond once connected — far cheaper than the 0.83 ms
  connect it replaces — so contention is not expected to be observable, but it
  is a real behavioural change and `test_concurrent_injection_is_serialised`
  pins it.
- The connection now lives for the session. A display that dies mid-session is
  handled by the drop-and-reconnect path rather than by the next call happening
  to open a fresh connection.

## Not done

`_linux_click` (`input_linux.py:114`) has the identical per-call `Display()`
pattern. It is deliberately left alone: clicks number in the low hundreds per
session (~1-2 MB, negligible beside 1,277 MB), and its sequence contains
multi-hundred-millisecond sleeps that must not be held under the injection
lock. Worth fixing, but not as part of a change to the safety-critical key path.

Carried forward as **Roadmap 002** — an Accepted ADR is a historical record, so
a live to-do left only in this section would quietly stop being one.

## References

- Performance 008 — the investigation, the two refuted hypotheses, and the
  census that produced the attribution above
- ADR 090 — memory guard, the mitigation this leak forced
- `wingman/heap_census.py` — the instrument; `gc_census` is off by default
  because the gc walk stalls the tick for seconds
