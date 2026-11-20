"""ADR 084: gate logic for the stall-recovery crops.

These actions leave squads and dismiss a modal sitting beside an "Exit to
Desktop" button, so the gate — not the detection — is the safety-critical part.
Every test here is about when the gate stays SHUT.

Usage: uv run pytest tests/test_stall_recovery.py -q
"""

import copy
import time
from concurrent.futures import Future
from pathlib import Path

import numpy as np
import pytest
import yaml

import wingman.analyzer as analyzer_module
from wingman.analyzer import (GameStateAnalyzer, GameEvent, GameState,
                              STALL_ACTION_STATES, STALL_RECOVERY_CROPS,
                              STALL_UNREADY_CROP)
from wingman.config_schema import schema_default
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


class TestStallGate:
    """The gate must stay shut during healthy operation."""

    @pytest.mark.parametrize("state", [GameState.GAME_LOBBY, GameState.GAME_WAITING,
                                       GameState.GAME_BATTLE, GameState.GAME_STARTING,
                                       GameState.GAME_END_B])
    def test_classified_states_never_open_the_gate(self, analyzer, state):
        """Even with the dwell clock maxed out, a classified state acts on nothing."""
        analyzer._stall_state_since = time.time() - 9999.0
        assert analyzer._stall_recovery_targets(state) == []

    @pytest.mark.parametrize("state", STALL_ACTION_STATES)
    def test_stalled_state_without_dwell_is_shut(self, analyzer, state):
        """A GAME_UNKNOWN blip mid-transition must not trigger recovery."""
        analyzer._stall_state_since = time.time()   # just entered
        assert analyzer._stall_recovery_targets(state) == []

    @pytest.mark.parametrize("state", STALL_ACTION_STATES)
    def test_stalled_state_past_dwell_opens_the_gate(self, analyzer, state):
        analyzer._stall_state_since = time.time() - (analyzer._stall_action_after_s + 1.0)
        targets = analyzer._stall_recovery_targets(state)
        assert targets == list(STALL_RECOVERY_CROPS)

    def test_lobby_blackout_past_dwell_opens_only_de_escalating_crops(self, analyzer):
        """ADR 087, widened by ADR 093: a sustained lobby blackout may act on
        the screens it can dismiss without escalating.

        The ESC pressed on every LOBBY_STALL beat is what opens "Exit to
        Desktop"; GAME_LOBBY is not in STALL_ACTION_STATES, so its crop was
        never scanned and wingman deadlocked against a modal it created
        (2026-08-21: 8 minutes, 187 blank cycles). ADR 093 added STALL_PROFILE
        on the same gate for the same reason, after a PROFILE overlay stranded
        a session for 110 minutes with nothing eligible to dismiss it.

        The invariant ADR 087 was really protecting is unchanged and is what
        this test now asserts: both of those crops are a single de-escalating
        click, and the INVASIVE ones stay gated on a genuinely unclassifiable
        state. Widening this set again needs the same argument.

        STALL_CHOOSE_REWARDS (Anomaly 006) is the one exception and is not
        de-escalating: it commits a reward. It is here on the operator's
        decision of 2026-10-02 ("always choose the reward in the middle of the
        screen then accept"), after the overlay stalled a lobby that still
        classified as GAME_LOBBY.

        STALL_INVITE_PLAYERS (2026-10-11) meets the original argument: the
        screen is identified by its title and the action is one ESC, the same
        ESC that already closed it once the ADR 093 ceiling passed.

        STALL_PARTS_CRATE (Anomaly 005, ADR 196 D7) meets it too: the screen
        is identified by its reward-card text, and the action is one click on
        the crate's '?' that opens a reward already owned and commits nothing.
        On 2026-11-19 05:14 a '+300 SILVER' crate stranded a lobby for 15 min.

        STALL_PARTS_SELECT (Anomaly 004) is not on this gate: it waits for
        the liveness guard's stall (TestPartsToSecondJet).
        """
        analyzer._stall_state_since = 0.0
        analyzer._lobby_blackout_since = time.time() - (analyzer._stall_action_after_s + 1.0)
        targets = analyzer._stall_recovery_targets(GameState.GAME_LOBBY)
        assert set(targets) == {"STALL_PROFILE", "STALL_EXIT_TO_DESKTOP",
                                "STALL_CHOOSE_REWARDS", "STALL_INVITE_PLAYERS",
                                "STALL_PARTS_CRATE"}, \
            f"lobby blackout must open exactly these crops, got {targets}"
        for invasive in ("STALL_RETRY", "STALL_AIRCRAFT"):
            assert invasive not in targets, \
                f"{invasive} must stay gated on an unclassifiable state (ADR 084)"

    def test_invite_players_is_closed_with_escape_not_a_click(self):
        """Every button on that screen sends or cancels an invite."""
        src = Path("wingman/main.py").read_text(encoding="utf-8")
        branch = src[src.index('if crop == "STALL_INVITE_PLAYERS":'):]
        branch = branch[:branch.index("return")]
        assert "ctrl.press_escape(" in branch
        assert "click_crop" not in branch

    def test_lobby_blackout_within_dwell_is_shut(self, analyzer):
        """A brief blank patch between lobby frames is not a stall."""
        analyzer._stall_state_since = 0.0
        analyzer._lobby_blackout_since = time.time()
        assert analyzer._stall_recovery_targets(GameState.GAME_LOBBY) == []

    def test_unset_lobby_blackout_clock_is_shut(self, analyzer):
        analyzer._stall_state_since = 0.0
        analyzer._lobby_blackout_since = 0.0
        assert analyzer._stall_recovery_targets(GameState.GAME_LOBBY) == []

    def test_lobby_blackout_does_not_duplicate_in_a_real_stall(self, analyzer):
        """Both gates open at once: the crop must appear exactly once."""
        analyzer._stall_state_since = time.time() - (analyzer._stall_action_after_s + 1.0)
        analyzer._lobby_blackout_since = time.time() - (analyzer._stall_action_after_s + 1.0)
        targets = analyzer._stall_recovery_targets(GameState.GAME_UNKNOWN)
        assert targets.count("STALL_EXIT_TO_DESKTOP") == 1, targets

    def test_exit_dialog_flag_suppresses_and_lapses(self, analyzer):
        """ADR 087: every ESC source stands down while the modal is up.

        ESC is what OPENS Exit-to-Desktop, so a press while it is on screen
        re-opens what the Cancel click just closed — the 2026-08-21 deadlock,
        where three ESC sources fought one 20s-cooldown recovery for 25
        minutes. The flag must also lapse on its own so a cleared dialog does
        not suppress ESC forever.
        """
        assert analyzer.exit_dialog_visible() is False, "unset flag must not suppress"
        analyzer._exit_dialog_seen_ts = time.time()
        assert analyzer.exit_dialog_visible() is True
        analyzer._exit_dialog_seen_ts = time.time() - 60.0
        assert analyzer.exit_dialog_visible() is False, "stale flag must lapse"

    def test_blackout_flag_gates_esc_independently_of_the_dialog(self, analyzer):
        """ADR 087 addendum 3: ESC is gated on the blackout, not the dialog.

        A single 'not found' scan clears the dialog flag, and the ESC that then
        fired re-created the dialog 1.4s later — a 23s cancel-then-reopen cycle
        (2026-08-21 10:48). The blackout outlasts those gaps, so it is the
        correct gate.
        """
        analyzer._lobby_blackout_since = 0.0
        assert analyzer.lobby_blackout_active() is False
        analyzer._lobby_blackout_since = time.time()
        analyzer._exit_dialog_seen_ts = 0.0      # dialog momentarily "not found"
        assert analyzer.exit_dialog_visible() is False
        assert analyzer.lobby_blackout_active() is True, \
            "ESC must stay suppressed across a momentary dialog miss"

    def test_click_to_suppression_yields_to_a_lobby_blackout(self, analyzer):
        """ADR 087 addendum 4: a forced GAME_LOBBY must not blind click-to OCR.

        `GAME_END_B timeout — forcing recovery to GAME_LOBBY` can assert a state
        the game is not in. The click-to scan self-suppresses in GAME_LOBBY, so
        the forced state disabled the one detector that clears the screen
        holding it there: on 2026-08-21 the post-match PERFORMANCE panel with
        "Click to Continue..." went unread for 17 minutes.

        The suppression must hold in a healthy lobby (2026-07-30 double
        click-through) and yield during a blackout.
        """
        def suppressed(state):
            # Mirrors the guard in _run_click_to_in_background.
            return not (state == GameState.GAME_LOBBY
                        and analyzer.lobby_blackout_active())

        analyzer._lobby_blackout_since = 0.0
        assert suppressed(GameState.GAME_LOBBY), \
            "healthy lobby must keep the 2026-07-30 suppression"
        analyzer._lobby_blackout_since = time.time()
        assert not suppressed(GameState.GAME_LOBBY), \
            "blackout must re-enable click-to OCR"
        assert suppressed(GameState.GAME_END_B), \
            "GAME_END_B suppression is unconditional"

    def test_unset_dwell_clock_is_shut(self, analyzer):
        """_stall_state_since == 0 means 'not stalled', not 'stalled since epoch'."""
        analyzer._stall_state_since = 0.0
        assert analyzer._stall_recovery_targets(GameState.GAME_UNKNOWN) == []

    def test_scan_order_is_most_specific_first(self, analyzer):
        """The batch breaks on first hit, so order is behaviour, not style."""
        analyzer._stall_state_since = time.time() - 9999.0
        targets = analyzer._stall_recovery_targets(GameState.GAME_UNKNOWN)
        assert targets.index("STALL_RETRY") < targets.index("STALL_AIRCRAFT")


