"""The performance release baseline stays out of git (ADR 100 D8).

D2 kept docs/performance/release/ in git so the regression baseline would be
portable to any clone. D8 reverses that: veda is the only host that generates
performance reports, so the baseline lives there and nowhere else, and the
report targets plus `make wrelease` are gated to veda by `require-veda`.

`.gitignore` alone does not hold this line: it has no effect on a path that is
already tracked, and `git add -f` overrides it outright. So the invariant is
asserted here, the same way D1 and D7 assert theirs — a reintroduction shows up
as a failing test, not as a release commit that quietly carries 150 run JSONs
again.
"""

import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent

RELEASE_DIR = "docs/performance/release"


def _git(*args):
    return subprocess.run(["git", "-C", str(REPO), *args],
                          capture_output=True, text=True, timeout=30)


def _in_git_repo():
    return _git("rev-parse", "--git-dir").returncode == 0


@pytest.mark.skipif(not _in_git_repo(), reason="not a git checkout")
def test_performance_release_dir_is_not_tracked():
    result = _git("ls-files", "--", RELEASE_DIR)
    tracked = [line for line in result.stdout.splitlines() if line]
    assert not tracked, (
        f"{len(tracked)} file(s) under {RELEASE_DIR}/ are tracked by git. The "
        f"release baseline is veda-only (ADR 100 D8) — untrack it with "
        f"`git rm --cached -r {RELEASE_DIR}`."
    )


@pytest.mark.skipif(not _in_git_repo(), reason="not a git checkout")
@pytest.mark.parametrize("path", [
    f"{RELEASE_DIR}/run_20260101_000000.json",
    f"{RELEASE_DIR}/runtime-performance-history.csv",
])
def test_performance_release_dir_is_ignored(path):
    """Checked on file paths, not the bare directory: `make wrelease` writes
    files into release/ and the next `make p` (`git add .`) must skip them.

    --no-index because check-ignore suppresses tracked paths by default, which
    would make this merely restate the tracking test above."""
    assert _git("check-ignore", "-q", "--no-index", path).returncode == 0, (
        f"{path} is not covered by .gitignore (ADR 100 D8)."
    )
