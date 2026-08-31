# ADR 100 — Repository Growth and Generated Artifacts

| Status | Date       | Wingman Version |
|--------|------------|-----------------|
| Draft  | 2026-08-31 | 1.8.7           |

## Context

`make wrelease` commits the session artifacts accumulated since the last
release. The v1.8.7 release committed **146 files, 38,900 insertions**, almost
all of it `docs/performance/release/run_*.json`, and the repository has reached
**406 MB** with a 206 MB pack.

The obvious reading is that the run JSONs are the problem. They are not.

### What is actually in the history

Every blob in `git rev-list --objects --all`, grouped by kind:

| Category | Size in history | Blobs |
|----------|----------------:|------:|
| **Generated trend HTML** | **357.4 MB** | 77 |
| PNG images | 288.2 MB | 165 |
| **Performance run JSON** | **3.5 MB** | 1,213 |
| Performance CSV | 0.1 MB | 28 |
| Everything else | 75.2 MB | 1,871 |

1,213 run JSONs across the entire history total **3.5 MB — under 1%**. They are
small, highly repetitive, and delta-compress to almost nothing. Deleting every
one of them would not measurably change a clone.

The growth is `docs/performance/runtime-performance-trends.html` and its
`.preview.html` sibling: **4.6 MB each, regenerated wholesale on every release**,
26 commits and counting. Because each regeneration rewrites the file with the
data embedded, consecutive versions barely delta against one another — 77 blobs
for two file paths.

The file count in the release output is what draws the eye; the byte count is
somewhere else entirely.

## Decision

**D1. Stop committing the generated trend HTML.** Both
`runtime-performance-trends.html` and `runtime-performance-trends.preview.html`
are derived artifacts, rebuilt from the CSVs and run JSONs by
`tests/runtime_performance_tracking.py` in seconds. They are gitignored and
regenerated locally on demand.

Note which target actually committed them: `make wrelease` never staged them —
it regenerates the charts *after* its own commit, leaving them modified in the
working tree. The stager was the next `make p`, whose `git add .` swept them up.
So gitignoring is the whole fix; no release-path change is needed. Because both
paths are already tracked, `.gitignore` alone has no effect on them, and they
must be untracked once with `git rm --cached`.

A test asserts neither path is tracked, so a later `git add -f` cannot quietly
reintroduce the growth.

This removes the dominant source of future growth. Nothing is lost that cannot
be rebuilt from data that remains in the repository.

**D2. Keep the run JSONs in git.** They cost 3.5 MB across all history, and they
are:

- the **raw evidence** behind the timing claims in the performance ADRs, which
  ADR 019 established must be actual measurements rather than estimates;
- the only source that can answer a question the CSV was not aggregated for — a
  different percentile, a per-crop breakdown, a metric not yet invented;
- what makes the regression baseline **portable**. `PerformanceTracker` compares
  each session against the release baseline, and job aid 010 documents running
  on a second machine. A baseline that exists only on one host is not a baseline.

Local-only storage would also put the entire performance history one disk
failure from gone. 3.5 MB is a low price for the evidence trail.

**D3. `make squash` is not the instrument for this.** It is
`git rebase -i --autosquash origin/main`, and it would collapse the 170 commits
currently ahead of `main`, releasing the intermediate blob versions among them.
That is a real saving, but it is the wrong tool here:

- The branch is **already pushed**. `origin/resolve_tech_debt` still references
  the old commits, so nothing is reclaimed until the squashed branch is
  force-pushed and the remote expires its own objects. Locally it also needs
  `git reflog expire --expire=now --all` followed by `git gc --prune=now`;
  without those the objects stay for the 90-day reflog window.
- It destroys 170 commits of genuine history to reclaim space, and eight ADRs
  contain hex strings that may be commit references. Squashing is for tidying a
  branch before merge, not for storage.
- It does nothing about the trend HTML in the **already-merged** history, which
  is where most of the 357 MB lives.

Squash if the commit history wants tidying. Do not squash to save bytes.

**D4. Reclaiming the existing 357 MB is deferred, and needs a separate
decision.** Gitignoring stops future growth; it does not shrink the current
clone. Removing those two paths from history requires `git filter-repo`, which
rewrites every commit SHA and obliges every clone to be re-made. That is
disruptive enough to be its own decision, and it is not urgent: the repository
is large but functional.

**D5. PNG images (288 MB) are the next target, and out of scope here.** Several
are 5 MB full-resolution screenshots. Whether they can be downsampled, deduped,
or moved depends on which are still referenced by the calibration and gate
corpora (ADR 071/072) — a question this ADR does not answer.

## Consequences

Future releases stop adding ~9 MB of regenerated HTML each. The release output
still lists many JSON files, and that remains the correct behaviour even though
it looks alarming — the file count is not the cost.

The trend charts become a local artifact. Anyone wanting them runs the make
target; anyone cloning the repository does not get them for free. That is the
trade being accepted, and it is the right way round: the inputs are versioned,
the rendering is not.

The repository stays at its current size until D4 is decided separately.

## Validation

- **V1.** `docs/performance/runtime-performance-trends.html` and
  `.preview.html` are gitignored, and `git status` after a `make wrelease` shows
  neither as staged or untracked-and-noisy.
- **V2.** The chart targets still regenerate both files from a clean checkout
  with no `docs/performance/` HTML present.
- **V3.** `PerformanceTracker`'s regression check still finds its baseline from
  the committed run JSONs, on a machine that has never produced a session.
- **V4.** Measure the delta: the size a release adds to the pack, before and
  after, so the claim in D1 is checked rather than assumed.

## References

- ADR 019 — performance ADRs carry actual measurements, which is why the raw
  JSONs are evidence rather than clutter
- ADR 071 / ADR 072 — the screenshot corpora behind the 288 MB in D5
- `docs/job-aids/010-run-metalstorm-on-linux.md` — second-machine validation,
  which a local-only baseline would break
- Measurement: `git rev-list --objects --all` grouped by path, 2026-08-31
