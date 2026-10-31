"""Code Review 019 (2026-10-19): the matchmaking panel's mode and map.

The JA37's crashes that no altitude alarm saw coming flew level into ground
1,000 to 1,600 m high: a sea-level floor cannot see it, a floor per map could.
The map is printed above CANCEL while matchmaking ("TEAM DEATHMATCH" over
"ICEBOUND"); wingman now reads it once per matchmaking and logs
``MATCH: mode=... map=...``, so each match's crashes can be put against its map.
"""

import logging
from concurrent.futures import Future
from pathlib import Path
from unittest.mock import MagicMock

import cv2
import numpy as np
import pytest
import yaml

from wingman.analyzer import _crop_for_ocr, _mode_and_map, _read_text_lines
from wingman.state import GameState
from wingman.tick_handlers import WaitingFallbackHandler
from constants import CONFIG_PATH

SHOT = Path("test_screenshots/MATCHMAKING_ICEBOUND.png")


def load_config() -> dict:
    with CONFIG_PATH.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


class _PendingExecutor:
    def __init__(self):
        self.futures = []

    def submit(self, fn, *args):
        fut = Future()
        self.futures.append(fut)
        return fut


@pytest.fixture
def analyzer(monkeypatch):
    from wingman.analyzer import GameStateAnalyzer
    a = GameStateAnalyzer(load_config())
    pool = _PendingExecutor()
    monkeypatch.setattr(type(a), "ocr_executor", property(lambda self: pool))
    a._test_pool = pool
    try:
        yield a
    finally:
        a.cleanup()


def _frame():
    return np.zeros((1200, 1920, 3), dtype=np.uint8)


def test_the_two_lines_are_mode_then_map():
    assert _mode_and_map(["TEAM DEATHMATCH", "ICEBOUND"]) == ("TEAM DEATHMATCH", "ICEBOUND")
    assert _mode_and_map(["ICEBOUND"]) == (None, "ICEBOUND")
    assert _mode_and_map([]) == (None, None)


def test_one_read_names_the_map_and_logs_it(analyzer, caplog):
    analyzer.note_matchmaking_frame(_frame())
    analyzer.note_matchmaking_frame(_frame())     # a read is already on its way
    assert len(analyzer._test_pool.futures) == 1
    with caplog.at_level(logging.INFO):
        analyzer._test_pool.futures[0].set_result(["TEAM DEATHMATCH", "ICEBOUND"])
    assert analyzer.match_map == "ICEBOUND"
    assert analyzer.match_mode == "TEAM DEATHMATCH"
    assert any("MATCH: mode=TEAM DEATHMATCH map=ICEBOUND" in r.getMessage() for r in caplog.records)
    analyzer._match_map_last_read_ts = 0.0
    analyzer.note_matchmaking_frame(_frame())     # known: no more reads
    assert len(analyzer._test_pool.futures) == 1


def test_an_empty_read_is_retried_up_to_three_times(analyzer):
    for _ in range(5):
        analyzer._match_map_last_read_ts = 0.0     # past the retry spacing
        analyzer.note_matchmaking_frame(_frame())
        if analyzer._test_pool.futures and not analyzer._test_pool.futures[-1].done():
            analyzer._test_pool.futures[-1].set_result([])
    assert len(analyzer._test_pool.futures) == 3
    assert analyzer.match_map is None


def test_the_lobby_starts_the_next_matchmaking_afresh(analyzer):
    analyzer.note_matchmaking_frame(_frame())
    analyzer._test_pool.futures[0].set_result(["TEAM DEATHMATCH", "ICEBOUND"])
    analyzer.on_enter_GAME_LOBBY()
    assert analyzer.match_map is None
    analyzer.note_matchmaking_frame(_frame())
    assert len(analyzer._test_pool.futures) == 2


def test_the_waiting_tick_asks_for_the_read():
    analyzer = MagicMock()
    analyzer.scan_region_for_cancel.return_value = False
    analyzer.scan_region_for_play_button.return_value = None
    analyzer.crops = {}
    handler = WaitingFallbackHandler(analyzer, MagicMock(), {})
    handler.on_state_change(GameState.GAME_WAITING)
    frame = _frame()
    handler.tick(frame, GameState.GAME_WAITING)
    analyzer.note_matchmaking_frame.assert_called_once_with(frame)
    handler.tick(frame, GameState.GAME_LOBBY)
    assert analyzer.note_matchmaking_frame.call_count == 1


@pytest.mark.slow
def test_the_crop_reads_the_real_panel():
    if not SHOT.exists():
        pytest.skip(f"{SHOT} not present")
    crop = load_config()["crops"]["MATCH_MAP"]["coords"]
    img = cv2.imread(str(SHOT))
    lines = _read_text_lines(_crop_for_ocr(img, (crop[0][0], crop[0][1], crop[1][0], crop[1][1])))
    assert _mode_and_map(lines) == ("TEAM DEATHMATCH", "ICEBOUND"), lines