class TestUnreadyGate:
    """STALL_MULTI_PLAYER is timed from the UNREADY read, not from the state."""

    def test_unready_below_dwell_is_shut(self, analyzer):
        analyzer._unready_since = time.time()
        analyzer._stall_state_since = 0.0
        assert analyzer._stall_recovery_targets(GameState.GAME_LOBBY) == []

    def test_unready_in_a_classified_lobby_leaves_at_the_squad_limit(self, analyzer):
        """The 2026-08-19 22:07 stall: UNREADY blocks PLAY in a lobby that
        classifies. ADR 196 D4: left by the squad_no_battle_s gate (the UNREADY
        read starts that clock), not by the 160 s dwell, which left a squad
        whose leader was still queueing on 2026-11-19."""
        analyzer._unready_since = time.time() - (analyzer._stall_unready_dwell_s + 1.0)
        analyzer._stall_state_since = 0.0
        assert analyzer._stall_recovery_targets(GameState.GAME_LOBBY) == []
        analyzer._squad_lobby_since = time.time() - (analyzer._stall_squad_no_battle_s + 1.0)
        assert analyzer._stall_recovery_targets(GameState.GAME_LOBBY) == [STALL_UNREADY_CROP]

    def test_an_unready_read_starts_the_squad_clock(self, analyzer):
        analyzer._note_unready("WAITINGCLICKTOUNREADY")
        assert analyzer._squad_lobby_since > 0.0

    def test_unready_past_dwell_also_fires_while_unclassified(self, analyzer):
        """UNREADY makes _classify_unknown_state fail, so it strands GAME_UNKNOWN too."""
        analyzer._unready_since = time.time() - (analyzer._stall_unready_dwell_s + 1.0)
        analyzer._stall_state_since = time.time() - (analyzer._stall_action_after_s + 1.0)
        targets = analyzer._stall_recovery_targets(GameState.GAME_UNKNOWN)
        assert STALL_UNREADY_CROP in targets
        assert targets[-1] == STALL_UNREADY_CROP   # after the state-gated crops

    def test_cleared_unready_shuts_the_gate(self, analyzer):
        analyzer._unready_since = 0.0
        assert analyzer._stall_recovery_targets(GameState.GAME_LOBBY) == []


