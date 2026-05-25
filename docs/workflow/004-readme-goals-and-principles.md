# Workflow 004 - README Goals and Principles

| Status | Date | Wingman Version |
|--------|------|-----------------|
| Draft | 2026-05-25 | 1.6.10 |

## Purpose

Define the optimization goals for `README.md` so future updates remain aligned with project intent and avoid drift.

## Primary Goal

`README.md` is the front door for users and contributors.
It should quickly communicate value, current capability, and first-run path while staying trustworthy to the current codebase.

## Audience Priorities

1. New users deciding whether the project is relevant.
2. Contributors trying to run and validate quickly.
3. Maintainers updating docs without re-deriving README strategy.

## Optimization Principles

1. Capture attention quickly.
   - Keep a strong opening statement and visual context (screenshots/GIFs where available).
2. Stay grounded in current reality.
   - Version, commands, and feature claims must match current implementation.
3. Keep vision visible.
   - Include aspirations and roadmap direction, but do not let future plans obscure current behavior.
4. Be action-oriented.
   - Provide copy-paste setup, run, and test commands near the top.
5. Map to deeper docs.
   - Use README as a hub; link to ADRs and job aids for detailed material.
6. Be maintainable.
   - Prefer concise sections that are easy to update when features change.

## Recommended README Structure

1. Hero section
   - Project value statement
   - Current version
   - One or more visuals
2. Vision
   - Short aspirational statement
3. What it does today
   - Concrete current capabilities and loop behavior
4. Quick start
   - Setup and core commands
5. Runtime operation
   - Hotkeys and operating model
6. Replay/testing/performance
   - Current validation commands and status
7. Roadmap
   - Near/mid/future direction
8. Documentation index
   - Links to deeper docs

## Guardrails

- Do not claim features that are not implemented.
- Do not leave stale version numbers after release changes.
- Do not bury setup/run commands below long architecture sections.
- Do not duplicate deep ADR details in full; link out instead.

## README Update Checklist

Before finalizing README edits, verify:

1. `WINGMAN_VERSION` matches `wingman/main.py`.
2. Commands reflect current `Makefile` targets.
3. Hotkeys match `wingman/controller.py` bindings.
4. Replay/testing statements match current implemented paths.
5. Roadmap section aligns with `docs/PROJECT_AI_ROADMAP.md`.
6. At least one section addresses current capabilities and one addresses future direction.

## Copilot Usage Note

When editing `README.md` with Copilot, treat this document as the source of intent for tone, structure, and accuracy constraints.
