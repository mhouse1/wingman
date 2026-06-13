# ADR 048 — Dual AI Assistant Instructions: Keep CLAUDE.md and AGENTS.md Active Simultaneously

| Status   | Date       | Wingman Version |
|----------|------------|-----------------|
| Accepted | 2026-06-13 | 1.6.19          |

## Context

Two AI coding assistants are in active use on this project:

- **Claude Code** (Anthropic) — reads `CLAUDE.md` at the repository root.
- **GitHub Copilot** (Microsoft) — reads `.github/copilot-instructions.md`. Copilot
  has access to multiple underlying models including Claude, GPT-4o, and Gemini,
  selectable per-session. This made it the superior tool for a period: one subscription
  gave access to the best available model at any time without committing to a single
  provider.

Recent pricing changes to GitHub Copilot (premium model completions now metered
separately rather than included in the base plan) significantly reduced its value
proposition for heavy daily use. Claude Code, billed per-token with no seat fee, became
cost-competitive or cheaper for the same workload.

The preference between the two tools is therefore not fixed — it is driven by current
pricing, model quality, and available features, all of which change independently.
Locking the repository to a single AI assistant instruction file would require a repo
change every time the preferred tool shifts, creating unnecessary churn.

## Decision

Keep both `CLAUDE.md` and `AGENTS.md` active at all times as names for the same
canonical instructions file. `.github/copilot-instructions.md` is always a symlink to
the same canonical source. No toggling is required for day-to-day assistant switching —
whichever tool is opened sees current instructions immediately.

The `toggle-ai-md.sh` script is retained for one narrow purpose: changing which
filename is the canonical editable source (i.e. which file an editor opens when
following the symlink). This is an authoring convenience, not an activation switch.

### File layout

```
CLAUDE.md                             ← canonical source (or symlink, depending on toggle state)
AGENTS.md                             → CLAUDE.md  (symlink)
.github/copilot-instructions.md       → ../CLAUDE.md  (symlink)
```

All three paths resolve to identical bytes at all times. Switching AI assistants
requires no repository changes — open the tool, it reads its expected file, done.

## Consequences

Positive:

- Zero friction when switching between Claude Code and GitHub Copilot: no file
  renames, no commits, no script to run.
- Instructions stay in sync across all tools automatically — editing one file
  updates all agent views instantly via symlinks.
- Preserves flexibility to migrate between assistants as pricing and model quality
  evolve without any repository policy change.

Trade-offs:

- The symlink setup must be reproduced on each new clone. Running `./toggle-ai-md.sh`
  once after cloning creates the missing symlinks. This step should be included in
  onboarding docs (ADR 047 pre-flight check is the natural place to surface it).
- GitHub's web UI resolves symlinks and shows the full file content at both paths,
  which is the desired behaviour. Tools that do not follow symlinks would see a symlink
  pointer rather than content — no known tools used on this project have this issue.

## Alternatives Considered

1. Maintain two independent files with duplicate content.
   - Rejected: content will diverge over time; no mechanism enforces sync.

2. Toggle between CLAUDE.md and AGENTS.md (original toggle-ai-md.sh design).
   - Rejected: toggling implies only one assistant is active at a time, which is
     unnecessarily restrictive and adds friction when switching mid-session.

3. Use only CLAUDE.md and point Copilot at it via a redirect stub.
   - Rejected: a redirect stub (the original `.github/copilot-instructions.md` content)
     is not guaranteed to be followed by all Copilot versions; a symlink is more
     reliable.

## References

- `toggle-ai-md.sh` — script for changing which root file is canonical
- ADR 047 — host environment pre-flight check (onboarding step for symlink setup)