class _InlineExecutor:
    """Runs each OCR task at submit time, so the scan needs no thread pool."""

    def submit(self, fn, *args):
        future = Future()
        future.set_result(fn(*args))
        return future


def _scan_play_button(analyzer, monkeypatch, **reads):
    """scan_region_for_play_button with each lobby crop reading the given text.

    Per crop, not per frame: on 2026-10-11 the UNREADY crop read nothing while
    the READY crop read the ready state. The token match is production's.
    """
    def _read(_img, tokens):
        text = reads.get(tokens[0])
        hit = text is not None and any(t in text for t in tokens)
        return (hit, 0.0, text if hit else None)

    monkeypatch.setattr(analyzer_module, "_process_text_region", _read)
    monkeypatch.setattr(type(analyzer), "ocr_executor",
                        property(lambda self: _InlineExecutor()), raising=False)
    return analyzer.scan_region_for_play_button(np.zeros((1200, 1920, 3), np.uint8))


class TestUnreadyReadThroughTheReadyCrop:
    """2026-10-11: the squad button read 'WAITING CLICK TO UNREADY' in the READY
    crop and nothing in the UNREADY crop. Returned as READY, it was re-clicked
    every 10 s (un-readying the player) and the dwell never started."""

    def test_it_suppresses_the_click_and_starts_the_dwell(self, analyzer, monkeypatch):
        assert _scan_play_button(analyzer, monkeypatch,
                                 READY="WAITINGCLICKTOUNREADY") is None
        assert analyzer._unready_since > 0.0

    def test_a_repeat_read_does_not_restart_the_dwell(self, analyzer, monkeypatch):
        started = time.time() - 30.0
        analyzer._unready_since = started
        _scan_play_button(analyzer, monkeypatch, READY="WAITINGCLICKTOUNREADY")
        assert analyzer._unready_since == started

    def test_the_unready_crop_still_works(self, analyzer, monkeypatch):
        assert _scan_play_button(analyzer, monkeypatch, UNREADY="UNREADY") is None
        assert analyzer._unready_since > 0.0

    def test_a_plain_ready_is_still_clickable_and_clears_the_dwell(self, analyzer, monkeypatch):
        analyzer._unready_since = time.time() - 30.0
        assert _scan_play_button(analyzer, monkeypatch, READY="READY") == "READY"
        assert analyzer._unready_since == 0.0

    def test_a_hidden_button_clears_the_dwell(self, analyzer, monkeypatch):
        """Unchanged: a miss on every crop is not an UNREADY read."""
        analyzer._unready_since = time.time() - 30.0
        assert _scan_play_button(analyzer, monkeypatch) is None
        assert analyzer._unready_since == 0.0


class TestSquadNoBattleGate:
    """STALL_MULTI_PLAYER also opens for a squad lobby that produces no battle,
    whatever the button reads. 2026-10-11: READY for 7h23m after an accepted
    invite, most of it under a dialog that rejected every READY click."""

    def test_squad_lobby_below_the_limit_is_shut(self, analyzer):
        analyzer._squad_lobby_since = time.time()
        assert analyzer._stall_recovery_targets(GameState.GAME_LOBBY) == []

    @pytest.mark.parametrize("state", [GameState.GAME_LOBBY, GameState.GAME_WAITING,
                                       GameState.GAME_STARTING])
    def test_squad_lobby_past_the_limit_opens_the_gate(self, analyzer, state):
        """The night's loop never dwelt in a stall state, so the gate cannot
        depend on one."""
        analyzer._squad_lobby_since = time.time() - (analyzer._stall_squad_no_battle_s + 1.0)
        assert analyzer._stall_recovery_targets(state) == [STALL_UNREADY_CROP]

    def test_both_gates_open_list_the_crop_once(self, analyzer):
        analyzer._unready_since = time.time() - (analyzer._stall_unready_dwell_s + 1.0)
        analyzer._squad_lobby_since = time.time() - (analyzer._stall_squad_no_battle_s + 1.0)
        assert analyzer._stall_recovery_targets(GameState.GAME_LOBBY) == [STALL_UNREADY_CROP]

    def test_solo_lobby_never_starts_the_clock(self, analyzer, monkeypatch):
        assert _scan_play_button(analyzer, monkeypatch, PLAY="PLAY") == "PLAY"
        assert analyzer._squad_lobby_since == 0.0

    @pytest.mark.parametrize("text", ["READY", "WAITINGCLICKTOUNREADY"])
    def test_a_squad_button_starts_the_clock(self, analyzer, monkeypatch, text):
        _scan_play_button(analyzer, monkeypatch, READY=text)
        assert analyzer._squad_lobby_since > 0.0

    def test_later_squad_reads_do_not_restart_the_clock(self, analyzer, monkeypatch):
        started = time.time() - 600.0
        analyzer._squad_lobby_since = started
        _scan_play_button(analyzer, monkeypatch, READY="READY")
        _scan_play_button(analyzer, monkeypatch, READY="WAITINGCLICKTOUNREADY")
        assert analyzer._squad_lobby_since == started

    def test_a_hidden_button_keeps_the_clock(self, analyzer, monkeypatch):
        """The dialog hid READY for 150 s of every 180 s cycle."""
        started = time.time() - 600.0
        analyzer._squad_lobby_since = started
        _scan_play_button(analyzer, monkeypatch)
        assert analyzer._squad_lobby_since == started

    def test_a_play_read_clears_the_clock(self, analyzer, monkeypatch):
        analyzer._squad_lobby_since = time.time() - 600.0
        _scan_play_button(analyzer, monkeypatch, PLAY="PLAY")
        assert analyzer._squad_lobby_since == 0.0

    def test_a_battle_clears_the_clock(self, analyzer):
        analyzer._squad_lobby_since = time.time() - 600.0
        analyzer.on_enter_GAME_BATTLE()
        assert analyzer._squad_lobby_since == 0.0

    def test_a_battle_clears_the_unready_dwell(self, analyzer):
        """2026-10-23 15:45: the dwell from the 15:38 READY outlived the battle
        it started and left the squad 12 s into the next lobby."""
        analyzer._unready_since = time.time() - 420.0
        analyzer.on_enter_GAME_BATTLE()
        assert analyzer._unready_since == 0.0
        assert analyzer._stall_recovery_targets(GameState.GAME_WAITING) == []

    def test_leave_is_pending_only_past_the_limit(self, analyzer):
        assert analyzer.squad_leave_pending() is False
        analyzer._squad_lobby_since = time.time()
        assert analyzer.squad_leave_pending() is False
        analyzer._squad_lobby_since = time.time() - (analyzer._stall_squad_no_battle_s + 1.0)
        assert analyzer.squad_leave_pending() is True

    def test_leaving_the_squad_ends_the_pending_leave(self, analyzer, monkeypatch):
        """The post-leave scan reads PLAY, which is what lets PLAY be clicked."""
        analyzer._squad_lobby_since = time.time() - (analyzer._stall_squad_no_battle_s + 1.0)
        assert _scan_play_button(analyzer, monkeypatch, PLAY="PLAY") == "PLAY"
        assert analyzer.squad_leave_pending() is False

    def test_a_pending_leave_holds_the_lobby_ready_click(self):
        """Same shape as the finish-round guard: the hold sits at the click
        site and ends at the `else:` that clicks, so neither the click nor the
        FSM trigger is reachable from it."""
        src = Path("wingman/analyzer.py").read_text(encoding="utf-8")
        guard = src.index('elif crop == "READY" and self.squad_leave_pending():')
        branch = src[guard:src.index("\n                        else:", guard)]
        assert "handled = True" in branch
        assert "emit(GameEvent.LOBBY_PLAY_CLICK" not in branch
        assert 'self._trigger("play_clicked")' not in branch

    def test_a_failed_leave_does_not_click_ready(self):
        """main's post-leave click-through: READY after the red X means the
        squad was not left, and clicking it would re-raise the dialog."""
        src = Path("wingman/main.py").read_text(encoding="utf-8")
        hold = src.index('if ready == "READY" and analyzer.squad_leave_pending():')
        click = src.index("clicking '%s' after squad exit", hold)
        assert "return" in src[hold:click]

    def test_the_limit_is_the_schema_default_and_the_shipped_value(self):
        assert (schema_default("stall_recovery.squad_no_battle_s")
                == load_config()["stall_recovery"]["squad_no_battle_s"] == 900.0)


