# Research 006 — Coding Standard Adoption (Ruff Lint and Format Gate)

| Status | Date       | Wingman Version |
|--------|------------|-----------------|
| Draft  | 2026-08-16 | 1.8.3           |

## Question

Should this project adopt a coding standard, and if so, in what form? Is the
benefit worth the extra step and added complexity for a solo-maintainer project?

## Current State

- No lint, format, or type-check tooling exists anywhere in the repository
  (no ruff, black, flake8, pylint, or mypy in `pyproject.toml` or the Makefile).
- Style consistency currently depends entirely on manual review discipline.
- `CLAUDE.md` already carries the project-specific rules that matter most —
  lock release in finally blocks, stoppable daemon threads, lock acquire
  timeouts on main-loop paths — plus documentation format and numbering
  conventions. These are enforced only by eyeballs.
- The project already has a gate culture: `make tp` bundles tests, the
  ADR044/ADR045 runtime gates, the requirements gate, and the performance
  regression check. Style is the one quality dimension with no gate.

## Benefits Assessed

1. **Review load reduction.** The workflow is AI-written code with manual
   review before anything enters history. A linter pre-filters trivial
   findings (unused imports, shadowed variables, dead code) so review
   attention goes to threading correctness, FSM transitions, and OCR timing —
   the things that actually need a human.
2. **Bug-class detection, not style.** The main value is rules that catch
   real defects: mutable default arguments, loop-variable capture in
   closures, f-strings missing placeholders, unused variables indicating
   incomplete refactors. Callback-heavy, threaded code (analyzer, replay) is
   exactly where these bite.
3. **Cross-session consistency.** Contributors are one maintainer plus many
   AI sessions over months. A formatter keeps diffs behavior-only, with no
   style churn between sessions.

## Costs Assessed

- **Adoption:** one session to configure, triage baseline findings, and tune
  the rule set. One-time.
- **Ongoing friction:** near zero if wired into `make tp` — an extra check
  inside an existing gate, not an extra step. It only becomes friction as a
  separate command that must be remembered.
- **False-positive noise:** the real recurring cost. Managed by starting with
  a small rule set and expanding, rather than enabling everything and
  suppressing.
- **Not applicable here:** the classic multi-contributor arguments
  (onboarding, ending team style debates). Worth approximately nothing for a
  solo maintainer and should not be counted as benefits.

## Recommendation

Adopt **PEP 8 via ruff defaults plus a small curated extension set**, with
`ruff format` (black-compatible) as the formatter. Do **not** write a prose
coding-standard document — it would duplicate PEP 8 and rot.

Proposed `pyproject.toml` configuration:

```toml
[tool.ruff]
line-length = 100
target-version = "py311"   # match the version pyproject actually requires

[tool.ruff.lint]
select = [
    "E", "W",    # pycodestyle - PEP 8 core
    "F",         # pyflakes - unused imports and vars, undefined names
    "B",         # bugbear - mutable defaults, loop-variable capture in closures
    "SIM",       # simplifications - collapsible ifs, redundant bool logic
    "ARG",       # unused function arguments - dead callback params
    "RET",       # inconsistent return paths
]
ignore = [
    "E501",      # line length - the formatter owns it
]
```

Rationale for the shape:

- **Ruff defaults, not a custom style.** Any deviation from the ecosystem
  default is pure maintenance cost for a solo project; every AI session,
  editor, and tool already assumes PEP 8.
- **`B` (bugbear) is the highest-value set for this codebase** — it catches
  closure and late-binding surprises, the failure modes of code that
  registers callbacks and spawns threads.
- **`ARG` suits the callback-heavy FSM** — an unused argument in a transition
  callback often means the callback signature drifted.
- **Line length 100, not 79** — avoids awkward wrapping around long
  crop-region names and log strings; black-compatible compromise.
- **Deliberately excluded for now:** annotation enforcement (`ANN` or mypy —
  retrofit pain; revisit incrementally, most valuable in the threaded
  analyzer and replay code), docstring rules (`D` — noise for a solo
  project), and import-sorting strictness beyond defaults.

Layering: ruff enforces the generic layer mechanically; `CLAUDE.md` continues
to carry the domain layer (lock patterns, daemon threads, lock timeouts).
Some `CLAUDE.md` rules are mechanically detectable (the
`try: release() except RuntimeError` anti-pattern) and could become custom
checks later — a nice-to-have, not part of initial adoption.

## Proposed Wiring

- `make lint` target running `ruff check` and `ruff format --check`.
- Add `make lint` to `make tp` so it rides the existing pre-release gate.
- Record the adoption as a short ADR, since it changes the release gate.

## Next Steps

1. Add ruff as a dev dependency and the configuration above.
2. Run the baseline: capture the initial finding count and triage — tune the
   rule set before it joins any gate.
3. Apply `ruff format` once as a dedicated, behavior-free commit so later
   diffs stay clean.
4. Wire `make lint` into `make tp`.
5. Write the adoption ADR referencing this research document.
6. Revisit mypy incrementally after ruff has settled (optional).

## Related

- Research 002 / Research 004 — prior tooling-adoption spikes (StrictDoc)
  showing the pattern of spiking a tool against the real repo before gating
  on it.
- Prior project lesson: adopt tooling early rather than waiting for a scale
  threshold — deferral inverts the real cost curve.
