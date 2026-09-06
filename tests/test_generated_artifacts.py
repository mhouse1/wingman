"""Generated artifacts stay out of git (ADR 100 D1).

The runtime trend charts are 4.6 MB each and are rewritten wholesale by
`make runtime-perf-release` on every release, so consecutive versions barely
delta against one another — 77 blobs and 357 MB of history for two file paths,
which is the dominant source of repository growth.

`.gitignore` alone does not hold this line: it has no effect on a path that is
already tracked, and `git add -f` overrides it outright. So the invariant is
asserted here, where a reintroduction shows up as a failing test rather than as
a repository that has quietly grown another 9 MB per release.
"""

import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent

GENERATED = [
    "docs/performance/runtime-performance-trends.html",
    "docs/performance/runtime-performance-trends.preview.html",
]


def _git(*args):
    return subprocess.run(["git", "-C", str(REPO), *args],
                          capture_output=True, text=True, timeout=30)


def _in_git_repo():
    return _git("rev-parse", "--git-dir").returncode == 0


@pytest.mark.skipif(not _in_git_repo(), reason="not a git checkout")
@pytest.mark.parametrize("path", GENERATED)
def test_generated_trend_chart_is_not_tracked(path):
    tracked = _git("ls-files", "--error-unmatch", path).returncode == 0
    assert not tracked, (
        f"{path} is tracked by git. It is a generated artifact (ADR 100 D1) — "
        f"untrack it with `git rm --cached {path}`."
    )


@pytest.mark.skipif(not _in_git_repo(), reason="not a git checkout")
@pytest.mark.parametrize("path", GENERATED)
def test_generated_trend_chart_is_ignored(path):
    """Untracking is not enough on its own: without the ignore rule the next
    `make p` (`git add .`) picks the regenerated file straight back up.

    `--no-index` because check-ignore suppresses tracked paths by default, which
    would make this assertion merely restate the tracking test above instead of
    checking the rule itself.
    """
    assert _git("check-ignore", "-q", "--no-index", path).returncode == 0, (
        f"{path} is not covered by .gitignore (ADR 100 D1)."
    )
