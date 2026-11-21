"""ADR 195: a battle state that is not a battle finds its way back.

2026-11-19 07:04:12: a false start (lobby digits read as health) put the FSM in
GAME_BATTLE, then GAME_BATTLE_EJECT, over the lobby's PLAY button. Nothing in a
battle state looked for the lobby, and the session sat there for minutes while
FR-012 warned that every weapon-panel crop was silent. Speed read 4 and altitude
nothing the whole time; health read 3, 7 and 97 off the lobby.
"""

import copy
import time
from concurrent.futures import Future
from pathlib import Path

import pytest
import yaml

from constants import CONFIG_PATH
from wingman.analyzer import (
    BATTLE_HUD_LOST_NO_TELEMETRY_S,
    BATTLE_LOBBY_NO_TELEMETRY_S,
    STARTING_PLAY_CONFIRM_READS,
    GameState,
    GameStateAnalyzer,
    LOBBY_RECHECK_STATES,
)


@pytest.fixture
def analyzer():
    with Path(CONFIG_PATH).open("r", encoding="utf-8") as fh:
        a = GameStateAnalyzer(copy.deepcopy(yaml.safe_load(fh)))
    a.state = GameState.GAME_BATTLE_EJECT.name
    try:
        yield a
    finally:
        a.cleanup()


def _futures(play: bool) -> dict:
    out = {}
    for crop in ("PLAY", "READY"):
        f = Future()
        f.set_result((play and crop == "PLAY", 0.9, "PLAY" if play else ""))
        out[crop] = f
    return out


def _reads(a, n, play=True):
    for _ in range(n):
        a._battle_lobby_check(a.game_state, _futures(play))


def _stale(a, seconds):
    a._hud_telemetry_ts = time.time() - seconds


# --- PLAY in a battle state -----------------------------------------------------

def test_play_on_six_reads_with_no_telemetry_is_the_lobby(analyzer):
    _stale(analyzer, BATTLE_LOBBY_NO_TELEMETRY_S + 1)
    _reads(analyzer, STARTING_PLAY_CONFIRM_READS - 1)
    assert analyzer.game_state == GameState.GAME_BATTLE_EJECT
    _reads(analyzer, 1)
    assert analyzer.game_state == GameState.GAME_LOBBY


def test_a_play_misread_in_a_live_battle_resets_nothing(analyzer):
    """ADR 102's rule: speed or altitude read recently means the battle is real."""
    _stale(analyzer, 1.0)
    _reads(analyzer, STARTING_PLAY_CONFIRM_READS * 3)
    assert analyzer.game_state == GameState.GAME_BATTLE_EJECT


def test_a_frame_without_play_restarts_the_count(analyzer):
    _stale(analyzer, BATTLE_LOBBY_NO_TELEMETRY_S + 1)
    _reads(analyzer, STARTING_PLAY_CONFIRM_READS - 1)
    _reads(analyzer, 1, play=False)
    _reads(analyzer, STARTING_PLAY_CONFIRM_READS - 1)
    assert analyzer.game_state == GameState.GAME_BATTLE_EJECT


def test_the_starting_recheck_set_is_unchanged():
    """The battle check is its own path; ADR 102's set stays GAME_STARTING only."""
    assert LOBBY_RECHECK_STATES == (GameState.GAME_STARTING,)


# --- the telemetry clock ------------------------------------------------------------

def _land_telemetry(a, speed, altitude):
    f = Future()
    f.set_result((speed, altitude, 0.0))
    with a._telemetry_lock:
        a._telemetry_future = f
    a._on_telemetry_done(f)


def test_the_lobbys_speed_reading_does_not_count_as_telemetry(analyzer):
    """07:04 to 07:08: the lobby gave speed 4 and no altitude."""
    _stale(analyzer, 30.0)
    _land_telemetry(analyzer, 4, None)
    assert analyzer.hud_telemetry_age_s() >= 29.0


def test_a_real_reading_resets_the_clock(analyzer):
    _stale(analyzer, 30.0)
    _land_telemetry(analyzer, 738, 940)
    assert analyzer.hud_telemetry_age_s() < 1.0


def test_entering_battle_gives_a_fresh_span(analyzer):
    _stale(analyzer, 300.0)
    analyzer.on_enter_GAME_BATTLE()
    assert analyzer.hud_telemetry_age_s() < 1.0


# --- the weapon-panel backstop ----------------------------------------------------

def _silence(a, labels=("WEAPON_NAME", "AMMO_FLARES", "AMMO_MISSILE")):
    for label in labels:
        a._note_panel_silent(label)


def test_three_silent_crops_and_no_telemetry_reclassify(analyzer):
    _stale(analyzer, BATTLE_HUD_LOST_NO_TELEMETRY_S + 1)
    _silence(analyzer)
    assert analyzer.game_state == GameState.GAME_UNKNOWN


def test_silent_crops_with_live_telemetry_are_a_moved_panel(analyzer, caplog):
    """The FR-012 case of 2026-11-18: the panel moved, the battle was real."""
    _stale(analyzer, 2.0)
    with caplog.at_level("INFO", logger="wingman.analyzer"):
        _silence(analyzer)
    assert analyzer.game_state == GameState.GAME_BATTLE_EJECT
    assert any("the panel moved" in r.getMessage() for r in caplog.records)


def test_two_silent_crops_are_not_enough(analyzer):
    _stale(analyzer, BATTLE_HUD_LOST_NO_TELEMETRY_S + 1)
    _silence(analyzer, ("WEAPON_NAME", "AMMO_FLARES"))
    assert analyzer.game_state == GameState.GAME_BATTLE_EJECT


def test_the_backstop_only_acts_in_a_battle_state(analyzer):
    analyzer.state = GameState.GAME_LOBBY.name
    _stale(analyzer, BATTLE_HUD_LOST_NO_TELEMETRY_S + 1)
    _silence(analyzer)
    assert analyzer.game_state == GameState.GAME_LOBBY
