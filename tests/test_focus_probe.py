"""Focus probe decision logic (scripts/focus-probe.py).

The probe exists to answer whether an X11 focus guard is buildable on a Wayland
session. Its X access cannot be tested here — there is no display in CI — so the
part under test is the decision a guard would make from a reading, plus the
promise that a probe failure never takes the run down with it.
"""

import importlib.util
import sys
import tempfile
import unittest.mock as mock
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location(
    "focus_probe", Path(__file__).parent.parent / "scripts" / "focus-probe.py")
fp = importlib.util.module_from_spec(_spec)
# @dataclass resolves its own module via sys.modules[cls.__module__]; a
# file-loaded module must be registered before exec or the decorator raises.
sys.modules["focus_probe"] = fp
_spec.loader.exec_module(fp)

GAME_PIDS = {4242}


# --- identity comes from the process, never the title ------------------------

def test_the_game_is_identified_by_pid():
    assert fp.classify("Wine Desktop", pid=4242, game_pids=GAME_PIDS) == fp.VERDICT_GAME


def test_a_window_titled_like_the_game_is_not_the_game():
    """The trap this probe walked into on 2026-08-28. With the game shut down, a
    VS Code window titled "Metalstorm config GitHub... - wingman - Visual Studio
    Code" was classified as the game by a title substring test. A guard using
    that rule keeps typing into the editor — the exact failure it must prevent."""
    title = "Metalstorm config GitHub… - wingman - Visual Studio Code"
    assert fp.classify(title, pid=1122514, game_pids=GAME_PIDS) == fp.VERDICT_OTHER


def test_any_window_may_claim_the_game_name():
    """A browser tab, a terminal on the log, a file manager — all can carry the
    word, and none of them is the game."""
    for t in ["metalstorm.log — less", "MetalStorm Wiki — Firefox", "~/metalstorm"]:
        assert fp.classify(t, pid=999, game_pids=GAME_PIDS) == fp.VERDICT_OTHER


def test_wm_class_is_the_fallback_when_a_window_sets_no_pid():
    assert fp.classify("Wine Desktop", pid=None, game_pids=GAME_PIDS,
                       wm_class='"metalstorm.exe", "Wine"') == fp.VERDICT_GAME
    assert fp.classify("Some editor", pid=None, game_pids=GAME_PIDS,
                       wm_class='"code", "code"') == fp.VERDICT_OTHER


def test_an_unidentifiable_window_is_never_called_the_game():
    """No PID, no class: refuse to guess. Guessing 'game' is the direction that
    resumes injection into an unknown window."""
    assert fp.classify("mystery", pid=None, game_pids=None, wm_class=None) == fp.VERDICT_OTHER


def test_the_game_not_running_means_no_window_can_match_by_pid():
    assert fp.classify("Wine Desktop", pid=4242, game_pids=set()) == fp.VERDICT_OTHER


@pytest.mark.parametrize("title", [None, "", "   "])
def test_no_window_is_none(title):
    assert fp.classify(title, pid=4242, game_pids=GAME_PIDS) == fp.VERDICT_NONE


def test_find_game_pids_never_raises_and_returns_a_set():
    assert isinstance(fp.find_game_pids("definitely-not-a-process"), set)


def test_game_session_is_empty_when_the_game_is_not_running():
    assert fp.game_session_pids("definitely-not-a-process") == set()


def test_the_session_includes_siblings_not_just_the_game_binary():
    """Verified live 2026-08-28: the WM-managed window is "Wine Desktop", owned
    by explorer.exe — a SIBLING of Metalstorm.exe under the Proton launcher, not
    the game binary. Matching only the binary means the active window reads as
    "not the game" whenever the virtual desktop has focus, which suppresses
    every keypress and silently stops wingman working."""
    #  1 systemd -> 100 launcher -> {200 game, 201 explorer, 202 helper}
    #                          999 unrelated app (parented at systemd)
    tree = {200: 100, 201: 100, 202: 100, 100: 1, 999: 1}
    with mock.patch.object(fp, "find_game_pids", lambda _n=None: {200}), \
         mock.patch.object(fp, "_ppid_of", lambda pid: tree.get(pid)), \
         mock.patch.object(fp.os, "listdir", lambda _p: [str(k) for k in tree]):
        session = fp.game_session_pids()
    assert {200, 201, 202} <= session, session
    assert 999 not in session, "an unrelated app was pulled into the session"


def test_the_session_walk_terminates_on_a_cycle():
    """A racing or malformed /proc must not hang the probe."""
    cyclic = {5: 6, 6: 5}
    with mock.patch.object(fp, "find_game_pids", lambda _n=None: {5}), \
         mock.patch.object(fp, "_ppid_of", lambda pid: cyclic.get(pid)), \
         mock.patch.object(fp.os, "listdir", lambda _p: ["5", "6"]):
        assert isinstance(fp.game_session_pids(), set)


# --- agreement: the probe's actual question ---------------------------------

def test_signals_agreeing_is_reported_as_agreement():
    r = fp.FocusReading("Wine Desktop", "Wine Desktop", fp.VERDICT_GAME, fp.VERDICT_GAME)
    assert fp.agreement(r) == "agree"


def test_the_wayland_failure_mode_is_reported_as_disagreement():
    """The case this probe was written to detect: the window manager still
    advertises the game as active while the X server knows focus has left every
    X client. A guard reading only ewmh would keep injecting."""
    r = fp.FocusReading("Wine Desktop", None, fp.VERDICT_GAME, fp.VERDICT_NONE)
    assert fp.agreement(r) == "disagree"