class TestCropsConfigured:
    """A missing crop must degrade to 'skip it', never to a KeyError mid-stall."""

    def test_absent_crop_is_dropped_from_targets(self, analyzer):
        analyzer._stall_state_since = time.time() - 9999.0
        analyzer.crops.pop("STALL_RETRY", None)
        assert "STALL_RETRY" not in analyzer._stall_recovery_targets(GameState.GAME_UNKNOWN)

    def test_all_four_crops_have_text_matchers(self, analyzer):
        """Coords without `text` silently never match — calibration is not enough."""
        for crop in (*STALL_RECOVERY_CROPS, STALL_UNREADY_CROP):
            assert crop in analyzer.crops, f"{crop} missing from config"
            assert analyzer.crops[crop].text, f"{crop} has no text matchers"


class TestEventWiring:
    def test_stall_recovery_action_event_fans_out(self, analyzer):
        seen = []
        analyzer.subscribe(GameEvent.STALL_RECOVERY_ACTION, seen.append, name="t")
        analyzer.emit(GameEvent.STALL_RECOVERY_ACTION, "STALL_RETRY")
        assert seen == ["STALL_RETRY"]

    def test_stall_states_exclude_lobby_and_waiting(self):
        """Regression guard: widening this tuple re-enables actions during healthy play."""
        assert GameState.GAME_LOBBY not in STALL_ACTION_STATES
        assert GameState.GAME_WAITING not in STALL_ACTION_STATES
        assert set(STALL_ACTION_STATES) == {GameState.GAME_UNKNOWN,
                                            GameState.GAME_STARTING_STALLED}


class TestStuckWarningPersists:
    """ADR 093: the screenshot cap must not silence the warning.

    On 2026-08-24 the last complaint was at 511s — the fifth and final capture —
    while the livelock ran another 100 minutes in total silence. An unattended
    session must keep saying it is stuck.
    """

    def _recorder(self, clock):
        from wingman.tick_handlers import UnknownAnomalyRecorder as R
        return R({"screenshot_after_s": 30.0, "recapture_interval_s": 120.0,
                  "max_per_episode": 5, "stuck_warn_interval_s": 300.0,
                  "stuck_warn_max_interval_s": 1800.0}, clock=clock)

    def test_warns_again_after_the_capture_cap(self, caplog):
        now = [1000.0]
        rec = self._recorder(lambda: now[0])
        rec._captured = 5                      # cap already reached
        with caplog.at_level("WARNING"):
            rec._warn_still_stuck(now[0], "GAME_LOBBY blackout", 600.0)
        assert any("STILL stuck" in r.getMessage() for r in caplog.records)

    def test_warning_is_throttled_then_repeats(self):
        """After the first warning the interval doubles to 600s, so 500s of
        silence is correct and 700s is not."""
        now = [1000.0]
        rec = self._recorder(lambda: now[0])
        rec._warn_still_stuck(now[0], "ep", 600.0)
        first = rec._stuck_warns
        now[0] += 500
        rec._warn_still_stuck(now[0], "ep", 1100.0)
        assert rec._stuck_warns == first, "must throttle inside the doubled interval"
        now[0] += 200          # 700s since the last warning, past the 600s interval
        rec._warn_still_stuck(now[0], "ep", 1300.0)
        assert rec._stuck_warns == first + 1, "must warn again past the interval"

    def test_interval_doubles_but_is_capped(self):
        now = [1000.0]
        rec = self._recorder(lambda: now[0])
        for _ in range(12):
            now[0] += 5000                     # always past any interval
            rec._warn_still_stuck(now[0], "ep", now[0])
        interval = min(rec._stuck_warn_interval_s * (2 ** rec._stuck_warns),
                       rec._stuck_warn_max_s)
        assert interval <= rec._stuck_warn_max_s, "interval must stay capped"
        assert rec._stuck_warns >= 10, "a long stall must keep complaining"


