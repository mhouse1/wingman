"""Behavioural tests for the ADR 060 Phase 2 tick-loop handlers.

Each handler is driven directly with stub collaborators — no capture, no OCR,
no main loop — which is the testability the extraction exists to buy.

Usage: uv run pytest tests/test_tick_handlers.py -q
"""

import time
from types import SimpleNamespace


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


def _ammo(analyzer=None, ctrl=None, bt_owns_eject=False, **cfg):
    from wingman.tick_handlers import AmmoEventsHandler
    base = {"no_missiles_abort_grace_s": 0.0, "no_missiles_consecutive_required": 2}
    base.update(cfg)
    a = analyzer or _AmmoAnalyzerStub()
    c = ctrl or _AmmoCtrlStub()
    return AmmoEventsHandler(a, c, base, bt_owns_eject=bt_owns_eject), a, c


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


class TestNoMissilesBtOwnsEject:
    """ADR 024 3.1b: with bt_owns_eject the handler keeps the debounce and
    every suppression gate but hands the confirmed verdict to the Eject leaf
    instead of actuating."""

    def test_confirmation_raises_flag_without_actuating(self):
        h, a, c = _ammo(bt_owns_eject=True)
        h.handle_no_missiles()
        assert h.consume_missiles_empty_confirmed() is False  # 1/2 — not yet
        h.handle_no_missiles()
        assert c.ejects == 0                                  # BT owns actuation
        assert a.triggers == []                               # no FSM event either
        assert h.consume_missiles_empty_confirmed() is True
        assert h.consume_missiles_empty_confirmed() is False  # consumed once

    def test_fire_eject_actuates_the_shared_path(self):
        h, a, c = _ammo(bt_owns_eject=True)
        h.fire_eject()
        assert c.ejects == 1
        assert a.triggers == ["eject_started"]

    def test_suppression_gates_still_apply(self):
        h, _, c = _ammo(bt_owns_eject=True)
        h.suppress_after_respawn(60.0)
        h.handle_no_missiles()
        h.handle_no_missiles()
        assert h.consume_missiles_empty_confirmed() is False

    def test_respawn_suppression_clears_a_pending_verdict(self):
        """A confirmed-but-unconsumed verdict must not survive into the next
        life — the exact stale-ammo hazard the 3.1b gate exists for."""
        h, _, _ = _ammo(bt_owns_eject=True)
        h.handle_no_missiles()
        h.handle_no_missiles()
        h.suppress_after_respawn(10.0)
        assert h.consume_missiles_empty_confirmed() is False

    def test_state_change_clears_a_pending_verdict(self):
        h, _, _ = _ammo(bt_owns_eject=True)
        h.handle_no_missiles()
        h.handle_no_missiles()
        h.on_state_change(GameState.GAME_BATTLE)
        assert h.consume_missiles_empty_confirmed() is False


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


class _RespawnAnalyzerStub:
    def __init__(self, *, state=GameState.GAME_BATTLE, missiles=4,
                 alive_after_observed_death=False):
        self.game_state = state
        self.alive_after_observed_death = alive_after_observed_death
        self._missiles = missiles
        self.alive_event = _EventStub()
        self.health_respawn_event = _EventStub()
        self.triggers = []
        self.health_resets = 0

    def get_ammo_missiles(self):
        return self._missiles

    def reset_health_for_respawn(self):
        self.health_resets += 1

    def trigger_event(self, name):
        self.triggers.append(name)
        return True


class _RespawnCtrlStub:
    def __init__(self, *, mission_running=False, teardown=False, auto_restart=True):
        self._running = mission_running
        self._teardown = teardown
        self._auto = auto_restart
        self.restarts = 0
        self.eject_stops = 0
        self.cancels = 0
        self.spawn_guard_starts = 0
        self.spawn_alive_notifies = 0

    def is_mission_running(self):
        return self._running

    def is_mission_teardown_in_progress(self):
        return self._teardown

    def is_auto_respawn_restart_enabled(self):
        return self._auto

    def restart_last_mission(self):
        self.restarts += 1

    def stop_eject_sequence(self, **kw):
        self.eject_stops += 1

    def cancel_mission(self):
        self.cancels += 1

    def set_auto_respawn_restart(self, v):
        self._auto = v

    def start_spawn_guard(self):
        self.spawn_guard_starts += 1

    def notify_spawn_alive(self):
        self.spawn_alive_notifies += 1