def test_an_error_on_either_signal_is_unknown_not_agreement():
    """A failed read must never be scored as the two signals agreeing."""
    for kw in ({"ewmh_error": "boom"}, {"xfocus_error": "boom"}):
        r = fp.FocusReading("a", "a", fp.VERDICT_GAME, fp.VERDICT_GAME, **kw)
        assert fp.agreement(r) == fp.VERDICT_UNKNOWN


# --- summary must not claim a result the run did not earn --------------------

def _reading(v, title="w"):
    return fp.FocusReading(title, title, v, v)


def test_a_run_where_the_game_never_appeared_proves_nothing():
    """2026-08-28: the probe was run with the game shut down. Every sample saw
    another app, which an earlier version reported as "focus tracking works" —
    the opposite of the truth."""
    out = fp.summarize([_reading(fp.VERDICT_OTHER)] * 10)
    assert "never the active window" in out
    assert "DID observe focus leaving" not in out


def test_a_run_that_never_left_the_game_says_it_proved_nothing():
    out = fp.summarize([_reading(fp.VERDICT_GAME)] * 10)
    assert "proves nothing" in out or "never left the game" in out


def test_a_run_that_saw_focus_leave_reports_the_guard_is_readable():
    out = fp.summarize([_reading(fp.VERDICT_GAME), _reading(fp.VERDICT_OTHER)])
    assert "DID observe focus leaving" in out


def test_one_signal_seeing_the_game_is_enough_to_count_as_present():
    """Run of 2026-08-28 10:09: ewmh named explorer.exe's "Wine Desktop" in all
    291 samples while xfocus named "Metalstorm" in 290. The summary read ewmh
    alone and declared the game had never been active — flatly contradicted by
    the log beside it."""
    rs = [fp.FocusReading("Wine Desktop", "Metalstorm",
                          fp.VERDICT_OTHER, fp.VERDICT_GAME)] * 291
    out = fp.summarize(rs)
    assert "never the active window" not in out
    assert "xfocus saw the game:  291" in out
    assert "must use xfocus" in out


def test_summarize_handles_no_samples():
    assert fp.summarize([]) == "no samples"


# --- operational contract ----------------------------------------------------

def test_format_line_carries_both_titles_and_errors():
    r = fp.FocusReading("Wine Desktop", None, fp.VERDICT_GAME, fp.VERDICT_NONE,
                        xfocus_error="X")
    line = fp.format_line(0.0, r)
    assert "ewmh=game" in line and "xfocus=none" in line and "xfocus_error=X" in line


def test_missing_display_exits_2_rather_than_raising():
    """Exit 2 is 'could not answer', distinct from a real verdict."""
    assert fp.main(["--display", "cannot-possibly-exist:99", "--seconds", "0.1"]) == 2


def test_ctrl_c_keeps_the_samples_and_exits_cleanly():
    """Stopping early is normal. An earlier version raised KeyboardInterrupt out
    of main(), printing a traceback and discarding every sample collected."""

    class _Stops:
        """Yields a few readings, then interrupts like a Ctrl-C would."""

        def __init__(self):
            self.n = 0

        def sample(self):
            self.n += 1
            if self.n > 4:
                raise KeyboardInterrupt
            return _reading(fp.VERDICT_GAME, "Wine Desktop")

    with tempfile.TemporaryDirectory() as d:
        out = Path(d) / "probe.log"
        with mock.patch.object(fp, "_XProbe", lambda _: _Stops()):
            rc = fp.main(["--seconds", "999", "--interval", "0.01", "--out", str(out)])
        assert rc == 0
        body = out.read_text()
        assert "stopped early by operator" in body
        assert body.count("ewmh=game") >= 3      # the samples survived


# --- armed before the game exists (make r1-probe) ---------------------------

def test_wait_for_game_returns_once_the_game_appears():
    """`make r1-probe` arms the probe and then launches the game, so the probe
    starts before the process exists and must wait rather than immediately
    concluding the game was never there."""
    calls = {"n": 0}

    def appears_on_third_look(_name=None):
        calls["n"] += 1
        return {4242} if calls["n"] >= 3 else set()

    with tempfile.TemporaryDirectory() as d:
        out = Path(d) / "probe.log"
        with mock.patch.object(fp, "find_game_pids", appears_on_third_look), \
             mock.patch.object(fp, "_XProbe", lambda _: mock.Mock(
                 sample=lambda: _reading(fp.VERDICT_GAME))), \
             mock.patch.object(fp.time, "sleep", lambda _s: None):
            rc = fp.main(["--wait-for-game", "30", "--seconds", "0.01",
                          "--interval", "0.01", "--out", str(out)])
    assert rc == 0
    assert calls["n"] >= 3, "did not keep looking for the game"


def test_wait_for_game_gives_up_and_says_so():
    """If the game never starts, the probe must still finish and report that
    nothing was proved rather than waiting forever."""
    with tempfile.TemporaryDirectory() as d:
        out = Path(d) / "probe.log"
        with mock.patch.object(fp, "find_game_pids", lambda _n=None: set()), \
             mock.patch.object(fp, "_XProbe", lambda _: mock.Mock(
                 sample=lambda: _reading(fp.VERDICT_OTHER))), \
             mock.patch.object(fp.time, "sleep", lambda _s: None):
            rc = fp.main(["--wait-for-game", "0.2", "--seconds", "0.01",
                          "--interval", "0.01", "--out", str(out)])
        assert rc == 0
        assert "never the active window" in out.read_text()
