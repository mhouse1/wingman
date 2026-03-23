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

## Code Review Todos

When completing work that addresses an item in `docs/code-review-todos.md`, update that file to mark the item resolved. Check it at the start of any session to see if pending items are relevant to the current task.