def _respawn(analyzer=None, ctrl=None, *, stability_s=0.0, enemy=None, ammo=None):
    from wingman.main import RespawnState, _alive_transition_disposition
    from wingman.tick_handlers import RespawnHandler
    a = analyzer or _RespawnAnalyzerStub()
    c = ctrl or _RespawnCtrlStub()
    enemy = enemy or SimpleNamespace(arm=lambda: None)
    ammo = ammo or SimpleNamespace(suppress_after_respawn=lambda s: None)
    h = RespawnHandler(a, c, {"respawn_clear_stability_s": stability_s},
                       enemy_presence=enemy, ammo_events=ammo,
                       disposition_fn=_alive_transition_disposition,
                       respawn_state_enum=RespawnState)
    return h, a, c


class TestAliveTransitionFlow:
    """ADR 059/061: the alive event is the only restart path and is never
    consumed silently."""

    def test_restarts_when_clear_and_stable(self):
        h, a, c = _respawn()
        h.note_respawn_screen(False)   # arms the clear clock
        h.handle_alive_transition()
        assert c.restarts == 1

    def test_deferral_rearms_the_one_shot_event(self):
        """Respawn screen still up → must re-arm, never swallow (ADR 059)."""
        h, a, c = _respawn()
        h.note_respawn_screen(True)    # clear clock == 0
        h.handle_alive_transition()
        assert c.restarts == 0
        assert a.alive_event.is_set()  # re-armed for the next tick

    def test_stability_window_defers_and_rearms(self):
        h, a, c = _respawn(stability_s=999.0)
        h.note_respawn_screen(False)
        h.handle_alive_transition()
        assert c.restarts == 0
        assert a.alive_event.is_set()

    def test_eject_with_observed_death_terminates_eject_and_rearms(self):
        """ADR 061: the missed-overlay respawn case."""
        a = _RespawnAnalyzerStub(state=GameState.GAME_BATTLE_EJECT,
                                 alive_after_observed_death=True)
        h, a, c = _respawn(a)
        h.note_respawn_screen(False)
        h.handle_alive_transition()
        assert c.eject_stops == 1
        assert a.alive_event.is_set()   # restart fires once state returns to battle
        assert c.restarts == 0

    def test_eject_without_observed_death_is_consumed(self):
        a = _RespawnAnalyzerStub(state=GameState.GAME_BATTLE_EJECT,
                                 alive_after_observed_death=False)
        h, a, c = _respawn(a)
        h.note_respawn_screen(False)
        h.handle_alive_transition()
        assert c.eject_stops == 0
        assert not a.alive_event.is_set()

    def test_manual_state_never_restarts(self):
        a = _RespawnAnalyzerStub(state=GameState.GAME_BATTLE_MANUAL)
        h, a, c = _respawn(a)
        h.note_respawn_screen(False)
        h.handle_alive_transition()
        assert c.restarts == 0

    def test_teardown_race_defers_and_rearms(self):
        """v1.6.29 race: lock held by a CANCELLED mission thread."""
        h, a, c = _respawn(ctrl=_RespawnCtrlStub(mission_running=True, teardown=True))
        h.note_respawn_screen(False)
        h.handle_alive_transition()
        assert c.restarts == 0
        assert a.alive_event.is_set()

    def test_genuine_running_mission_consumes_without_rearm(self):
        h, a, c = _respawn(ctrl=_RespawnCtrlStub(mission_running=True, teardown=False))
        h.note_respawn_screen(False)
        h.handle_alive_transition()
        assert c.restarts == 0
        assert not a.alive_event.is_set()

    def test_empty_missiles_skips_restart(self):
        h, a, c = _respawn(_RespawnAnalyzerStub(missiles=0))
        h.note_respawn_screen(False)
        h.handle_alive_transition()
        assert c.restarts == 0

    def test_auto_restart_disabled_consumes(self):
        h, a, c = _respawn(ctrl=_RespawnCtrlStub(auto_restart=False))
        h.note_respawn_screen(False)
        h.handle_alive_transition()
        assert c.restarts == 0


