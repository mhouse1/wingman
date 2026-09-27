"""HLDD 016 Part 3 (CR-018-11): the versioned pre-push hook blocks a red push.

HEAD was red for a day (2026-09-26/27) because nothing ran the suite before a
push. The hook is opt-in (`make hooks`); these tests run the real hook script in
a throwaway repository whose Makefile stubs `lint` and `test`.
"""

import os
import pathlib
import shutil
import stat
import subprocess

import pytest

_HOOK = pathlib.Path(".githooks/pre-push").resolve()

pytestmark = pytest.mark.skipif(shutil.which("git") is None or shutil.which("make") is None,
                                reason="needs git and make")


def _repo(tmp_path, test_rc):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / "Makefile").write_text(
        "lint:\n\t@echo linted\n"
        f"test:\n\t@echo tested; exit {test_rc}\n", encoding="utf-8")
    return tmp_path


def _run_hook(repo):
    return subprocess.run(["bash", str(_HOOK), "origin", "git@example:repo"], cwd=repo,
                          capture_output=True, text=True, stdin=subprocess.DEVNULL,
                          env={**os.environ, "GIT_DIR": str(repo / ".git")})


def test_a_green_suite_lets_the_push_through(tmp_path):
    result = _run_hook(_repo(tmp_path, 0))
    assert result.returncode == 0, result.stderr
    assert "linted" in result.stderr and "tested" in result.stderr


def test_a_red_suite_blocks_the_push(tmp_path):
    result = _run_hook(_repo(tmp_path, 1))
    assert result.returncode != 0


def test_the_hook_is_executable_and_never_runs_the_operator_lanes():
    assert _HOOK.stat().st_mode & stat.S_IXUSR
    body = "\n".join(ln for ln in _HOOK.read_text().splitlines() if not ln.lstrip().startswith("#"))
    for lane in ("make tp", "tp-full", "rr-", "wrelease"):
        assert lane not in body, lane


def test_make_hooks_only_points_git_at_the_versioned_hooks():
    """Opt-in by design: nothing installs the hook on its own."""
    out = subprocess.run(["make", "-n", "hooks"], capture_output=True, text=True, check=True)
    assert "git config core.hooksPath .githooks" in out.stdout
