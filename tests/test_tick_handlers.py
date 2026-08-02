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


class _EnemyAnalyzerStub:
    def __init__(self, red=False):
        self._red = red

    def detect_enemy_red(self, _frame):
        return self._red


class _EnemyCtrlStub:
    def __init__(self, mission_running=True):
        self._running = mission_running
        self.disengages = 0

    def is_mission_running(self):
        return self._running

    def disengage_roll_right(self):
        self.disengages += 1


class TestEnemyPresence:
    def _h(self, red=False, mission_running=True, after=30.0):
        from wingman.tick_handlers import EnemyPresenceHandler
        a = _EnemyAnalyzerStub(red)
        c = _EnemyCtrlStub(mission_running)
        return EnemyPresenceHandler(a, c, disengage_after_s=after), c

    def test_inert_until_armed(self):
        h, c = self._h(after=0.0)
        assert h.tick(object(), GameState.GAME_BATTLE) is False
        assert c.disengages == 0  # clock never armed → no disengage

    def test_battle_entry_arms_the_clock(self):
        h, _ = self._h()
        h.on_state_change(GameState.GAME_BATTLE)
        assert h.last_seen_ts > 0

    def test_non_battle_entry_does_not_arm(self):
        h, _ = self._h()
        h.on_state_change(GameState.GAME_LOBBY)
        assert h.last_seen_ts == 0.0

    def test_red_seen_keeps_resetting_the_clock(self):
        h, c = self._h(red=True, after=0.0)
        h.arm()
        h.tick(object(), GameState.GAME_BATTLE)
        assert c.disengages == 0  # enemy present → never disengages

    def test_idle_window_triggers_disengage(self):
        h, c = self._h(red=False, after=0.0)
        h.arm()
        h.tick(object(), GameState.GAME_BATTLE)
        assert c.disengages == 1

    def test_no_disengage_without_running_mission(self):
        h, c = self._h(red=False, mission_running=False, after=0.0)
        h.arm()
        h.tick(object(), GameState.GAME_BATTLE)
        assert c.disengages == 0

    def test_disengage_resets_clock_so_it_does_not_repeat(self):
        h, c = self._h(red=False, after=5.0)
        h.arm()
        h._last_seen_ts = time.time() - 10.0
        h.tick(object(), GameState.GAME_BATTLE)
        h.tick(object(), GameState.GAME_BATTLE)
        assert c.disengages == 1  # second tick is inside the fresh window

    def test_inert_outside_battle(self):
        h, c = self._h(red=False, after=0.0)
        h.arm()
        h.tick(object(), GameState.GAME_BATTLE_MANUAL)
        assert c.disengages == 0


class _AmmoAnalyzerStub:
    def __init__(self, *, state=GameState.GAME_BATTLE, respawning=False,
                 incoming=False, incoming_ts=0.0):
        self.game_state = state
        self._respawning = respawning
        self._incoming = incoming
        self._incoming_ts = incoming_ts
        self.triggers = []
        self.low_flares_event = _EventStub()
        self.no_missiles_event = _EventStub()

    def get_respawn_cache_result(self):
        return (self._respawning, 0.0, None)

    def get_incoming_cache_result(self):
        return (self._incoming, 0.0, None)

    def get_incoming_cache_timestamp(self):
        return self._incoming_ts

    def trigger_event(self, name):
        self.triggers.append(name)
        return True


class _EventStub:
    def __init__(self):
        self._set = False

    def set(self):
        self._set = True

    def clear(self):
        self._set = False

    def is_set(self):
        return self._set


class _AmmoCtrlStub:
    def __init__(self, mission_running=True):
        self._running = mission_running
        self.reloads = 0
        self.ejects = 0
        self.padlock_switches = 0

    def is_mission_running(self):
        return self._running

    def reload_flares(self):
        self.reloads += 1

    def eject_and_dive(self, on_complete=None):
        self.ejects += 1

    def padlock_target_switch(self):
        self.padlock_switches += 1

    def deploy_flares(self, **kw):
        pass


def _ammo(analyzer=None, ctrl=None, **cfg):
    from wingman.tick_handlers import AmmoEventsHandler
    base = {"no_missiles_abort_grace_s": 0.0, "no_missiles_consecutive_required": 2}
    base.update(cfg)
    a = analyzer or _AmmoAnalyzerStub()
    c = ctrl or _AmmoCtrlStub()
    return AmmoEventsHandler(a, c, base), a, c