class TestRespawnDetection:
    def _gs(self, respawning=True):
        return {"is_respawning": respawning, "respawn_confidence": 1.0}

    def test_detection_runs_the_flow_and_requests_continue(self):
        h, a, c = _respawn()
        assert h.tick_detect(object(), self._gs(True), GameState.GAME_BATTLE) is True
        assert c.cancels == 1
        assert a.health_resets == 1
        assert not a.alive_event.is_set()   # stale pending event cleared

    def test_no_respawn_is_a_noop(self):
        h, a, c = _respawn()
        assert h.tick_detect(object(), self._gs(False), GameState.GAME_BATTLE) is False
        assert c.cancels == 0

    def test_eject_is_stopped_even_when_cooldown_suppresses_restart(self):
        """CR-013-4 regression: the eject interrupt must not be nested under
        the restart dedup cooldown."""
        h, a, c = _respawn()
        h.tick_detect(object(), self._gs(True), GameState.GAME_BATTLE)   # arms cooldown
        h.note_gameplay_resumed()
        c.cancels = 0
        h.tick_detect(object(), self._gs(True), GameState.GAME_BATTLE)   # inside cooldown
        assert c.eject_stops == 2      # stopped both times
        assert c.cancels == 0          # but the restart flow was suppressed

    def test_health_fallback_event_triggers_the_flow(self):
        """ADR 064 dual mode: OCR missed the overlay, health evidence fired."""
        h, a, c = _respawn()
        a.health_respawn_event.set()
        assert h.tick_detect(object(), self._gs(False), GameState.GAME_BATTLE) is True
        assert c.cancels == 1
        assert not a.health_respawn_event.is_set()   # consumed

    def test_manual_death_returns_to_auto(self):
        """ADR 059: death ends manual takeover."""
        h, a, c = _respawn(_RespawnAnalyzerStub(state=GameState.GAME_BATTLE_MANUAL))
        h.tick_detect(object(), self._gs(True), GameState.GAME_BATTLE_MANUAL)
        assert "respawn_reset" in a.triggers

    def test_latch_dedupes_while_screen_persists(self):
        h, a, c = _respawn()
        h.tick_detect(object(), self._gs(True), GameState.GAME_BATTLE)
        h.tick_detect(object(), self._gs(True), GameState.GAME_BATTLE)
        assert c.cancels == 1   # RESPAWNING latch blocks the second run

    def test_gameplay_resumed_clears_the_latch(self):
        from wingman.main import RespawnState
        h, a, c = _respawn()
        h.tick_detect(object(), self._gs(True), GameState.GAME_BATTLE)
        assert h.state == RespawnState.RESPAWNING
        h.note_gameplay_resumed()
        assert h.state == RespawnState.IDLE

    def test_collaborators_are_called_explicitly_not_via_shared_state(self):
        """ADR 060 rule 2: cross-concern effects are named calls."""
        armed, suppressed = [], []
        h, a, c = _respawn(enemy=SimpleNamespace(arm=lambda: armed.append(1)),
                           ammo=SimpleNamespace(suppress_after_respawn=suppressed.append))
        h.tick_detect(object(), self._gs(True), GameState.GAME_BATTLE)
        assert armed == [1]
        assert suppressed == [10.0]


class _TrackerStub:
    def __init__(self, enabled=True, obs=None):
        self.enabled = enabled
        self._obs = obs or {"error_norm": 0.5, "visible": True, "mode": "TRACKING"}
        self.resets = 0
        self.updates = 0

    def update(self, _frame):
        self.updates += 1
        return self._obs

    def reset(self):
        self.resets += 1