class TestChooseRewards:
    """Anomaly 006: the middle reward, then SELECT ONE (operator, 2026-10-02)."""

    class _Ctrl:
        def __init__(self):
            self.clicks = []

        def click_crop(self, crop, *, block, count, region_name):
            self.clicks.append((region_name, crop))

    class _Analyzer:
        def __init__(self, crops, state=GameState.GAME_LOBBY):
            self.crops = crops
            self.game_state = state

    @staticmethod
    def _crops():
        crops = load_config()["crops"]
        return {name: crops[name] for name in
                ("STALL_CHOOSE_REWARDS", "STALL_CHOOSE_REWARDS_PICK",
                 "STALL_CHOOSE_REWARDS_ACCEPT")}

    def test_the_pick_crop_is_the_card_under_the_screen_centre(self):
        (x1, y1), (x2, y2) = self._crops()["STALL_CHOOSE_REWARDS_PICK"]["coords"]
        assert x1 < 0.5 < x2, "the middle card spans the middle of the screen"
        assert abs((x1 + x2) / 2 - 0.5) < 0.02
        assert 0.3 < y1 < y2 < 0.7

    def test_the_accept_crop_is_clear_of_the_cards(self):
        (ax1, ay1), _ = self._crops()["STALL_CHOOSE_REWARDS_ACCEPT"]["coords"]
        _, (_px2, py2) = self._crops()["STALL_CHOOSE_REWARDS_PICK"]["coords"]
        assert ay1 > py2, "SELECT ONE sits below the cards"
        assert ax1 > 0.7

    def test_clicks_the_middle_card_then_accept(self):
        import logging
        from wingman.main import _choose_middle_reward
        ctrl, analyzer = self._Ctrl(), self._Analyzer(self._crops())

        thread = _choose_middle_reward(ctrl, analyzer, logging.getLogger("test"), delay_s=0.05)

        assert [name for name, _ in ctrl.clicks] == ["STALL_CHOOSE_REWARDS_PICK"], (
            "the card is chosen first, the button waits")
        thread.join(timeout=2.0)
        assert [name for name, _ in ctrl.clicks] == [
            "STALL_CHOOSE_REWARDS_PICK", "STALL_CHOOSE_REWARDS_ACCEPT"]

    def test_accept_is_dropped_when_the_game_has_moved_on(self):
        import logging
        from wingman.main import _choose_middle_reward
        ctrl, analyzer = self._Ctrl(), self._Analyzer(self._crops())

        thread = _choose_middle_reward(ctrl, analyzer, logging.getLogger("test"), delay_s=0.05)
        analyzer.game_state = GameState.GAME_BATTLE
        thread.join(timeout=2.0)

        assert [name for name, _ in ctrl.clicks] == ["STALL_CHOOSE_REWARDS_PICK"]

    def test_nothing_is_clicked_without_both_crops(self):
        import logging
        from wingman.main import _choose_middle_reward
        crops = self._crops()
        del crops["STALL_CHOOSE_REWARDS_ACCEPT"]
        ctrl = self._Ctrl()

        assert _choose_middle_reward(ctrl, self._Analyzer(crops), logging.getLogger("test")) is None
        assert ctrl.clicks == []