class TestNoMissiles:
    def test_requires_consecutive_confirmations(self):
        h, a, c = _ammo()
        h.handle_no_missiles()
        assert c.ejects == 0            # 1/2 — awaiting confirmation
        h.handle_no_missiles()
        assert c.ejects == 1            # 2/2 — fires
        assert a.triggers == ["eject_started"]

    def test_no_eject_without_running_mission(self):
        h, _, c = _ammo(ctrl=_AmmoCtrlStub(mission_running=False))
        h.handle_no_missiles()
        h.handle_no_missiles()
        assert c.ejects == 0

    def test_no_eject_outside_game_battle(self):
        """Eject is auto-mode only — must not inject into a manual flight."""
        h, _, c = _ammo(_AmmoAnalyzerStub(state=GameState.GAME_BATTLE_MANUAL))
        h.handle_no_missiles()
        h.handle_no_missiles()
        assert c.ejects == 0

    def test_respawn_screen_suppresses(self):
        h, _, c = _ammo(_AmmoAnalyzerStub(respawning=True))
        h.handle_no_missiles()
        h.handle_no_missiles()
        assert c.ejects == 0

    def test_post_respawn_window_suppresses(self):
        h, _, c = _ammo()
        h.suppress_after_respawn(60.0)
        h.handle_no_missiles()
        h.handle_no_missiles()
        assert c.ejects == 0

    def test_positive_missile_count_resets_the_streak(self):
        h, _, c = _ammo()
        h.handle_no_missiles()                                   # streak 1
        h.tick_missile_count(4, GameState.GAME_BATTLE)           # reload → reset
        h.handle_no_missiles()                                   # streak 1 again
        assert c.ejects == 0


class TestPadlockSpread:
    def test_two_missiles_fired_switches_target(self):
        h, _, c = _ammo()
        h.tick_missile_count(4, GameState.GAME_BATTLE)
        h.tick_missile_count(3, GameState.GAME_BATTLE)
        assert c.padlock_switches == 0
        h.tick_missile_count(2, GameState.GAME_BATTLE)
        assert c.padlock_switches == 1

    def test_reload_resets_the_partial_count(self):
        h, _, c = _ammo()
        h.tick_missile_count(4, GameState.GAME_BATTLE)
        h.tick_missile_count(3, GameState.GAME_BATTLE)   # 1 fired
        h.tick_missile_count(4, GameState.GAME_BATTLE)   # reloaded
        h.tick_missile_count(3, GameState.GAME_BATTLE)   # 1 fired since reload
        assert c.padlock_switches == 0

    def test_inert_outside_battle(self):
        h, _, c = _ammo()
        h.tick_missile_count(4, GameState.GAME_BATTLE_MANUAL)
        h.tick_missile_count(2, GameState.GAME_BATTLE_MANUAL)
        assert c.padlock_switches == 0


class TestFlares:
    def test_low_flares_reloads_once_within_cooldown(self):
        h, _, c = _ammo()
        h.handle_low_flares()
        h.handle_low_flares()
        assert c.reloads == 1

    def test_low_flares_inert_outside_battle(self):
        h, _, c = _ammo(_AmmoAnalyzerStub(state=GameState.GAME_LOBBY))
        h.handle_low_flares()
        assert c.reloads == 0

    def test_incoming_deploys_flares_once_per_detection(self):
        a = _AmmoAnalyzerStub(incoming=True, incoming_ts=100.0)
        h, _, _ = _ammo(a)
        assert h.deploy_flares_on_new_incoming() is True
        assert h.deploy_flares_on_new_incoming() is False   # same timestamp

    def test_incoming_suppressed_after_respawn(self):
        a = _AmmoAnalyzerStub(incoming=True, incoming_ts=100.0)
        h, _, _ = _ammo(a)
        h.suppress_after_respawn(60.0)
        assert h.deploy_flares_on_new_incoming() is False

    def test_tick_events_only_fires_set_events(self):
        a = _AmmoAnalyzerStub()
        h, _, c = _ammo(a)
        h.tick_events()
        assert c.reloads == 0
        a.low_flares_event.set()
        h.tick_events()
        assert c.reloads == 1