class _HudStub:
    def __init__(self):
        self.renders = []

    def maybe_render(self, frame, obs, state, health, missiles, flares):
        self.renders.append((obs, state))


class _TrackCtrlStub:
    def __init__(self, mission_running=True):
        self._running = mission_running
        self.orients = []

    def is_mission_running(self):
        return self._running

    def orient_nose_to_target(self, err, **kw):
        self.orients.append(err)
        return "right"


class _TrackAnalyzerStub:
    def get_ammo_missiles(self):
        return 4

    def get_ammo_flares(self):
        return 2


def _tracking(tracker=None, hud=None, ctrl=None):
    from wingman.tick_handlers import TrackingHudHandler
    t = tracker or _TrackerStub()
    h = hud or _HudStub()
    c = ctrl or _TrackCtrlStub()
    return TrackingHudHandler(t, h, _TrackAnalyzerStub(), c, {}), t, h, c


class TestTrackingHud:
    def test_tracks_and_orients_in_battle(self):
        handler, t, _, c = _tracking()
        handler.tick(object(), GameState.GAME_BATTLE, {"health": 100})
        assert t.updates == 1
        assert c.orients == [0.5]

    def test_no_autonomous_roll_without_running_mission(self):
        handler, t, _, c = _tracking(ctrl=_TrackCtrlStub(mission_running=False))
        handler.tick(object(), GameState.GAME_BATTLE, {"health": 100})
        assert t.updates == 0
        assert c.orients == []

    def test_no_tracking_in_manual_mode(self):
        handler, t, _, c = _tracking()
        handler.tick(object(), GameState.GAME_BATTLE_MANUAL, {"health": 100})
        assert t.updates == 0

    def test_disabled_tracker_is_skipped(self):
        handler, t, _, _ = _tracking(tracker=_TrackerStub(enabled=False))
        handler.tick(object(), GameState.GAME_BATTLE, {"health": 100})
        assert t.updates == 0

    def test_hud_renders_in_manual_mode_too(self):
        handler, _, hud, _ = _tracking()
        handler.tick(object(), GameState.GAME_BATTLE_MANUAL, {"health": 100})
        assert len(hud.renders) == 1

    def test_hud_not_rendered_outside_battle(self):
        handler, _, hud, _ = _tracking()
        handler.tick(object(), GameState.GAME_LOBBY, {"health": 100})
        assert hud.renders == []

    def test_hud_receives_the_tracking_observation(self):
        handler, _, hud, _ = _tracking()
        handler.tick(object(), GameState.GAME_BATTLE, {"health": 100})
        obs, state = hud.renders[0]
        assert obs["mode"] == "TRACKING"
        assert state == "GAME_BATTLE"

    def test_leaving_battle_resets_the_tracker(self):
        """Regression: this reset was orphaned into an unreachable branch during
        the step 2.3 extraction and silently stopped firing."""
        handler, t, _, _ = _tracking()
        handler.on_state_change(GameState.GAME_LOBBY, GameState.GAME_BATTLE)
        assert t.resets == 1

    def test_moving_between_battle_states_does_not_reset(self):
        handler, t, _, _ = _tracking()
        handler.on_state_change(GameState.GAME_BATTLE_EJECT, GameState.GAME_BATTLE)
        handler.on_state_change(GameState.GAME_BATTLE_MANUAL, GameState.GAME_BATTLE_EJECT)
        assert t.resets == 0

    def test_entering_battle_does_not_reset(self):
        handler, t, _, _ = _tracking()
        handler.on_state_change(GameState.GAME_BATTLE, GameState.GAME_LOBBY)
        assert t.resets == 0


