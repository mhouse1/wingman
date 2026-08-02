"""Behavioural tests for the ADR 060 Phase 2 tick-loop handlers.

Each handler is driven directly with stub collaborators — no capture, no OCR,
no main loop — which is the testability the extraction exists to buy.

Usage: uv run pytest tests/test_tick_handlers.py -q
"""

import time
from types import SimpleNamespace

import pytest

from wingman.analyzer import GameState
from wingman.tick_handlers import WaitingFallbackHandler


class _AnalyzerStub:
    def __init__(self, *, cancel=False, play_crop=None, diff=None):
        self.crops = {"CANCEL": object(), "PLAY": object()}
        self._cancel = cancel
        self._play_crop = play_crop
        self._diff = diff
        self.triggers = []

    def scan_region_for_cancel(self, _frame):
        return self._cancel

    def scan_region_for_play_button(self, _frame):
        return self._play_crop

    def compute_waiting_cancel_diff(self, _frame):
        return self._diff

    def trigger_event(self, name):
        self.triggers.append(name)
        return True


class _CtrlStub:
    def __init__(self):
        self.clicks = []

    def click_crop(self, crop, **kw):
        self.clicks.append(kw.get("region_name"))


def _handler(analyzer, ctrl=None, **cfg):
    base = {"waiting_fallback_min_elapsed_s": 0.0, "waiting_fallback_score_threshold": 2,
            "waiting_fallback_consecutive_required": 1, "waiting_fallback_diff_threshold": 0.08}
    base.update(cfg)
    return WaitingFallbackHandler(analyzer, ctrl or _CtrlStub(), base,
                                  cancel_scan_interval_s=0.0)


class TestArming:
    def test_entering_waiting_arms_the_clock(self):
        h = _handler(_AnalyzerStub())
        h.on_state_change(GameState.GAME_WAITING)
        assert h.waiting_since > 0

    def test_leaving_waiting_clears_the_clock(self):
        h = _handler(_AnalyzerStub())
        h.on_state_change(GameState.GAME_WAITING)
        h.on_state_change(GameState.GAME_LOBBY)
        assert h.waiting_since == 0.0

    def test_tick_is_inert_outside_waiting(self):
        a = _AnalyzerStub(cancel=True)
        h = _handler(a)
        h.on_state_change(GameState.GAME_BATTLE)
        assert h.tick(object(), GameState.GAME_BATTLE) is False
        assert a.triggers == []


class TestCancelDetection:
    def test_cancel_detected_triggers_transition(self):
        a = _AnalyzerStub(cancel=True)
        h = _handler(a)
        h.on_state_change(GameState.GAME_WAITING)
        assert h.tick(object(), GameState.GAME_WAITING) is False
        assert a.triggers == ["cancel_detected"]

    def test_timeout_returns_to_lobby(self):
        a = _AnalyzerStub()
        h = _handler(a)
        h.on_state_change(GameState.GAME_WAITING)
        h._waiting_since = time.time() - 200.0  # past the 180s timeout
        h.tick(object(), GameState.GAME_WAITING)
        assert a.triggers == ["waiting_timeout"]
        assert h.waiting_since == 0.0


class TestQueueFallback:
    def test_fallback_promotes_and_requests_continue(self):
        """Score 2 per tick (diff over threshold) with threshold 2 → fires at once."""
        a = _AnalyzerStub(diff=0.5)
        h = _handler(a)
        h.on_state_change(GameState.GAME_WAITING)
        assert h.tick(object(), GameState.GAME_WAITING) is True  # loop must `continue`
        assert a.triggers == ["cancel_detected"]

    def test_visible_play_resets_the_score(self):
        a = _AnalyzerStub(diff=0.5, play_crop="PLAY")
        h = _handler(a, waiting_fallback_score_threshold=99)
        h.on_state_change(GameState.GAME_WAITING)
        h.tick(object(), GameState.GAME_WAITING)
        assert h._score == 0
        assert a.triggers == []

    def test_disabled_fallback_never_promotes(self):
        a = _AnalyzerStub(diff=0.9)
        h = _handler(a, waiting_fallback_enabled=False)
        h.on_state_change(GameState.GAME_WAITING)
        assert h.tick(object(), GameState.GAME_WAITING) is False
        assert a.triggers == []


class TestPlayReclick:
    def test_visible_play_is_reclicked_after_interval(self):
        a = _AnalyzerStub(play_crop="PLAY")
        ctrl = _CtrlStub()
        h = _handler(a, ctrl, waiting_fallback_enabled=False, play_reclick_missed_interval=0.0)
        h.on_state_change(GameState.GAME_WAITING)
        h.tick(object(), GameState.GAME_WAITING)
        assert ctrl.clicks == ["PLAY"]

    def test_absent_play_is_not_clicked(self):
        """Clicking PLAY during matchmaking cancels it — never click blind."""
        a = _AnalyzerStub(play_crop=None)
        ctrl = _CtrlStub()
        h = _handler(a, ctrl, waiting_fallback_enabled=False, play_reclick_missed_interval=0.0)
        h.on_state_change(GameState.GAME_WAITING)
        h.tick(object(), GameState.GAME_WAITING)
        assert ctrl.clicks == []

    def test_state_is_private_to_the_handler(self):
        """ADR 060 rule 2: no other concern can reach this handler's state."""
        h = _handler(_AnalyzerStub())
        h.on_state_change(GameState.GAME_WAITING)
        other = _handler(_AnalyzerStub())
        assert other.waiting_since == 0.0  # independent instances share nothing
