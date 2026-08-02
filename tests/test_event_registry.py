"""Unit tests for the ADR 060 Phase 1 orchestration event registry.

Covers the three properties the single-slot `set_on_*` setters lacked:
multi-subscriber fan-out, registration-time failure on duplicates/bad events,
and per-subscriber exception isolation.

Usage: uv run pytest tests/test_event_registry.py -q
"""

import copy
from pathlib import Path

import pytest
import yaml

from wingman.analyzer import GameStateAnalyzer, GameEvent, GameState
from constants import CONFIG_PATH


def load_config(path: Path = CONFIG_PATH) -> dict:
    with path.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


@pytest.fixture
def analyzer():
    a = GameStateAnalyzer(copy.deepcopy(load_config()))
    try:
        yield a
    finally:
        a.cleanup()


class TestSubscribe:
    def test_single_subscriber_receives_payload(self, analyzer):
        seen = []
        analyzer.subscribe(GameEvent.LOBBY_POPUP_CLICK, seen.append, name="t")
        analyzer.emit(GameEvent.LOBBY_POPUP_CLICK, "INVITED")
        assert seen == ["INVITED"]

    def test_multi_subscriber_fan_out(self, analyzer):
        """The property that fixes stats-not-recorded-during-replay."""
        a, b, c = [], [], []
        analyzer.subscribe(GameEvent.FSM_TRANSITION, lambda *x: a.append(x), name="replay")
        analyzer.subscribe(GameEvent.FSM_TRANSITION, lambda *x: b.append(x), name="capture")
        analyzer.subscribe(GameEvent.FSM_TRANSITION, lambda *x: c.append(x), name="stats")
        analyzer.emit(GameEvent.FSM_TRANSITION, "trig", "A", "B", 1.0)
        assert a and b and c
        assert a[0] == ("trig", "A", "B", 1.0)

    def test_duplicate_name_raises(self, analyzer):
        analyzer.subscribe(GameEvent.LOBBY_STALL, lambda: None, name="dup")
        with pytest.raises(ValueError, match="already registered"):
            analyzer.subscribe(GameEvent.LOBBY_STALL, lambda: None, name="dup")

    def test_duplicate_name_replaces_when_asked(self, analyzer):
        calls = []
        analyzer.subscribe(GameEvent.LOBBY_STALL, lambda: calls.append("first"), name="x")
        analyzer.subscribe(GameEvent.LOBBY_STALL, lambda: calls.append("second"), name="x",
                           replace=True)
        analyzer.emit(GameEvent.LOBBY_STALL)
        assert calls == ["second"]

    def test_same_name_different_events_is_fine(self, analyzer):
        analyzer.subscribe(GameEvent.LOBBY_STALL, lambda: None, name="main")
        analyzer.subscribe(GameEvent.CANCEL_MISSION, lambda: None, name="main")  # no raise

    def test_non_event_raises_at_registration(self, analyzer):
        with pytest.raises(TypeError):
            analyzer.subscribe("LOBBY_STALL", lambda: None, name="t")

    def test_non_callable_raises_at_registration(self, analyzer):
        with pytest.raises(TypeError):
            analyzer.subscribe(GameEvent.LOBBY_STALL, "not callable", name="t")


class TestEmit:
    def test_emit_with_no_subscribers_is_a_noop(self, analyzer):
        analyzer.emit(GameEvent.RESPAWN_DETECTED, object())  # must not raise

    def test_subscriber_exception_is_isolated(self, analyzer):
        """One failing subscriber must not stop the others (the try/except that
        used to be copy-pasted at every _on_* call site)."""
        survived = []

        def boom(*_):
            raise RuntimeError("subscriber blew up")

        analyzer.subscribe(GameEvent.FSM_TRANSITION, boom, name="bad")
        analyzer.subscribe(GameEvent.FSM_TRANSITION, lambda *x: survived.append(x), name="good")
        analyzer.emit(GameEvent.FSM_TRANSITION, "t", "A", "B", 1.0)  # must not raise
        assert len(survived) == 1

    def test_subscriber_may_subscribe_during_dispatch(self, analyzer):
        """Dispatch runs on a snapshot outside the lock — no deadlock, and the
        new subscriber takes effect on the NEXT emit."""
        later = []

        def adder(*_):
            analyzer.subscribe(GameEvent.LOBBY_STALL, lambda: later.append(1), name="late",
                               replace=True)

        analyzer.subscribe(GameEvent.LOBBY_STALL, adder, name="adder")
        analyzer.emit(GameEvent.LOBBY_STALL)
        assert later == []
        analyzer.emit(GameEvent.LOBBY_STALL)
        assert later == [1]

    def test_has_subscribers_and_unsubscribe(self, analyzer):
        assert analyzer.has_subscribers(GameEvent.LOBBY_STALL) is False
        analyzer.subscribe(GameEvent.LOBBY_STALL, lambda: None, name="t")
        assert analyzer.has_subscribers(GameEvent.LOBBY_STALL) is True
        assert analyzer.unsubscribe(GameEvent.LOBBY_STALL, name="t") is True
        assert analyzer.has_subscribers(GameEvent.LOBBY_STALL) is False
        assert analyzer.unsubscribe(GameEvent.LOBBY_STALL, name="t") is False


class TestLegacyShims:
    """ADR 039 setters keep single-slot replace semantics during migration."""

    def test_setter_registers_and_fires(self, analyzer):
        seen = []
        analyzer.set_on_lobby_popup_click(seen.append)
        analyzer.emit(GameEvent.LOBBY_POPUP_CLICK, "SILVER")
        assert seen == ["SILVER"]

    def test_setter_called_twice_replaces_not_duplicates(self, analyzer):
        calls = []
        analyzer.set_on_lobby_stall(lambda: calls.append("a"))
        analyzer.set_on_lobby_stall(lambda: calls.append("b"))
        analyzer.emit(GameEvent.LOBBY_STALL)
        assert calls == ["b"]

    def test_setter_and_named_subscriber_coexist(self, analyzer):
        calls = []
        analyzer.set_on_lobby_stall(lambda: calls.append("legacy"))
        analyzer.subscribe(GameEvent.LOBBY_STALL, lambda: calls.append("new"), name="new")
        analyzer.emit(GameEvent.LOBBY_STALL)
        assert sorted(calls) == ["legacy", "new"]


class TestFsmIntegration:
    def test_transition_emits_fsm_event(self, analyzer):
        seen = []
        analyzer.subscribe(GameEvent.FSM_TRANSITION, lambda *x: seen.append(x), name="t")
        analyzer.state = GameState.GAME_LOBBY.name
        analyzer.trigger_event("play_clicked")
        assert seen, "FSM_TRANSITION should fire on a successful transition"
        trigger, prev, nxt, _ts = seen[0]
        assert trigger == "play_clicked"
        assert prev == GameState.GAME_LOBBY.name
        assert nxt == GameState.GAME_WAITING.name

    def test_invalid_trigger_emits_nothing(self, analyzer):
        seen = []
        analyzer.subscribe(GameEvent.FSM_TRANSITION, lambda *x: seen.append(x), name="t")
        analyzer.state = GameState.GAME_LOBBY.name
        analyzer.trigger_event("respawn_reset")  # not valid from GAME_LOBBY
        assert seen == []

    def test_cancel_mission_emitted_on_lobby_entry(self, analyzer):
        calls = []
        analyzer.subscribe(GameEvent.CANCEL_MISSION, lambda: calls.append(1), name="ctrl")
        analyzer.state = GameState.GAME_END_B.name
        analyzer.trigger_event("continue_clicked")
        assert calls == [1]
