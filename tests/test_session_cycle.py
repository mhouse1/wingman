"""Session cycle (ADR 202): stop after N rounds, close the game, wait, exit 75 for make r."""

import logging
import shutil
import subprocess
import threading
from pathlib import Path

import pytest
import yaml

from wingman import main as wingman_main
from wingman import session_cycle
from wingman.session_cycle import SessionCycle
from wingman.state import GameState as S

LOG = logging.getLogger("test_session_cycle")


def _round(cycle, *, stop_requested=False, via_unknown=False, end=S.GAME_END_B):
    """One round: lobby flow, battle, the end. Returns every note_state result."""
    seq = [S.GAME_WAITING, S.GAME_STARTING, S.GAME_BATTLE]
    if via_unknown:
        seq += [S.GAME_UNKNOWN, S.GAME_BATTLE]   # ADR 195: HUD gone mid-round
    seq += [end, S.GAME_LOBBY] if end == S.GAME_END_B else [end]
    return [cycle.note_state(st, stop_requested) for st in seq]


def test_stop_comes_at_the_end_of_the_second_round_and_only_then():
    c = SessionCycle(enabled=True, rounds=2, wait_s=120)
    assert not any(_round(c))
    assert c.finished == 1 and not c.pending
    results = _round(c)
    # True exactly once, on GAME_END_B; the lobby after it is the same round.
    assert results.count(True) == 1 and results[3] is True
    assert c.finished == 2 and c.pending
    assert not any(_round(c))          # already pending: no second request


def test_unknown_mid_round_does_not_end_the_round():
    c = SessionCycle(enabled=True, rounds=2, wait_s=0)
    _round(c, via_unknown=True)
    assert c.finished == 1


def test_a_round_whose_end_screen_was_missed_ends_at_the_lobby():
    c = SessionCycle(enabled=True, rounds=1, wait_s=0)
    assert _round(c, end=S.GAME_LOBBY)[-1] is True


def test_lobby_without_a_battle_is_not_a_round():
    c = SessionCycle(enabled=True, rounds=1, wait_s=0)
    for st in (S.GAME_UNKNOWN, S.GAME_LOBBY, S.GAME_WAITING, S.GAME_STARTING,
               S.GAME_LOBBY, S.GAME_END_B):
        assert c.note_state(st, False) is False
    assert c.finished == 0


def test_the_operators_own_stop_is_not_taken_for_the_cycles():
    c = SessionCycle(enabled=True, rounds=1, wait_s=0)
    assert not any(_round(c, stop_requested=True))
    assert not c.should_restart(finish_round_exit=True)


def test_operator_cancel_turns_the_cycle_off_for_the_session():
    c = SessionCycle(enabled=True, rounds=1, wait_s=0)
    _round(c)
    assert c.note_stop_flag(True) is False         # still pending
    assert c.note_stop_flag(False) is True         # 'z' cleared it
    assert c.note_stop_flag(False) is False        # said once
    assert not c.enabled and not c.should_restart(True)
    assert not any(_round(c))


def test_restart_needs_the_finish_round_exit():
    c = SessionCycle(enabled=True, rounds=1, wait_s=0)
    _round(c)
    assert c.should_restart(True) and not c.should_restart(False)


def test_disabled_never_stops():
    c = SessionCycle(enabled=False, rounds=1, wait_s=0)
    assert not any(_round(c) + _round(c))


def test_replay_and_capture_runs_never_cycle():
    section = {"enabled": True, "rounds": 1, "wait_s": 0}
    assert SessionCycle.from_config(section, live=True).enabled
    assert not SessionCycle.from_config(section, live=False).enabled


def test_schema_default_is_off():
    assert not SessionCycle.from_config({}, live=True).enabled


def test_shipped_config_cycles_after_thirty_rounds_and_two_minutes():
    cfg = yaml.safe_load(Path("wingman/config.yaml").read_text())["session_cycle"]
    c = SessionCycle.from_config(cfg, live=True)
    assert (c.enabled, c.rounds, c.wait_s) == (True, 30, 120.0)


# --- the wait in main -------------------------------------------------------

def test_wait_runs_out_and_rearms_the_watchdog(monkeypatch):
    calls = []
    monkeypatch.setattr(wingman_main, "_cancel_shutdown_watchdog", lambda: calls.append("cancel"))
    monkeypatch.setattr(wingman_main, "_arm_shutdown_watchdog", lambda: calls.append("arm"))
    c = SessionCycle(enabled=True, rounds=1, wait_s=0.01)
    assert wingman_main._cycle_wait(c, threading.Event(), LOG) is True
    assert calls == ["cancel", "arm"]


def test_a_signal_during_the_wait_ends_the_loop(monkeypatch):
    monkeypatch.setattr(wingman_main, "_cancel_shutdown_watchdog", lambda: None)
    monkeypatch.setattr(wingman_main, "_arm_shutdown_watchdog", lambda: None)
    ev = threading.Event()
    ev.set()
    c = SessionCycle(enabled=True, rounds=1, wait_s=60)
    assert wingman_main._cycle_wait(c, ev, LOG) is False


def test_main_exits_with_the_cycle_code():
    src = Path("wingman/main.py").read_text()
    assert "sys.exit(session_cycle.EXIT_CODE)" in src
    assert session_cycle.EXIT_CODE == 75


# --- the make loop ----------------------------------------------------------

@pytest.mark.skipif(shutil.which("make") is None, reason="make not installed")
def test_make_r_loops_on_75_and_dry_run_starts_nothing():
    """make -n prints the loop without running it, and it relaunches on 75 only.

    A recipe line naming $(MAKE) runs even under -n; this one started wingman on
    2026-11-23 before CYCLE_MAKE replaced it.
    """
    out = subprocess.run(["make", "-n", "r", "NESTED=0"], capture_output=True,
                         text=True, timeout=60).stdout
    assert "while [ $status -eq 75 ]" in out
    assert "round-scores: " not in out           # the ingest did not run
    recipe = Path("Makefile").read_text().split("\nr: $(GAME_LAUNCH_DEPS)\n", 1)[1]
    recipe = recipe.split("\n\n", 1)[0]
    assert "$(MAKE)" not in recipe and "$(CYCLE_MAKE)" in recipe
