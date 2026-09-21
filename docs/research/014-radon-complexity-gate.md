# Research 014 — Radon Complexity Gate for Future Adoption

| Status | Date       | Wingman Version |
|--------|------------|-----------------|
| Draft  | 2026-09-21 | 1.8.11          |

## Question

Should this project adopt a complexity checker after the Ruff baseline is in place, and if so, how should it be introduced without turning it into an early gate that creates churn?

## Current State

- Ruff is already the preferred first-line quality gate, per Research 006.
- The project has a strong AI-assisted workflow and is increasingly complex across analyzer logic, controller behavior, replay, and shutdown paths.
- The codebase is already showing the kinds of risks that complexity metrics are designed to catch: large callback-heavy functions, dense state-machine logic, and multi-responsibility modules.
- The project currently has no explicit complexity budget, so growth can outpace review discipline unless a warning signal is added.

## Why Radon is Relevant

Radon is a good fit for this project because the main risk is not style drift; it is architectural drift and function growth.

The project already carries domain-specific safety rules in `CLAUDE.md` and a real gate culture through `make tp`, `make test`, and replay/runtime validation. That is a strong foundation for a second-stage metric that warns on function complexity before it becomes a maintenance trap.

More specifically, Radon gives useful signals for:

- functions whose branching count has grown beyond a healthy range
- modules where a single function is doing too many things at once
- callback-driven logic that is harder for AI and humans to reason about safely
- tactical code paths that are likely to need re-reading during bug fixes and future refactors

This matters because the project is heavily AI-assisted. Bad complexity is amplified by AI workflows: a function that is hard to reason about invites broad reads, speculative fixes, and repeated patching.

## Why Radon Is Not the First Gate

Research 006 intentionally chose Ruff as the initial adoption path because it is:

- low friction
- highly familiar
- broadly compatible with editor and CI tooling
- effective at catching real defects with minimal noise

Radon is different. Its value is real, but it is less universal and more context-sensitive. A bad complexity threshold can become a source of churn before the codebase is ready for it.

The right approach is therefore:

- adopt Ruff first
- let the repo settle on that baseline
- then add Radon as a debt-prevention and review aid
- only later decide whether a threshold should become a hard gate

## Proposed Adoption Plan

### Phase 1 — Baseline and diagnosis

- Add Radon as a dev-only dependency.
- Run `radon cc wingman/ -a` for a baseline snapshot.
- Review the top offenders without changing code yet.
- Focus on functions that combine multiple responsibilities or are deeply nested under callback/state-machine logic.

### Phase 2 — Report-only adoption

- Add a Make target such as `make complexity` or `make radon` that emits the report.
- Keep it advisory, not blocking.
- Use it as a review signal during active work and before release tasks.

### Phase 3 — Threshold selection

- After a stable period with Ruff in place, select a threshold based on actual repo data.
- Prefer a threshold that catches genuinely risky functions without turning the gate into churn.
- A threshold should be justified by measured repo behavior, not by a generic rule copied from another project.

### Phase 4 — Optional enforcement

- Only after the project has matured and the baseline is understood should the report be promoted to a gate.
- Enforcement should remain narrow, keyed to the highest-risk hotspots rather than the entire repo at once.

## Recommended Scope

A future Radon gate should cover:

- `wingman/analyzer.py`
- `wingman/controller.py`
- `wingman/main.py`
- any replay or shutdown logic that is deeply callback-driven

It should not be treated as a blanket line-count or function-size policy across the entire project before the repo has settled.

## Benefits Expected

1. **Debt reduction.** High-complexity functions are the easiest places for AI-assisted code to drift and accumulate hidden assumptions.
2. **Better review focus.** Reviewers can target the actual hot spots instead of reading entire large modules linearly.
3. **Lower churn in AI sessions.** A flagged hotspot is easier to split or simplify before a speculative patch begins.
4. **Safer scaling.** As the project grows, complexity metrics give an early warning before the architecture becomes brittle.

## Costs and Tradeoffs

- Radon adds another tool to manage and a bit of setup, but the overhead is small.
- Complexity numbers are not perfect: some deliberately dense functions are still understandable if they are very cohesive.
- A hard threshold chosen too soon will create noise and friction.
- The tool should be used as a debt signal, not as a substitute for judgment.

## Recommendation

Adopt Radon after Ruff has been established, as a follow-on complexity guardrail rather than as the initial coding-standard gate.

The correct sequence is:

1. establish the Ruff baseline
2. run Radon in report mode
3. review the hotspots and refine the threshold
4. only then consider a future gate if the project continues to grow in complexity

This keeps the project aligned with the Research 006 philosophy: start with low-friction, high-value tooling and add complexity checks only once the codebase is mature enough to benefit from them.

## Related

- Research 006 — Coding Standard Adoption (Ruff Lint and Format Gate)
- Project-specific rule set in `CLAUDE.md` for lock patterns, daemon-thread stoppability, and main-loop timeout rules