class TestUnknownAnomalyRecorder:
    """ADR 074: screenshots of GAME_UNKNOWN episodes that outlive the threshold."""

    def _recorder(self, tmp_path, clock, **cfg_overrides):
        from wingman.tick_handlers import UnknownAnomalyRecorder
        cfg = {"screenshot_after_s": 30.0, "recapture_interval_s": 120.0,
               "max_per_episode": 2, "dir": str(tmp_path)}
        cfg.update(cfg_overrides)
        return UnknownAnomalyRecorder(cfg, clock=clock)

    @staticmethod
    def _frame():
        import numpy as np
        return np.zeros((4, 4, 3), dtype=np.uint8)

    @staticmethod
    def _clock(start=1000.0):
        state = {"now": start}
        def clock():
            return state["now"]
        clock.state = state
        return clock

    def test_lobby_blackout_captures_evidence(self, tmp_path):
        """ADR 087: a classified state whose crops all read empty is captured.

        On 2026-08-21 wingman sat in GAME_LOBBY for 8 minutes with every lobby
        crop blank, pressing ESC on a loop. No screenshot was taken because the
        capture was gated on GAME_UNKNOWN, so the screen that caused it could
        not be identified afterwards.
        """
        clock = self._clock()
        r = self._recorder(tmp_path, clock)
        # Blackout beats arrive every 10s from the analyzer.
        r.note_lobby_stall()
        assert r.tick(self._frame(), GameState.GAME_LOBBY) is None
        clock.state["now"] += 10.0
        r.note_lobby_stall()
        assert r.tick(self._frame(), GameState.GAME_LOBBY) is None, \
            "captured before the threshold"
        clock.state["now"] += 25.0          # 35s since the first beat
        r.note_lobby_stall()
        path = r.tick(self._frame(), GameState.GAME_LOBBY)
        assert path is not None, "no evidence captured for a lobby blackout"
        assert "blackout" in str(path), f"episode not named in {path}"

    def test_lobby_blackout_stops_when_scan_recovers(self, tmp_path):
        """A recovered scan stops emitting beats; the episode must lapse."""
        clock = self._clock()
        r = self._recorder(tmp_path, clock)
        r.note_lobby_stall()
        clock.state["now"] += 40.0          # past the threshold, but no new beat
        assert r.tick(self._frame(), GameState.GAME_LOBBY) is None, \
            "captured after the blackout had already cleared"
        assert list(tmp_path.iterdir()) == []

    def test_state_change_ends_blackout_episode(self, tmp_path):
        """Classification producing a new answer means the blackout is over."""
        clock = self._clock()
        r = self._recorder(tmp_path, clock)
        r.note_lobby_stall()
        clock.state["now"] += 35.0
        r.on_state_change(GameState.GAME_BATTLE, GameState.GAME_LOBBY)
        r.note_lobby_stall()                # fresh episode, clock restarts
        assert r.tick(self._frame(), GameState.GAME_BATTLE) is None
        assert list(tmp_path.iterdir()) == []

    def test_normal_startup_never_captures(self, tmp_path):
        clock = self._clock()
        r = self._recorder(tmp_path, clock)
        assert r.tick(self._frame(), GameState.GAME_UNKNOWN) is None  # arms
        clock.state["now"] += 5.0   # startup classification window
        assert r.tick(self._frame(), GameState.GAME_UNKNOWN) is None
        r.on_state_change(GameState.GAME_LOBBY, GameState.GAME_UNKNOWN)
        assert list(tmp_path.iterdir()) == []

    def test_captures_after_threshold_with_stuck_duration_in_name(self, tmp_path):
        clock = self._clock()
        r = self._recorder(tmp_path, clock)
        r.on_state_change(GameState.GAME_UNKNOWN, GameState.GAME_STARTING_STALLED)
        clock.state["now"] += 31.0
        path = r.tick(self._frame(), GameState.GAME_UNKNOWN)
        assert path is not None and "stuck31s" in path
        assert (tmp_path / path.split("/")[-1]).exists()

    def test_recapture_interval_and_episode_cap(self, tmp_path):
        clock = self._clock()
        r = self._recorder(tmp_path, clock)
        r.on_state_change(GameState.GAME_UNKNOWN, GameState.GAME_STARTING_STALLED)
        clock.state["now"] += 31.0
        assert r.tick(self._frame(), GameState.GAME_UNKNOWN) is not None   # 1st
        clock.state["now"] += 5.0
        assert r.tick(self._frame(), GameState.GAME_UNKNOWN) is None       # interval gate
        clock.state["now"] += 120.0
        assert r.tick(self._frame(), GameState.GAME_UNKNOWN) is not None   # 2nd
        clock.state["now"] += 120.0
        assert r.tick(self._frame(), GameState.GAME_UNKNOWN) is None       # cap (max 2)

    def test_episode_reset_on_reentry(self, tmp_path):
        clock = self._clock()
        r = self._recorder(tmp_path, clock)
        r.on_state_change(GameState.GAME_UNKNOWN, GameState.GAME_STARTING_STALLED)
        clock.state["now"] += 31.0
        assert r.tick(self._frame(), GameState.GAME_UNKNOWN) is not None
        r.on_state_change(GameState.GAME_LOBBY, GameState.GAME_UNKNOWN)
        r.on_state_change(GameState.GAME_UNKNOWN, GameState.GAME_LOBBY)
        clock.state["now"] += 5.0
        assert r.tick(self._frame(), GameState.GAME_UNKNOWN) is None   # fresh clock
        clock.state["now"] += 26.0
        assert r.tick(self._frame(), GameState.GAME_UNKNOWN) is not None

    def test_other_states_ignored(self, tmp_path):
        clock = self._clock()
        r = self._recorder(tmp_path, clock)
        clock.state["now"] += 100.0
        assert r.tick(self._frame(), GameState.GAME_BATTLE) is None
        assert list(tmp_path.iterdir()) == []

    def test_dismissal_attempt_defers_capture(self, tmp_path):
        """A popup being handled is not an anomaly — hold the capture."""
        clock = self._clock()
        r = self._recorder(tmp_path, clock, dismiss_grace_s=20.0)
        r.on_state_change(GameState.GAME_UNKNOWN, GameState.GAME_STARTING_STALLED)
        clock.state["now"] += 31.0
        r.note_dismiss_attempt("NEW_FLIGHT_PASS")
        assert r.tick(self._frame(), GameState.GAME_UNKNOWN) is None
        clock.state["now"] += 19.0
        assert r.tick(self._frame(), GameState.GAME_UNKNOWN) is None   # still in grace
        assert list(tmp_path.iterdir()) == []

    def test_capture_proceeds_when_dismissal_does_not_clear(self, tmp_path):
        """Grace runs from the FIRST attempt, so repeated failing clicks still capture."""
        clock = self._clock()
        r = self._recorder(tmp_path, clock, dismiss_grace_s=20.0)
        r.on_state_change(GameState.GAME_UNKNOWN, GameState.GAME_STARTING_STALLED)
        clock.state["now"] += 31.0
        r.note_dismiss_attempt("NEW_FLIGHT_PASS")
        assert r.tick(self._frame(), GameState.GAME_UNKNOWN) is None
        for _ in range(4):            # popup re-detected and re-clicked every 5s
            clock.state["now"] += 6.0
            r.note_dismiss_attempt("NEW_FLIGHT_PASS")
        assert r.tick(self._frame(), GameState.GAME_UNKNOWN) is not None
        assert r._dismiss_attempts == 5

    def test_successful_dismissal_never_captures(self, tmp_path):
        """Popup cleared, state left GAME_UNKNOWN — no evidence needed."""
        clock = self._clock()
        r = self._recorder(tmp_path, clock, dismiss_grace_s=20.0)
        r.on_state_change(GameState.GAME_UNKNOWN, GameState.GAME_STARTING_STALLED)
        clock.state["now"] += 31.0
        r.note_dismiss_attempt("NEW_FLIGHT_PASS")
        assert r.tick(self._frame(), GameState.GAME_UNKNOWN) is None
        r.on_state_change(GameState.GAME_LOBBY, GameState.GAME_UNKNOWN)
        assert list(tmp_path.iterdir()) == []

    def test_dismiss_history_resets_between_episodes(self, tmp_path):
        """A later stall must not inherit the previous episode's grace window."""
        clock = self._clock()
        r = self._recorder(tmp_path, clock, dismiss_grace_s=20.0)
        r.on_state_change(GameState.GAME_UNKNOWN, GameState.GAME_STARTING_STALLED)
        r.note_dismiss_attempt("NEW_FLIGHT_PASS")
        r.on_state_change(GameState.GAME_LOBBY, GameState.GAME_UNKNOWN)
        r.on_state_change(GameState.GAME_UNKNOWN, GameState.GAME_LOBBY)
        assert r._dismiss_attempts == 0 and r._dismiss_popups == []
        clock.state["now"] += 31.0
        assert r.tick(self._frame(), GameState.GAME_UNKNOWN) is not None

    def test_no_dismissal_still_captures_immediately(self, tmp_path):
        """Nothing matched the screen — the original ADR 074 path is unchanged."""
        clock = self._clock()
        r = self._recorder(tmp_path, clock, dismiss_grace_s=20.0)
        r.on_state_change(GameState.GAME_UNKNOWN, GameState.GAME_STARTING_STALLED)
        clock.state["now"] += 31.0
        assert r.tick(self._frame(), GameState.GAME_UNKNOWN) is not None

    def test_popup_absent_ends_grace_and_records_cleared(self, tmp_path):
        """Popup gone but still unclassified: capture at once, blame nothing.

        Regression for the live 2026-08-20 00:33:37 mislabel — the ESC HAD
        worked (5 consecutive 'not found' scans) yet the capture claimed the
        dismissal "did NOT clear it", because failure was inferred from the
        state instead of from the popup still being on screen.
        """
        clock = self._clock()
        r = self._recorder(tmp_path, clock, dismiss_grace_s=20.0)
        r.on_state_change(GameState.GAME_UNKNOWN, GameState.GAME_STARTING_STALLED)
        clock.state["now"] += 31.0
        r.note_dismiss_attempt("NEW_FLIGHT_PASS")
        assert r.tick(self._frame(), GameState.GAME_UNKNOWN) is None   # in grace
        r.note_popup_absent()                                          # ESC worked
        assert r._first_dismiss_ts == 0.0
        assert r._cleared_popups == ["NEW_FLIGHT_PASS"]
        # Grace no longer applies: the stall is real but not a popup failure.
        assert r.tick(self._frame(), GameState.GAME_UNKNOWN) is not None

    def test_popup_absent_before_any_dismissal_is_a_noop(self, tmp_path):
        clock = self._clock()
        r = self._recorder(tmp_path, clock, dismiss_grace_s=20.0)
        r.on_state_change(GameState.GAME_UNKNOWN, GameState.GAME_STARTING_STALLED)
        r.note_popup_absent()
        assert r._cleared_popups == [] and r._dismiss_attempts == 0
        clock.state["now"] += 31.0
        assert r.tick(self._frame(), GameState.GAME_UNKNOWN) is not None

    def test_popup_reappearing_after_clear_re_arms_grace(self, tmp_path):
        """A popup that comes back is a fresh handling attempt, not a cleared one."""
        clock = self._clock()
        r = self._recorder(tmp_path, clock, dismiss_grace_s=20.0)
        r.on_state_change(GameState.GAME_UNKNOWN, GameState.GAME_STARTING_STALLED)
        clock.state["now"] += 31.0
        r.note_dismiss_attempt("NEW_FLIGHT_PASS")
        r.note_popup_absent()
        r.note_dismiss_attempt("NEW_FLIGHT_PASS")      # back again
        assert r.tick(self._frame(), GameState.GAME_UNKNOWN) is None   # grace re-armed
        assert list(tmp_path.iterdir()) == []


