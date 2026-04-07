# Wingman — AI Collaboration Rules

## ADR Authoring

When creating any ADR under `docs/adr/`, place status, date, and version in a compact table immediately after the ADR title. Use today's actual date and read `WINGMAN_VERSION` from `wingman/main.py`:

```
| Status   | Date       | Wingman Version |
|----------|------------|-----------------|
| Accepted | 2026-03-21 | 1.5.2           |
```

## ADR — Sequential Numbering

Before creating a new ADR, list the files in `docs/adr/` to find the highest existing number and increment by 1. Never guess or reuse a number — gaps and collisions break the sequence across sessions.

## ADR — Performance Changes

Performance ADRs must include actual log excerpts with timing data, not just estimates. ADR 019 is the reference example — before/after timings should come directly from production logs.

## ADR — Superseding Decisions

Do not modify an ADR that has status `Accepted`. If a decision is superseded, write a new ADR and reference the old one. This keeps the decision history intact.

## Diagrams

Always use Mermaid for diagrams in documentation. Never use ASCII text diagrams (no box-drawing characters, no `┌─┐` borders, no `→` arrow art). Wrap all diagrams in a fenced code block with the `mermaid` language tag.

## Lock Release in Finally Blocks

Never use `try: lock.release() except RuntimeError: pass` in finally blocks. Always guard with:

```python
if self._some_lock.locked():
    self._some_lock.release()
```

The swallowed-exception pattern silently leaves the lock held if `release()` fails, permanently blocking future `acquire(blocking=False)` callers.

## Stoppable Daemon Threads

Any long-running daemon thread must be stoppable via a `threading.Event`. Use `event.wait(timeout=interval)` as the loop tick — not `while True: time.sleep(interval)`. The stop event must be set in `cleanup()` before the executor is shut down:

```python
# __init__
self._my_stop = threading.Event()

# thread body
while not self._my_stop.wait(timeout=5.0):
    ...

# cleanup()
self._my_stop.set()
```

## Lock Acquire Timeout on Main-Loop Paths

Any lock that can be held by a background thread must use `acquire(timeout=N)` when called from the main loop. Bare `with lock:` is only safe when both sides run in background threads. Return or skip the cycle gracefully on timeout:

```python
if not self._some_lock.acquire(timeout=5.0):
    logger.warning("lock timeout - skipping frame")
    return cached_result
try:
    ...
finally:
    self._some_lock.release()
```

## Document Heading Format

All new documents (job aids, performance docs, code reviews, ADRs, and any other docs under `docs/`) must begin with a title and a compact status/metadata table immediately after:

```
# <Document Type NNN> — <Title>

| Status   | Date       | Wingman Version |
|----------|------------|-----------------|
| Draft    | 2026-04-07 | 1.6.0           |
```

- Read `WINGMAN_VERSION` from `wingman/main.py` — never guess it.
- Use today's actual date.
- Use `Draft` for new documents; update to `Active` or `Accepted` once reviewed.
- This rule applies to **all** new docs, not just ADRs.

## Code Review Todos

Review files live in `docs/code-review/` and are numbered sequentially (`001-2026-03.md`, `002-…`, etc.). Each file covers one review cycle and is closed (immutable) once all items resolve.

At the start of any session, open the highest-numbered file and check for open items relevant to the current task. When an item is resolved, mark it Resolved in that file. When starting a new review cycle, create the next numbered file in the same directory.