class TestPartsToSecondJet:
    """Anomaly 004: the second jet, then CONFIRM (operator, 2026-11-10)."""

    _NAMES = ("STALL_PARTS_SELECT", "STALL_PARTS_PICK", "STALL_PARTS_CONFIRM")

    @classmethod
    def _crops(cls):
        crops = load_config()["crops"]
        return {name: crops[name] for name in cls._NAMES}

    def test_the_pick_crop_is_inside_the_second_card(self):
        """Second card on every archived 1920x1200 frame: x 541-981, y 485-787 px."""
        (x1, y1), (x2, y2) = self._crops()["STALL_PARTS_PICK"]["coords"]
        assert 541 / 1920 < x1 < x2 < 981 / 1920
        assert 485 / 1200 < y1 < y2 < 787 / 1200

    def test_confirm_sits_below_the_cards(self):
        (_, cy1), _ = self._crops()["STALL_PARTS_CONFIRM"]["coords"]
        assert cy1 > 787 / 1200

    def test_the_title_is_what_detects_the_screen(self):
        crops = self._crops()
        assert crops["STALL_PARTS_SELECT"].get("text"), "the title crop needs matchers"
        assert not crops["STALL_PARTS_PICK"].get("text"), "a card is clicked, not read"

    @pytest.mark.parametrize("state", [GameState.GAME_LOBBY, GameState.GAME_UNKNOWN,
                                       GameState.GAME_STARTING_STALLED])
    def test_waits_for_the_liveness_stall_however_long_the_screen_is_up(
            self, analyzer, state):
        """Operator, 2026-11-13: an operator at the desk picks the jet first."""
        analyzer._stall_state_since = time.time() - 9999.0
        analyzer._lobby_blackout_since = time.time() - 9999.0
        analyzer.note_liveness_stall(False)
        assert "STALL_PARTS_SELECT" not in analyzer._stall_recovery_targets(state)

    @pytest.mark.parametrize("state", [GameState.GAME_LOBBY, GameState.GAME_UNKNOWN,
                                       GameState.GAME_STARTING_STALLED])
    def test_the_liveness_stall_opens_it_first(self, analyzer, state):
        analyzer._stall_state_since = 0.0
        analyzer._lobby_blackout_since = 0.0
        analyzer.note_liveness_stall(True)
        targets = analyzer._stall_recovery_targets(state)
        assert targets[0] == "STALL_PARTS_SELECT"

    @pytest.mark.parametrize("state", [GameState.GAME_BATTLE, GameState.GAME_WAITING,
                                       GameState.GAME_STARTING, GameState.GAME_END_B])
    def test_the_liveness_stall_never_opens_it_outside_the_lobby_states(
            self, analyzer, state):
        analyzer.note_liveness_stall(True)
        assert "STALL_PARTS_SELECT" not in analyzer._stall_recovery_targets(state)

    def test_progress_closes_it_again(self, analyzer):
        analyzer.note_liveness_stall(True)
        analyzer.note_liveness_stall(False)
        assert "STALL_PARTS_SELECT" not in analyzer._stall_recovery_targets(
            GameState.GAME_LOBBY)

    def test_the_main_loop_feeds_the_liveness_verdict_to_the_analyzer(self):
        src = Path("wingman/main.py").read_text(encoding="utf-8")
        assert "analyzer.note_liveness_stall(liveness.check())" in src

    def test_it_is_not_on_the_15_second_dwell_list(self):
        assert "STALL_PARTS_SELECT" not in STALL_RECOVERY_CROPS

    def test_clicks_the_second_card_then_confirm(self):
        import logging
        from wingman.main import _give_parts_to_second_jet
        ctrl = TestChooseRewards._Ctrl()
        analyzer = TestChooseRewards._Analyzer(self._crops())

        thread = _give_parts_to_second_jet(ctrl, analyzer, logging.getLogger("test"),
                                           delay_s=0.05)

        assert [name for name, _ in ctrl.clicks] == ["STALL_PARTS_PICK"], (
            "CONFIRM is inert until a card is chosen")
        thread.join(timeout=2.0)
        assert [name for name, _ in ctrl.clicks] == ["STALL_PARTS_PICK", "STALL_PARTS_CONFIRM"]

    def test_confirm_is_dropped_when_the_game_has_moved_on(self):
        import logging
        from wingman.main import _give_parts_to_second_jet
        ctrl = TestChooseRewards._Ctrl()
        analyzer = TestChooseRewards._Analyzer(self._crops())

        thread = _give_parts_to_second_jet(ctrl, analyzer, logging.getLogger("test"),
                                           delay_s=0.05)
        analyzer.game_state = GameState.GAME_BATTLE
        thread.join(timeout=2.0)

        assert [name for name, _ in ctrl.clicks] == ["STALL_PARTS_PICK"]

    def test_nothing_is_clicked_without_both_crops(self):
        import logging
        from wingman.main import _give_parts_to_second_jet
        crops = self._crops()
        del crops["STALL_PARTS_PICK"]
        ctrl = TestChooseRewards._Ctrl()

        assert _give_parts_to_second_jet(
            ctrl, TestChooseRewards._Analyzer(crops), logging.getLogger("test")) is None
        assert ctrl.clicks == []

    def test_the_handler_is_wired_to_the_title_crop(self):
        src = Path("wingman/main.py").read_text(encoding="utf-8")
        branch = src[src.index('if crop == "STALL_PARTS_SELECT":'):]
        assert "_give_parts_to_second_jet(" in branch[:branch.index("return")]


class TestInviteRestartsTheSquadClock:
    """2026-11-19 10:14: an accepted invite inherited the 09:12 squad's clock
    and refused READY two minutes later ("no battle for 64 min")."""

    def test_accepting_an_invite_clears_the_clock(self, analyzer):
        analyzer._squad_lobby_since = time.time() - 3800.0
        analyzer._unready_since = time.time() - 3800.0
        assert analyzer.squad_leave_pending() is True
        analyzer.note_invite_accepted()
        assert analyzer.squad_leave_pending() is False
        assert analyzer._unready_since == 0.0

    def test_main_restarts_the_clock_on_accept(self):
        src = Path("wingman/main.py").read_text(encoding="utf-8")
        branch = src[src.index('elif popup == "INVITED":'):]
        branch = branch[:branch.index("def _click_ready_after_invite")]
        reject = branch.index("if not accept_invite:")
        reset = branch.index("analyzer.note_invite_accepted()")
        # After the REJECT return, before the replay-mode return.
        assert reject < reset < branch.index("if replay_mode:")


FIXTURES = Path(__file__).parent / "fixtures"


def _party_frame(strip_name):
    """A 1920x1200 frame holding the saved PARTY-panel column at x 1690."""
    import cv2
    strip = cv2.imread(str(FIXTURES / strip_name))
    frame = np.zeros((1200, 1920, 3), dtype=np.uint8)
    frame[:, 1690:1690 + strip.shape[1]] = strip
    return frame


class TestRedXLocator:
    """The red X moves down the PARTY panel; the crop must follow it."""

    @pytest.mark.parametrize("strip, x_centre_px", [
        ("party_red_x_strip_20261102.png", 300.5),          # the calibration frame
        ("party_red_x_strip_banner_20261119.png", 374.5),   # event banner above the panel
        ("party_red_x_strip_three_20261119.png", 426.5),    # a third member
    ])
    def test_crop_is_centred_on_the_x(self, strip, x_centre_px):
        crop = analyzer_module.load_crops(load_config()["crops"])[STALL_UNREADY_CROP]
        found = analyzer_module.locate_red_x(
            _party_frame(strip), crop, schema_default("stall_recovery.red_x_search_y"))
        assert found is not None
        assert abs((found.y1 + found.y2) / 2 * 1200 - x_centre_px) <= 2.0
        # Same size and column as the calibrated crop: OCR of the X is size-sensitive.
        assert found.y2 - found.y1 == pytest.approx(crop.y2 - crop.y1)
        assert (found.x1, found.x2, found.text) == (crop.x1, crop.x2, crop.text)

    def test_no_red_means_no_crop(self):
        crop = analyzer_module.load_crops(load_config()["crops"])[STALL_UNREADY_CROP]
        frame = np.full((1200, 1920, 3), 60, dtype=np.uint8)
        assert analyzer_module.locate_red_x(frame, crop, (0.12, 0.75)) is None

    def test_click_uses_the_located_crop(self, analyzer):
        calibrated = analyzer.crops[STALL_UNREADY_CROP]
        assert analyzer.red_x_crop() == calibrated
        moved = calibrated._replace(y1=calibrated.y1 + 0.06, y2=calibrated.y2 + 0.06)
        analyzer._red_x_crop = moved
        assert analyzer.red_x_crop() == moved

    def test_main_clicks_the_located_crop(self):
        src = Path("wingman/main.py").read_text(encoding="utf-8")
        assert 'crops["STALL_MULTI_PLAYER"]' not in src
        assert "click_crop(analyzer.red_x_crop()" in src


