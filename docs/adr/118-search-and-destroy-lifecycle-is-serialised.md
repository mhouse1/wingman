# ADR 118 — The Search-and-Destroy Lifecycle Is Serialised

| Status | Date       | Wingman Version |
|--------|------------|-----------------|
| Draft  | 2026-09-05 | 1.8.8           |

## Context

`mission_j20` died mid-session on 2026-09-05:

```
01:05:11,423  search_and_destroy padlock loop stopped
01:05:11,423  search_and_destroy weapon loop stopped
01:05:11,424  search_and_destroy padlock loop started
01:05:11,424  [ERROR] Controller: mission_j20 failed
              RuntimeError: cannot join thread before it is started
01:05:11,426  search_and_destroy weapon loop started
```

`start_search_and_destroy_loop` assigns both `Thread` objects and only then
starts them:

```python
self._sdl_padlock_thread = threading.Thread(...)
self._sdl_weapon_thread  = threading.Thread(...)
self._sdl_padlock_thread.start()
self._sdl_weapon_thread.start()
```

A concurrent `stop_search_and_destroy_loop` that lands between the second
assignment and the second `start()` calls `join()` on a thread that has never
run. `Thread.join` raises `RuntimeError` in that state, the exception escaped
into `_mission_runner`, and the mission ended.

The window is a few microseconds wide and it was hit **twice** in one session,
because the conditions concentrate it: the crash sits in a sixteen-minute tail
where the HUD was gone (health OCR silent for 1001 s) and `Disengage` was
cycling the loops repeatedly. Rare races become common under churn.

`if self._sdl_weapon_thread:` looks like a guard and is not one. It tests that
the attribute is set, which is exactly the state the race creates.

## Decision

**D1. One lifecycle lock covering both start and stop.** The bug is not the
join; it is that a half-built start is observable. Guarding only the join would
leave a stop that sets `_sdl_stop` while a start is mid-flight, producing loops
that exit immediately for no visible reason.

**D2. `acquire(timeout=2.0)`, never a bare `with`.** Both entry points are
reachable from the tick path and from mission threads, which is precisely the
case the project's lock rule names. A start that cannot get the lock logs and
returns; a stop that cannot get it leaves the loops running and says so.

**D3. Release with the `locked()` guard**, per the project rule — never a
swallowed `RuntimeError`.

**D4. Guard each join with `is_alive()`.** Defence in depth behind D1, and it
states the real precondition: `is_alive()` is False both for a finished thread
and for one that was never started, and `join()` is only valid for the former.

**D5. Clear the attribute whether or not the join ran.** The old code cleared it
only on the joined path, so a thread that failed to join stayed referenced and
the next start saw `padlock_alive` on a dead object.

## Consequences

Start and stop can no longer interleave, so the loops' state is always either
fully built or fully torn down as seen from outside.

A start can now be **skipped** under contention rather than queued. That is the
right trade for a loop that the tick re-requests every cycle — the next tick
starts it — and it is logged rather than silent.

This says nothing about why `Disengage` was cycling the loops every tick for
sixteen minutes with no HUD. That churn is what made a microsecond race
reproducible, and it is worth its own look; a stable system would not have
exercised this path hard enough to find the bug.

## Validation

- **V1.** Stopping a thread that was assigned but never started does not raise.
- **V2.** Stopping a *running* loop still joins it — the guard must not turn the
  stop into a no-op.
- **V3.** A second stop is harmless.
- **V4.** Every stop path releases the lock, including the early return. A leaked
  lock would make every later start silently skip.
- **V5.** While the lock is held, a concurrent stop declines rather than reaching
  into half-built state.
- **V6 — live.** No `cannot join thread before it is started` in a session of
  comparable length. Not yet observed.

## References

- CLAUDE.md — the lock-timeout and `locked()`-release rules this follows
- ADR 024 — Disengage, whose churn concentrated the race
- `wingman/controller.py` — `start_search_and_destroy_loop`,
  `stop_search_and_destroy_loop`
- `tests/test_search_and_destroy_lifecycle.py` — V1-V5
