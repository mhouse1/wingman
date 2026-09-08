"""test_screenshots stays out of git (ADR 100 D7).

D5 measured the corpora still tracked at the time — 31 loose top-level PNGs,
test_screenshots/telemetry/, and test_screenshots/integration_test/ — and kept
them in git because every referencing test needed to run from any clone. D7
reverses that: the corpus lives on veda only now, `make tp`/`make tp-full` are
gated to that host, and every referencing test skips gracefully when the files
are absent (the same pattern this repo already used for `unknown_anomalies/`).

`.gitignore` alone does not hold this line: it has no effect on a path that is
already tracked, and `git add -f` overrides it outright. So the invariant is
asserted here, the same way ADR 100 D1 asserts it for the generated trend
charts — a reintroduction shows up as a failing test, not as a repository that
quietly starts growing again.
"""

import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent

# Directories moved off git entirely by D7.
UNTRACKED_DIRS = [
    "test_screenshots/telemetry",
    "test_screenshots/integration_test",
]


def _git(*args):
    return subprocess.run(["git", "-C", str(REPO), *args],
                          capture_output=True, text=True, timeout=30)


def _in_git_repo():
    return _git("rev-parse", "--git-dir").returncode == 0


@pytest.mark.skipif(not _in_git_repo(), reason="not a git checkout")
@pytest.mark.parametrize("path", UNTRACKED_DIRS)
def test_screenshot_corpus_dir_is_not_tracked(path):
    tracked = _git("ls-files", "--error-unmatch", path).returncode == 0
    assert not tracked, (
        f"{path} is tracked by git. It moved to veda-only storage (ADR 100 D7) "
        f"— untrack it with `git rm --cached -r {path}`."
    )


@pytest.mark.skipif(not _in_git_repo(), reason="not a git checkout")
@pytest.mark.parametrize("path", UNTRACKED_DIRS)
def test_screenshot_corpus_dir_is_ignored(path):
    """--no-index because check-ignore suppresses tracked paths by default,
    which would make this merely restate the tracking test above."""
    assert _git("check-ignore", "-q", "--no-index", path).returncode == 0, (
        f"{path} is not covered by .gitignore (ADR 100 D7)."
    )


@pytest.mark.skipif(not _in_git_repo(), reason="not a git checkout")
def test_no_loose_top_level_screenshots_are_tracked():
    """The 31 loose PNGs directly under test_screenshots/ (INCOMING.png,
    STALL_PROFILE.png, AMMO_MISSILE.png, ...) are files, not one directory —
    checked with a single glob rather than one parametrized case each."""
    result = _git("ls-files", "--", "test_screenshots/*.png")
    tracked = [line for line in result.stdout.splitlines() if line]
    assert not tracked, (
        f"loose PNGs under test_screenshots/ are tracked by git: {tracked}. "
        "They moved to veda-only storage (ADR 100 D7) — untrack with "
        "`git rm --cached test_screenshots/*.png`."
    )