class TestHealthDropoutRecorder:
    """ADR 080 d2: frames captured during live-flight health OCR dropouts."""

    class _FakeDropoutAnalyzer:
        def __init__(self):
            self.gap = None
            self.live = True

        def health_confirmed_gap_s(self):
            return self.gap

        def telemetry_hud_live(self):
            return self.live

    def _recorder(self, tmp_path, analyzer, clock, **cfg_overrides):
        from wingman.tick_handlers import HealthDropoutRecorder
        cfg = {"capture_after_s": 5.0, "recapture_interval_s": 60.0,
               "max_per_session": 2, "dir": str(tmp_path)}
        cfg.update(cfg_overrides)
        return HealthDropoutRecorder(cfg, analyzer, clock=clock)

    @staticmethod
    def _frame():
        import numpy as np
        return np.zeros((4, 4, 3), dtype=np.uint8)

    @staticmethod
    def _clock(start=1000.0):
        state = {"now": start}
        def clock():
            return state["now"]
        clock.state = state
        return clock

    def test_short_gap_never_captures(self, tmp_path):
        a = self._FakeDropoutAnalyzer()
        a.gap = 3.0
        r = self._recorder(tmp_path, a, self._clock())
        assert r.tick(self._frame(), GameState.GAME_BATTLE) is None
        assert list(tmp_path.iterdir()) == []

    def test_captures_past_threshold_with_gap_in_name(self, tmp_path):
        a = self._FakeDropoutAnalyzer()
        a.gap = 7.2
        r = self._recorder(tmp_path, a, self._clock())
        path = r.tick(self._frame(), GameState.GAME_BATTLE)
        assert path is not None and "gap7s" in path
        assert (tmp_path / path.split("/")[-1]).exists()

    def test_stale_telemetry_gap_never_captures(self, tmp_path):
        """A gap with stale telemetry is a death/menu gap, not a dropout."""
        a = self._FakeDropoutAnalyzer()
        a.gap = 20.0
        a.live = False
        r = self._recorder(tmp_path, a, self._clock())
        assert r.tick(self._frame(), GameState.GAME_BATTLE) is None
        assert list(tmp_path.iterdir()) == []

    def test_one_per_episode_plus_recapture_and_session_cap(self, tmp_path):
        a = self._FakeDropoutAnalyzer()
        a.gap = 6.0
        clock = self._clock()
        r = self._recorder(tmp_path, a, clock)
        assert r.tick(self._frame(), GameState.GAME_BATTLE) is not None  # 1st
        clock.state["now"] += 5.0
        a.gap = 11.0
        assert r.tick(self._frame(), GameState.GAME_BATTLE) is None     # interval gate
        clock.state["now"] += 60.0
        a.gap = 71.0
        assert r.tick(self._frame(), GameState.GAME_BATTLE) is not None  # recapture
        clock.state["now"] += 60.0
        a.gap = 131.0
        assert r.tick(self._frame(), GameState.GAME_BATTLE) is None     # session cap (2)

    def test_confirmed_read_resets_episode(self, tmp_path):
        a = self._FakeDropoutAnalyzer()
        a.gap = 6.0
        clock = self._clock()
        r = self._recorder(tmp_path, a, clock)
        assert r.tick(self._frame(), GameState.GAME_BATTLE) is not None  # episode 1
        a.gap = 0.5                                                      # confirm landed
        assert r.tick(self._frame(), GameState.GAME_BATTLE) is None
        clock.state["now"] += 1.0
        a.gap = 6.0                                                      # new episode
        assert r.tick(self._frame(), GameState.GAME_BATTLE) is not None  # captures again

    def test_non_battle_state_never_captures(self, tmp_path):
        a = self._FakeDropoutAnalyzer()
        a.gap = 30.0
        r = self._recorder(tmp_path, a, self._clock())
        assert r.tick(self._frame(), GameState.GAME_LOBBY) is None
        assert list(tmp_path.iterdir()) == []

    def test_disabled_config_is_noop(self, tmp_path):
        a = self._FakeDropoutAnalyzer()
        a.gap = 30.0
        r = self._recorder(tmp_path, a, self._clock(), enabled=False)
        assert r.tick(self._frame(), GameState.GAME_BATTLE) is None
        assert list(tmp_path.iterdir()) == []