class TestOneReadyClickAfterAnInvite:
    """READY toggles: two clicks a second apart un-ready the player. 7 of 13
    accepts (2026-11-11 to 11-19) had main's and the quick-scan's READY clicks
    1-3 s apart, in either order."""

    def test_first_claim_wins_and_stamps_the_quick_scan_cooldown(self, analyzer):
        assert analyzer.claim_lobby_click() is True
        assert time.time() - analyzer._last_lobby_play_click_ts < 1.0
        assert analyzer.claim_lobby_click() is False

    def test_a_quick_scan_click_blocks_the_invite_click(self, analyzer):
        """The 11-11 order: the quick-scan clicked first."""
        analyzer._last_lobby_play_click_ts = time.time() - 2.0
        assert analyzer.claim_lobby_click() is False

    def test_a_missed_click_can_be_retried(self, analyzer):
        analyzer._last_lobby_play_click_ts = (
            time.time() - analyzer_module.LOBBY_PLAY_RETRY_S - 0.1)
        assert analyzer.claim_lobby_click() is True

    def test_main_claims_before_the_invite_ready_click(self):
        src = Path("wingman/main.py").read_text(encoding="utf-8")
        body = src[src.index("def _click_ready_after_invite"):]
        body = body[:body.index("threading.Thread(target=_click_ready_after_invite")]
        claim = body.index("analyzer.claim_lobby_click()")
        click = body.index("ctrl.click_crop(")
        assert claim < click
        assert body.count("ctrl.click_crop(") == 1


class TestUnreadyDwellOnlyWhenUnclassified:
    """2026-11-19 18:36: the 160 s UNREADY dwell left a 3-min-old squad from
    GAME_WAITING while the leader's queue cycled every 25 s."""

    @pytest.mark.parametrize("state", [GameState.GAME_LOBBY, GameState.GAME_WAITING,
                                       GameState.GAME_STARTING])
    def test_a_classified_squad_lobby_waits(self, analyzer, state):
        analyzer._unready_since = time.time() - (analyzer._stall_unready_dwell_s + 30.0)
        analyzer._squad_lobby_since = time.time() - 200.0
        assert STALL_UNREADY_CROP not in analyzer._stall_recovery_targets(state)

    @pytest.mark.parametrize("state", STALL_ACTION_STATES)
    def test_an_unclassified_stall_still_leaves(self, analyzer, state):
        """ADR 084's case: UNREADY keeps the FSM in GAME_UNKNOWN."""
        analyzer._stall_state_since = time.time() - (analyzer._stall_action_after_s + 1.0)
        analyzer._unready_since = time.time() - (analyzer._stall_unready_dwell_s + 1.0)
        assert STALL_UNREADY_CROP in analyzer._stall_recovery_targets(state)

    def test_the_squad_limit_still_leaves_from_game_waiting(self, analyzer):
        analyzer._unready_since = time.time() - 1000.0
        analyzer._squad_lobby_since = time.time() - (analyzer._stall_squad_no_battle_s + 1.0)
        assert STALL_UNREADY_CROP in analyzer._stall_recovery_targets(GameState.GAME_WAITING)


class TestLobbyEntryKeepsARecentClick:
    def test_a_click_under_10s_old_survives_lobby_entry(self, analyzer):
        """2026-11-19 18:33: GAME_UNKNOWN → GAME_LOBBY 0.8 s after the
        invite's click wiped its stamp."""
        assert analyzer.claim_lobby_click() is True
        analyzer.on_enter_GAME_LOBBY()
        assert analyzer.claim_lobby_click() is False

    def test_an_old_click_is_cleared(self, analyzer):
        analyzer._last_lobby_play_click_ts = (
            time.time() - analyzer_module.LOBBY_PLAY_RETRY_S - 1.0)
        analyzer.on_enter_GAME_LOBBY()
        assert analyzer._last_lobby_play_click_ts == 0.0

    def test_the_invite_never_clicks_play(self):
        """The frame 1.5 s after ACCEPT still showed the solo lobby's PLAY."""
        src = Path("wingman/main.py").read_text(encoding="utf-8")
        body = src[src.index("def _click_ready_after_invite"):]
        body = body[:body.index("threading.Thread(target=_click_ready_after_invite")]
        play = body.index('elif ready == "PLAY":')
        assert play < body.index("analyzer.claim_lobby_click()")
        assert "click_crop" not in body[play:body.index("elif ready and not", play)]


