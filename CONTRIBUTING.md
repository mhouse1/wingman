# Contributing to Wingman

Thanks for contributing.

This project is still a prototype, so clear changes, reproducible tests, and concise PR notes matter more than perfect polish.

## Development Setup

1. Clone and enter the repo.
2. Install dependencies:

```bash
uv sync --all-groups
```

3. Run the app locally:

```bash
uv run python -m wingman.main --log-level DEBUG
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
- Update `README.md` if behavior changes for users

## Performance-Sensitive Changes

For OCR/performance work:
- Compare before/after timing from `Analyzer: Parallel OCR Timings`
- Share average and worst-case numbers
- Note test context (idle vs active mission)

## Documentation

Update docs when behavior changes:
- `README.md` for user-facing changes
- `docs/` for architecture and workflow details
- Job aids for repeatable operational tasks

## Reporting Issues

When filing a bug, include:
- OS and Python version
- Command used to run Wingman
- Relevant config snippets (region, mission, OCR settings)
- Log excerpt that shows the issue

## Security and Safety

Do not commit secrets, local credentials, or machine-specific private data.

This tool automates game inputs. Test changes in safe scenarios before regular use.
