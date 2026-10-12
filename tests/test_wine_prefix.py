"""Is a Wine prefix in use? (wingman/wine_prefix.py)

On 2026-10-11 a settings sync into a new account prefix was written while the
game was still up and was lost when wineserver saved the registry. The guard
then in use searched process command lines for the prefix path, which they
never hold, so it answered "free" for every prefix.

Usage: uv run pytest tests/test_wine_prefix.py -q
"""

import os

from wingman.wine_prefix import prefix_in_use, wait_until_free


def _server_dir(prefix) -> str:
    st = os.stat(prefix)
    return f"/tmp/.wine-1000/server-{st.st_dev:x}-{st.st_ino:x}"


def _process(proc, pid: int, comm: str, cwd: str) -> None:
    d = proc / str(pid)
    d.mkdir(parents=True)
    (d / "comm").write_text(comm + "\n", encoding="utf-8")
    os.symlink(cwd, d / "cwd")


def test_a_wineserver_working_from_this_prefixs_server_dir_means_in_use(tmp_path):
    prefix = tmp_path / "Metalstorm-acct2"
    prefix.mkdir()
    proc = tmp_path / "proc"
    _process(proc, 100, "wineserver", _server_dir(prefix))
    assert prefix_in_use(prefix, proc=str(proc)) is True


def test_a_wineserver_for_another_prefix_does_not_count(tmp_path):
    """The operator can have one account's game up while another prefix is edited."""
    prefix, other = tmp_path / "Metalstorm-acct2", tmp_path / "Metalstorm-acct1"
    prefix.mkdir()
    other.mkdir()
    proc = tmp_path / "proc"
    _process(proc, 100, "wineserver", _server_dir(other))
    assert prefix_in_use(prefix, proc=str(proc)) is False


def test_the_game_process_alone_is_not_what_is_matched(tmp_path):
    """Only wineserver writes user.reg, and only its cwd names the prefix."""
    prefix = tmp_path / "Metalstorm"
    prefix.mkdir()
    proc = tmp_path / "proc"
    _process(proc, 100, "Metalstorm.exe", "/home/op/dev-tools/github/wingman")
    (proc / "self").mkdir()
    assert prefix_in_use(prefix, proc=str(proc)) is False


def test_a_wineserver_that_cannot_be_attributed_counts_as_in_use(tmp_path):
    """A wrong "free" loses the edit; a wrong "in use" only delays it."""
    prefix = tmp_path / "Metalstorm"
    prefix.mkdir()
    proc = tmp_path / "proc"
    d = proc / "100"
    d.mkdir(parents=True)
    (d / "comm").write_text("wineserver\n", encoding="utf-8")
    (d / "cwd").write_text("not a link", encoding="utf-8")   # readlink -> EINVAL
    assert prefix_in_use(prefix, proc=str(proc)) is True


def test_a_wineserver_that_exited_mid_scan_is_skipped(tmp_path):
    prefix = tmp_path / "Metalstorm"
    prefix.mkdir()
    proc = tmp_path / "proc"
    d = proc / "100"
    d.mkdir(parents=True)
    (d / "comm").write_text("wineserver\n", encoding="utf-8")   # no cwd: gone
    assert prefix_in_use(prefix, proc=str(proc)) is False


def test_a_prefix_that_does_not_exist_is_not_in_use(tmp_path):
    assert prefix_in_use(tmp_path / "missing", proc=str(tmp_path)) is False


def test_wait_returns_once_the_wineserver_has_gone(tmp_path, monkeypatch):
    answers = iter([True, True, False])
    monkeypatch.setattr("wingman.wine_prefix.prefix_in_use", lambda _p: next(answers))
    now = [0.0]
    slept = []

    def sleep(s):
        slept.append(s)
        now[0] += s

    assert wait_until_free(tmp_path, 20.0, clock=lambda: now[0], sleep=sleep) is True
    assert len(slept) == 2


def test_wait_gives_up_at_the_timeout(tmp_path, monkeypatch):
    monkeypatch.setattr("wingman.wine_prefix.prefix_in_use", lambda _p: True)
    now = [0.0]

    def sleep(s):
        now[0] += s

    assert wait_until_free(tmp_path, 2.0, clock=lambda: now[0], sleep=sleep) is False
    assert now[0] >= 2.0


def test_zero_timeout_is_a_single_check(tmp_path, monkeypatch):
    """How a hand-run sync asks: refuse at once, do not wait."""
    monkeypatch.setattr("wingman.wine_prefix.prefix_in_use", lambda _p: True)

    def sleep(_s):
        raise AssertionError("must not wait")

    assert wait_until_free(tmp_path, 0.0, clock=lambda: 0.0, sleep=sleep) is False
