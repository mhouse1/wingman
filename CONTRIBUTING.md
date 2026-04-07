# Contributing to Wingman

Thanks for contributing.

This project is still a prototype, so clear changes, reproducible tests, and concise PR notes matter more than perfect polish.

## Development Setup

See [Job Aid 001 — Setup and Usage](docs/job-aids/001-setup-and-usage.md) for full requirements, install steps, hotkey reference, and config options.

Quick start:

```bash
uv sync --all-groups
make run
```

## Branch and Commit Guidelines

- Create a feature/fix branch from `main`.
- Keep commits focused and small.
- Use imperative commit messages.

Examples:
- `fix incoming OCR cache timestamp handling`
- `add mission restart delay config`
- `improve analyzer timing logs`

## Code Style

- Keep code readable and explicit.
- Prefer small functions with clear names.
- Add comments only when the intent is not obvious.
- Avoid broad refactors unrelated to your change.

## Testing Expectations

Before opening a PR, run relevant checks.

Minimum:

```bash
make test
```

Useful additional checks:

```bash
make test1
make test2
make test-perf
```

If your change touches OCR, mission timing, or controller input, include:
- What you tested
- Which commands you ran
- Relevant timing or log excerpts

## Pull Request Checklist

Include these items in the PR description:

- Summary of what changed
- Why the change was needed
- How you tested it
- Any config changes required
- Any known limitations or follow-up tasks

## Configuration Changes

If you add or change settings in `wingman/config.yaml`:
- Document defaults and expected ranges
- Update [Job Aid 001 — Setup and Usage](docs/job-aids/001-setup-and-usage.md) for any new or changed config keys

## Performance-Sensitive Changes

For OCR/performance work:
- Compare before/after timing from `Analyzer: Parallel OCR Timings`
- Share average and worst-case numbers
- Note test context (idle vs active mission)

## Documentation

Update docs when behavior changes:
- [README.md](README.md) — what the bot does, current capabilities, where the project is going
- [Job Aid 001 — Setup and Usage](docs/job-aids/001-setup-and-usage.md) — install, config, hotkeys, testing, troubleshooting
- [docs/PROJECT_AI_ROADMAP.md](docs/PROJECT_AI_ROADMAP.md) — current phase, implemented features, future phases
- `docs/adr/` — architecture decisions (add a new ADR for significant design choices)
- `docs/job-aids/` — repeatable operational tasks

## Reporting Issues

When filing a bug, include:
- OS and Python version
- Command used to run Wingman
- Relevant config snippets (region, mission, OCR settings)
- Log excerpt that shows the issue

## Security and Safety

Do not commit secrets, local credentials, or machine-specific private data.

This tool automates game inputs. Test changes in safe scenarios before regular use.
