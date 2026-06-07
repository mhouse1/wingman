# Wingman — AI Collaboration Rules

## Sequential Numbering — All `docs/` Subdirectories

Every file created under any subdirectory of `docs/` must have a zero-padded three-digit prefix:

```
001-my-document.md
002-another-document.md
```

Before creating a new file in any `docs/` subdirectory, list the existing files in that folder to find the highest number and increment by 1. Never guess or reuse a number — gaps and collisions break the sequence across sessions.

This applies to: `docs/adr/`, `docs/job-aids/`, `docs/performance/`, `docs/code-review/`, and any future subdirectory under `docs/`.

## ADR — Sequential Numbering

The general sequential numbering rule above applies. Additionally: before creating a new ADR, list the files in `docs/adr/` to find the highest existing number and increment by 1.

## ADR — Performance Changes

Performance ADRs must include actual log excerpts with timing data, not just estimates. ADR 019 is the reference example — before/after timings should come directly from production logs.

## ADR — Superseding Decisions

Do not modify an ADR that has status `Accepted`. If a decision is superseded, write a new ADR and reference the old one. This keeps the decision history intact.

## Command Execution

Always use the project Makefile and bash shell for commands in this repository.

- Prefer `make <target>` for tests, builds, and project tasks.
- Use bash as the execution shell for terminal commands.
- Do not bypass the Makefile with ad-hoc `python`, `pytest`, or shell commands when a Makefile target already covers the task.

## Diagrams

Always use Mermaid for diagrams in documentation. Never use ASCII text diagrams (no box-drawing characters, no `┌─┐` borders, no `→` arrow art). Wrap all diagrams in a fenced code block with the `mermaid` language tag.

Use a compatibility-first Mermaid profile for shared docs:

- Default to syntax that renders across common Mermaid versions/renderers.
- Keep node labels plain-language; do not put symbolic expressions (for example `>`, `<`, `<=`, `>=`, or punctuation-heavy logic) inside node declarations.
- Put equations/conditions in nearby bullets or surrounding prose, not in decision-node text.
- **Forbidden in node labels and edge labels:** `~`, `/`, `+`, `@`, `#`, `;` — these cause silent parse failures across common renderers. Use plain English instead: "and" for `+`, "approx" for `~`, "via" for `/`, "at" for `@`.
- **Edge labels** (`-->|text|` syntax) must contain only plain alphanumeric text, spaces, hyphens, and periods. No other punctuation.
- To include a special character in a node label, wrap the label in double quotes: `A["label text"]` — this requires Mermaid v10+; prefer omitting special characters for maximum compatibility.

Advanced Mermaid features are allowed when both conditions are met:

1. The target renderer/pipeline version is known to support the feature.
2. A simplified fallback diagram (or equivalent textual explanation) is provided for portability.

After creating or editing Mermaid blocks, verify they render in the target environment (not only in one local preview).

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
- **ADRs** must start as `Draft` when first created.
- Update an ADR to `Accepted` only after implementation is complete.

## Code Review Todos

Review files live in `docs/code-review/` and are numbered sequentially (`001-2026-03.md`, `002-…`, etc.). Each file covers one review cycle.

Closed review files are historical records. Do not edit a closed review to change findings, severity, or the narrative assessment after the cycle is complete.

Allowed before closure:
- Add a final resolution summary for items reviewed in that cycle.

After closure:
- Record later status changes in a new review-cycle file, not by rewriting the old file.
- Reference the original item ID and mark the current disposition explicitly: `Resolved`, `Deferred`, `Superseded`, or `Closed as stale`.
- Treat the latest review file as the authoritative source for current disposition.