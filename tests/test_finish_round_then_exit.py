"""FINISH_ROUND_THEN_EXIT hotkey (ADR 094), V1-V9.

Finish the round, exit wingman at the next lobby, then close MetalStorm. The
deferral reuses the safe point ADR 090 and ADR 093 already wait on; the game
close is new, and is the part with teeth — wingman terminating another process.
"""

import signal

import pytest

from pathlib import Path

from wingman import game_shutdown
from wingman.game_shutdown import close_game, find_game_pids

MAIN_SRC = Path("wingman/main.py").read_text()


# --- V6/V7: closing the game ------------------------------------------------

class _FakeProc:
    """A pid that dies after `dies_after` liveness checks, or never."""
    def __init__(self, pid, dies_after=0, term_raises=None):
        self.pid = pid
        self.dies_after = dies_after
        self.checks = 0
        self.signals = []
        self.term_raises = term_raises


@pytest.fixture
def fake_procs(monkeypatch):
    procs = {}

    def _kill(pid, sig):
        p = procs[pid]
        if p.term_raises and sig == signal.SIGTERM:
            raise p.term_raises
        p.signals.append(sig)
        if sig == signal.SIGKILL:
            p.dies_after = 0
            p.checks = 999

    def _alive(pid):
        p = procs.get(pid)
        if p is None:
            return False
        p.checks += 1
        return p.checks <= p.dies_after

    monkeypatch.setattr(game_shutdown, "find_game_pids", lambda name="x": list(procs))
    monkeypatch.setattr(game_shutdown.os, "kill", _kill)
    monkeypatch.setattr(game_shutdown, "_alive", _alive)
    return procs


def test_absent_game_is_not_an_error(fake_procs):
    r = close_game(grace_s=0, sleep=lambda s: None)
    assert r["found"] == 0 and r["ok"] is True


def test_sigterm_is_enough_when_the_game_exits(fake_procs):
    fake_procs[101] = _FakeProc(101, dies_after=0)
    r = close_game(grace_s=1, sleep=lambda s: None)
    assert r["terminated"] == [101]
    assert r["killed"] == [], "must not SIGKILL a process that already exited"
    assert r["ok"] is True


def test_sigkill_escalates_for_a_survivor(fake_procs):
    fake_procs[102] = _FakeProc(102, dies_after=99)
    ticks = iter([0.0, 0.1, 0.2, 99.0])
    r = close_game(grace_s=0.3, clock=lambda: next(ticks), sleep=lambda s: None)
    assert 102 in r["terminated"] and 102 in r["killed"]


def test_a_failing_signal_never_raises(fake_procs):
    fake_procs[103] = _FakeProc(103, term_raises=PermissionError("nope"))
    r = close_game(grace_s=0, sleep=lambda s: None)      # must not raise — V7
    assert r["ok"] is False and 103 in r["failed"]


def test_already_gone_between_scan_and_signal_is_success(fake_procs):
    fake_procs[104] = _FakeProc(104, term_raises=ProcessLookupError())
    r = close_game(grace_s=0, sleep=lambda s: None)
    assert r["ok"] is True and r["failed"] == []


def test_find_game_pids_never_raises_on_a_broken_proc(monkeypatch):
    monkeypatch.setattr(game_shutdown.os, "listdir",
                        lambda p: (_ for _ in ()).throw(OSError("boom")))
    assert find_game_pids() == []


def test_find_game_pids_ignores_non_pid_entries(tmp_path, monkeypatch):
    (tmp_path / "1234").mkdir()
    (tmp_path / "1234" / "comm").write_text("Metalstorm.exe\n")
    (tmp_path / "cpuinfo").write_text("not a pid")
    monkeypatch.setattr(game_shutdown, "_PROC", str(tmp_path))
    assert find_game_pids("Metalstorm.exe") == [1234]


def test_find_game_pids_does_not_match_wingman_itself(tmp_path, monkeypatch):
    """Why this is a /proc scan and not `pkill -f`: the Makefile has to split its
    own pattern through a shell variable so pkill cannot kill the recipe shell."""
    (tmp_path / "1").mkdir()
    (tmp_path / "1" / "comm").write_text("python3\n")
    monkeypatch.setattr(game_shutdown, "_PROC", str(tmp_path))
    assert find_game_pids("Metalstorm.exe") == []


# --- V1/V2/V3: the deferral contract ---------------------------------------

class _Ctrl:
    """The controller surface the main loop uses for this feature."""
    def __init__(self):
        import threading
        self._finish_round_event = threading.Event()
    finish_round_requested = property(lambda self: self._finish_round_event.is_set())


def _controller():
    from wingman.controller import Controller
    c = Controller.__new__(Controller)
    import threading
    c._finish_round_event = threading.Event()
    return c


def test_request_and_cancel_toggle():
    from wingman.controller import Controller
    c = _controller()
    assert Controller.finish_round_requested(c) is False
    Controller.request_finish_round(c, True)
    assert Controller.finish_round_requested(c) is True
    Controller.request_finish_round(c, False)      # V3 — a second press cancels
    assert Controller.finish_round_requested(c) is False


def test_safe_point_condition_matches_the_guards():
    """V1/V2: the deferral must not invent its own safe point — a divergence
    would mean exiting mid-battle, which is the thing this avoids."""
    src = MAIN_SRC
    assert "if ctrl.finish_round_requested() and _safe:" in src, \
        "finish-round must gate on the same _safe the guards use"


def test_game_close_happens_after_the_artifacts():
    """V6 ordering: a hung close must not cost the session's data."""
    src = MAIN_SRC
    assert src.index("resource_sampler.summarize()") < src.index("close_game("), \
        "close_game must run after the artifacts are written"
    assert src.index("stats_tracker.finalize") < src.index("close_game(")


def test_only_the_deliberate_stop_closes_the_game():
    """V9: a guard exit means 'restart wingman' — the client should stay up."""
    src = MAIN_SRC
    assert "if finish_round_exit:" in src
    guard_line = src.index("MEMORY GUARD: ending session")
    close_line = src.index("if finish_round_exit:")
    assert guard_line < close_line
    assert "close_game(" not in src[guard_line:close_line], \
        "a guard exit must not close the game"


# --- V5: the hotkey must not reach the game --------------------------------

def test_hotkey_is_not_a_maneuver_key():
    from wingman import keybindings as k
    assert k.FINISH_ROUND_THEN_EXIT not in k._WATCHED_MANEUVER_KEYS, \
        "would read as manual takeover (ADR 070 d4)"
    game_keys = {k.NOSE_UP_KEY, k.NOSE_DOWN_KEY, k.ROLL_LEFT_KEY, k.ROLL_RIGHT_KEY,
                 k.YAW_LEFT, k.AFTERBURNER_KEY, k.AIRBRAKE_KEY, k.WINGSWEEP_KEY,
                 k.DEPLOY_FLARES_KEY, k.FIRE_MACHINE_GUN, k.FIRE_ACTIVE_WEAPON,
                 k.SWITCH_WEAPON, k.SPECIAL_ABILITY, k.PADLOCK_CAMERA}
    assert k.FINISH_ROUND_THEN_EXIT not in game_keys


# --- V8: config -------------------------------------------------------------

def test_close_game_can_be_disabled():
    import yaml
    cfg = yaml.safe_load(Path("wingman/config.yaml").read_text())["finish_round_then_exit"]
    assert cfg["close_game"] is True
    assert cfg["game_term_grace_s"] > 0
    src = MAIN_SRC
    assert '_fr_cfg.get("close_game", True)' in src, "close_game must be honoured"