class TestLobbyBlackoutThatIsABattle:
    """2026-11-19 20:08:47: GAME_STARTING went back to GAME_LOBBY 3 s before the
    match loaded, and the jet sat unflown under a 'lobby blackout' until the
    ESC ceiling opened the scoreboard over the round at 20:11:02."""

    @pytest.fixture
    def lobby(self, analyzer, monkeypatch):
        analyzer.state = "GAME_LOBBY"
        analyzer._lobby_blackout_since = time.time() - 10.0
        monkeypatch.setattr(analyzer, "_ocr_executor", _InlineExecutor(), raising=False)
        fired = []
        monkeypatch.setattr(analyzer, "_trigger", lambda t: fired.append(t))
        analyzer._fired = fired
        return analyzer

    def _reads(self, monkeypatch, health, speed, altitude):
        monkeypatch.setattr(analyzer_module, "_process_health_region",
                            lambda crop: (health, 0.0))
        monkeypatch.setattr(analyzer_module, "_process_telemetry_region",
                            lambda crop: (speed, altitude, 0.0))

    def _probe(self, a):
        a._lobby_hud_last_ts = 0.0
        a._schedule_lobby_hud_probe(np.zeros((1200, 1920, 3), dtype=np.uint8))

    def test_two_hud_frames_hand_the_lobby_to_game_starting(self, lobby, monkeypatch):
        self._reads(monkeypatch, 100, 650, 1200)
        self._probe(lobby)
        assert lobby._fired == []
        self._probe(lobby)
        assert lobby._fired == ["cancel_detected"]

    def test_a_dialog_blackout_stays_in_the_lobby(self, lobby, monkeypatch):
        """A lone health digit is the loading-screen '7' (ADR 189 D6)."""
        self._reads(monkeypatch, 7, None, None)
        for _ in range(5):
            self._probe(lobby)
        assert lobby._fired == []

    def test_a_gap_restarts_the_count(self, lobby, monkeypatch):
        self._reads(monkeypatch, 100, 650, 1200)
        self._probe(lobby)
        self._reads(monkeypatch, None, None, None)
        self._probe(lobby)
        self._reads(monkeypatch, 100, 650, 1200)
        self._probe(lobby)
        assert lobby._fired == []

    def test_cancel_detected_takes_the_lobby_to_game_starting(self):
        from wingman.state import FSM_TRANSITIONS as TRANSITIONS
        assert any(t["trigger"] == "cancel_detected" and t["source"] == "GAME_LOBBY"
                   and t["dest"] == "GAME_STARTING" for t in TRANSITIONS)

    def test_analyze_frame_runs_the_probe_only_after_the_popup_pass(self):
        assert analyzer_module.LOBBY_HUD_CHECK_AFTER_S >= 5.0
        src = Path("wingman/analyzer.py").read_text(encoding="utf-8")
        # analyze_frame reaches _detect_respawn_ocr on every GAME_LOBBY tick;
        # its early return for the lobby states comes after the probe.
        body = src[src.index("    def _detect_respawn_ocr(self, frame):"):]
        body = body[:body.index("    def ", 10)]
        probe = body.index("self._schedule_lobby_hud_probe(frame)")
        assert probe < body.index("GameState.GAME_WAITING):")


class TestRewardCrateInALobbyBlackout:
    """ADR 196 D7: 2026-11-19 05:14, a '+300 SILVER' crate stranded GAME_LOBBY
    for 15 min; ESC every 45 s did nothing, the 900 s limit ended the session."""

    def test_a_lobby_blackout_scans_the_crate(self, analyzer):
        analyzer._lobby_blackout_since = time.time() - (analyzer._stall_action_after_s + 1.0)
        assert "STALL_PARTS_CRATE" in analyzer._stall_recovery_targets(GameState.GAME_LOBBY)

    def test_a_fresh_blackout_does_not(self, analyzer):
        analyzer._lobby_blackout_since = time.time()
        assert "STALL_PARTS_CRATE" not in analyzer._stall_recovery_targets(GameState.GAME_LOBBY)

    def test_the_label_reads_silver_crates(self):
        assert "SILVER" in load_config()["crops"]["STALL_PARTS_CRATE"]["text"]

    def test_the_silver_label_sits_in_the_crate_label_crop(self):
        """The fixture is rows 380-420, x 160-520 of the 05:14:59 frame; the
        card's text must fall inside the crop's rows and columns."""
        import cv2
        strip = cv2.imread(str(Path(__file__).parent / "fixtures"
                               / "parts_crate_silver_label.png"))
        ys, xs = np.nonzero(strip.min(axis=2) > 200)      # the white letters
        (x1, y1), (x2, y2) = load_config()["crops"]["STALL_PARTS_CRATE"]["coords"]
        assert y1 * 1200 <= 380 + ys.min() and 380 + ys.max() <= y2 * 1200
        assert x1 * 1920 <= 160 + xs.min() and 160 + xs.max() <= x2 * 1920


class TestJoinRefusedHoldsReady:
    """ADR 196 D8: each READY into a squad that is already queueing raised
    "Unable to join" again (2026-11-19 22:00, three in 17 s)."""

    def test_the_refusal_window_and_the_hold(self, analyzer):
        assert analyzer.join_refused_recently() is False
        analyzer._join_refused_ts = time.time() - 5.0
        assert analyzer.join_refused_recently() is True
        analyzer._join_refused_ts = time.time() - (analyzer_module.JOIN_REFUSED_WAITING_S + 1)
        assert analyzer.join_refused_recently() is False
        assert analyzer.join_refused_recently(analyzer_module.JOIN_REFUSED_HOLD_S) is True

    def test_the_hold_sits_before_the_click_and_covers_play(self):
        """2026-11-20 02:30: the solo PLAY was refused too, and re-clicked
        every few seconds."""
        src = Path("wingman/analyzer.py").read_text(encoding="utf-8")
        hold = src.index("elif self.join_refused_recently(JOIN_REFUSED_HOLD_S):")
        branch = src[hold:src.index("\n                        elif ", hold + 10)]
        assert "handled = True" in branch
        assert "emit(GameEvent.LOBBY_PLAY_CLICK" not in branch
        assert hold < src.index("self.emit(GameEvent.LOBBY_PLAY_CLICK, crop, frame)")
        assert src.index("if crop == \"UNABLE_TO_JOIN\":") > 0

    def test_a_click_clears_the_refusal_it_follows(self, analyzer):
        """2026-11-20 02:30:16: a PLAY was bounced 0.5 s after it was
        clicked, on the refusal of the click before."""
        analyzer._join_refused_ts = time.time() - 4.0
        analyzer._last_lobby_play_click_ts = 0.0
        assert analyzer.claim_lobby_click() is True
        assert analyzer.join_refused_recently() is False
        src = Path("wingman/analyzer.py").read_text(encoding="utf-8")
        click = src.index("self.emit(GameEvent.LOBBY_PLAY_CLICK, crop, frame)")
        assert "self._join_refused_ts = 0.0" in src[click - 200:click]
