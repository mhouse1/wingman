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
    c._operator_stop_event = threading.Event()
    return c


def test_request_and_cancel_toggle():
    from wingman.controller import Controller
    c = _controller()
    assert Controller.finish_round_requested(c) is False
    Controller.request_finish_round(c, True)
    assert Controller.finish_round_requested(c) is True
    Controller.request_finish_round(c, False)      # V3 — a second press cancels
    assert Controller.finish_round_requested(c) is False


def test_the_deferred_exit_is_broader_than_the_guards_safe_point():
    """Supersedes the original V1/V2, which required the hotkey to share the
    guards' lobby-only `_safe`. That made 'z' dead in exactly the states an
    operator presses it from — GAME_UNKNOWN at startup above all, before the
    first classification has happened.

    The guards and the hotkey mean different things. A guard exit means
    "restart wingman at a clean moment" and should wait for a real lobby. 'z'
    means "stop unless that would abandon an aircraft in flight"."""
    assert ("if ctrl.finish_round_requested() and not (_in_round or _ending_round"
            in MAIN_SRC)
    assert "_in_round = analyzer.game_state in BATTLE_STATES" in MAIN_SRC
    # The guards keep the narrow, lobby-only condition.
    assert "if liveness.should_stop() and _safe:" in MAIN_SRC
    assert "_safe = (analyzer.game_state == GameState.GAME_LOBBY" in MAIN_SRC


def test_the_exit_still_never_fires_mid_round():
    """The one guarantee that must survive the broadening: no exit while an
    aircraft is in flight, which is what ADR 094 exists to protect."""
    from wingman.analyzer import BATTLE_STATES, GameState
    assert {
        GameState.GAME_BATTLE,
        GameState.GAME_BATTLE_MANUAL,
        GameState.GAME_BATTLE_EJECT,
    } == BATTLE_STATES


def test_states_that_must_now_permit_an_immediate_exit():
    """The states the old condition wrongly excluded. GAME_UNKNOWN is the one
    that prompted the change.

    GAME_END_B was on this list and has been removed: it is post-scoring, so
    nothing in flight is lost, but exiting there strands MetalStorm on the
    end-of-round screen and the next session opens outside the lobby. It is now
    handled by _ending_round below — deferred, not immediate."""
    from wingman.analyzer import BATTLE_STATES, GameState
    for st in (GameState.GAME_UNKNOWN, GameState.GAME_LOBBY,
               GameState.GAME_WAITING, GameState.GAME_STARTING,
               GameState.GAME_STARTING_STALLED):
        assert st not in BATTLE_STATES, st


def test_the_end_of_round_screen_defers_the_exit():
    """The 2026-09-01 report: 'z' stopped in GAME_END_B one second after the
    state was entered, leaving the game on the end screen. GAME_END_B is not a
    battle state, so only the dedicated clause holds the exit there."""
    assert "_ending_round = analyzer.game_state == GameState.GAME_END_B" in MAIN_SRC
    # The flag has to actually gate the exit, not merely be computed.
    assert ("if ctrl.finish_round_requested() and not (_in_round or _ending_round"
            in MAIN_SRC)


def test_the_settle_is_timed_from_the_click_not_the_state_change():
    """Waiting on GAME_LOBBY alone would race: the analyzer can report the lobby
    before the game has finished settling into it. The window runs from the
    click that demonstrably went out."""
    assert "FINISH_ROUND_SETTLE_S = 3.0" in MAIN_SRC
    assert "final_continue_ts[0] > 0.0" in MAIN_SRC
    assert "time.time() - final_continue_ts[0] < FINISH_ROUND_SETTLE_S" in MAIN_SRC


def test_the_deferred_exit_cannot_wait_forever():
    """Both new waits are bounded. Without the stall guard a stuck click-to OCR
    would hold 'z' in GAME_END_B for the rest of the session — the exact
    open-ended hang the hotkey exists to avoid."""
    assert "GAME_END_B timeout — click-to OCR may be stuck" in MAIN_SRC
    assert 'analyzer.trigger_event("manual_reset")' in MAIN_SRC


def test_battle_states_has_a_single_definition():
    """Three copies of this set already existed. A fourth that drifts would let
    the exit fire mid-round."""
    from wingman.analyzer import BATTLE_STATES
    from wingman.tick_handlers import _BATTLE_STATES
    assert _BATTLE_STATES is BATTLE_STATES


def test_game_close_happens_after_the_artifacts():
    """V6 ordering: a hung close must not cost the session's data."""
    src = MAIN_SRC
    assert src.index("resource_sampler.summarize()") < src.index("close_game("), \
        "close_game must run after the artifacts are written"
    assert src.index("stats_tracker.finalize") < src.index("close_game(")


def test_only_the_deliberate_stop_closes_the_game():
    """V9: a guard exit means 'restart wingman' — the client should stay up.

    Amended for ADR 099: the gate widened from the finish-round exit alone to
    any OPERATOR-initiated stop ('z' or Backspace). Guard exits and the
    startup-stall exit are still excluded, which is the property V9 protects."""
    assert "elif finish_round_exit:" in MAIN_SRC
    assert "elif standby_armed:" in MAIN_SRC
    assert "standby_armed = (ctrl.operator_stop_requested()" in MAIN_SRC
    src = MAIN_SRC
    guard_line = src.index("MEMORY GUARD: ending session")
    close_line = src.index("def _close_session():")
    assert guard_line < close_line
    assert "close_game(" not in src[guard_line:close_line], \
        "a guard exit must not close the game"
    assert "close_nested_display(" not in src[guard_line:close_line], \
        "a guard exit must not tear down the nested display either"


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


# --- V10: pressing 'z' in the lobby must not start another round -------------

class _FakeAnalyzer:
    """Just enough analyzer to exercise the round-start suppressor."""

    def __init__(self):
        from wingman.analyzer import GameStateAnalyzer
        self._suppress_round_start = None
        self.set_round_start_suppressor = \
            GameStateAnalyzer.set_round_start_suppressor.__get__(self)
        self._round_start_suppressed = \
            GameStateAnalyzer._round_start_suppressed.__get__(self)


def test_no_suppressor_installed_permits_the_click():
    """The on-screen default: nothing installed means nothing suppressed."""
    a = _FakeAnalyzer()
    assert a._round_start_suppressed() is False


def test_pending_exit_suppresses_the_round_start():
    a = _FakeAnalyzer()
    a.set_round_start_suppressor(lambda: True)
    assert a._round_start_suppressed() is True


def test_no_pending_exit_permits_the_round_start():
    a = _FakeAnalyzer()
    a.set_round_start_suppressor(lambda: False)
    assert a._round_start_suppressed() is False


def test_a_raising_suppressor_does_not_take_perception_down():
    """Fail open, not closed. A broken predicate must not silently stop wingman
    playing — that is the guard-disables-the-system failure mode."""
    def boom():
        raise RuntimeError("predicate exploded")
    a = _FakeAnalyzer()
    a.set_round_start_suppressor(boom)
    assert a._round_start_suppressed() is False


# --- the suppressor is actually wired, and gates the FSM trigger -------------

def test_main_wires_the_suppressor_to_the_pending_exit():
    assert "analyzer.set_round_start_suppressor(ctrl.finish_round_requested)" in MAIN_SRC


def test_the_guard_precedes_the_play_clicked_trigger():
    """The gate must sit at the analyzer's click site, not in the subscriber.
    That site also fires _trigger("play_clicked"), so a subscriber that declined
    to click would still move the FSM to GAME_WAITING and strand the exit for a
    whole further round."""
    src = Path("wingman/analyzer.py").read_text()
    guard = src.index("elif self._round_start_suppressed():")   # the call site
    # The suppressed branch must end at the `else:` that does the clicking, so
    # neither the click nor the FSM trigger is reachable from it.
    branch = src[guard:src.index("\n                        else:", guard)]
    assert "handled = True" in branch
    assert "emit(GameEvent.LOBBY_PLAY_CLICK" not in branch
    assert 'self._trigger("play_clicked")' not in branch


def test_the_other_automatic_round_starts_are_gated():
    """Invite-accept and stall-recovery also click PLAY/READY from the lobby."""
    assert MAIN_SRC.count("ctrl.finish_round_requested()") >= 3


# --- ADR 099: the nested display is torn down with the game ------------------

def _proc_tree(tmp_path, entries):
    """Build a fake /proc. entries: {pid: (comm, [argv...])}"""
    for pid, (comm, argv) in entries.items():
        d = tmp_path / str(pid)
        d.mkdir()
        (d / "comm").write_text(comm + "\n")
        (d / "cmdline").write_bytes(b"\0".join(a.encode() for a in argv) + b"\0")
    return str(tmp_path)


def test_the_nested_server_is_found_by_exact_display(tmp_path, monkeypatch):
    from wingman import game_shutdown as gs
    root = _proc_tree(tmp_path, {
        4523: ("Xwayland", ["/usr/bin/Xwayland", ":0", "-rootless"]),
        9001: ("Xwayland", ["Xwayland", ":3", "-geometry", "1920x1200"]),
    })
    monkeypatch.setattr(gs, "_PROC", root)
    assert gs.find_nested_display_pids(":3") == [9001]


def test_the_operators_own_xwayland_is_never_a_candidate(tmp_path, monkeypatch):
    """The failure that must not happen: a substring match over the command line
    would catch `Xwayland :0`, and killing that takes the whole desktop down."""
    from wingman import game_shutdown as gs
    root = _proc_tree(tmp_path, {
        4523: ("Xwayland", ["/usr/bin/Xwayland", ":0", "-rootless", "-core"]),
    })
    monkeypatch.setattr(gs, "_PROC", root)
    assert gs.find_nested_display_pids(":3") == []


def test_a_display_number_is_matched_whole(tmp_path, monkeypatch):
    """':3' is a substring of ':30'."""
    from wingman import game_shutdown as gs
    root = _proc_tree(tmp_path, {
        9002: ("Xwayland", ["Xwayland", ":30", "-geometry", "800x600"]),
    })
    monkeypatch.setattr(gs, "_PROC", root)
    assert gs.find_nested_display_pids(":3") == []


def test_non_xwayland_processes_are_ignored(tmp_path, monkeypatch):
    from wingman import game_shutdown as gs
    root = _proc_tree(tmp_path, {
        9003: ("bash", ["bash", ":3"]),
    })
    monkeypatch.setattr(gs, "_PROC", root)
    assert gs.find_nested_display_pids(":3") == []


def test_closing_an_absent_nested_display_is_not_an_error(monkeypatch):
    from wingman import game_shutdown as gs
    monkeypatch.setattr(gs, "find_nested_display_pids", lambda d: [])
    r = gs.close_nested_display(":3", grace_s=0, sleep=lambda s: None)
    assert r["found"] == 0 and r["ok"] is True


def test_closing_the_nested_display_never_raises(monkeypatch):
    """A stop path must never fail to stop."""
    from wingman import game_shutdown as gs
    def boom(_d):
        raise RuntimeError("proc scan exploded")
    monkeypatch.setattr(gs, "find_nested_display_pids", boom)
    assert gs.close_nested_display(":3", grace_s=0, sleep=lambda s: None)["ok"] is False


def test_an_empty_display_matches_nothing():
    from wingman.game_shutdown import find_nested_display_pids
    assert find_nested_display_pids("") == []


def test_the_display_is_torn_down_after_the_game_not_before():
    """Ordering has teeth: killing the server first yanks the game's display
    out from under it mid-shutdown."""
    game = MAIN_SRC.index("close_game(")
    nested = MAIN_SRC.index("close_nested_display(nested_display")
    assert game < nested


def test_the_teardown_is_gated_on_close_game():
    """close_game: false means "leave MetalStorm running"; killing the server
    would close it anyway, so the two must not disagree — and standby would have
    nothing to offer, so it is not armed either."""
    assert "if not _close_enabled:" in MAIN_SRC
    assert "and _close_enabled)" in MAIN_SRC
    body = MAIN_SRC[MAIN_SRC.index("def _close_session():"):
                    MAIN_SRC.index("if not _close_enabled:")]
    assert "close_game(" in body and "close_nested_display(nested_display" in body


# --- ADR 099: Backspace tears the session down too ---------------------------

def test_backspace_sets_a_flag_distinct_from_exit_requested():
    """exit_requested cannot stand in for the operator's press: SIGTERM and the
    startup-stall exit set it too, and the stall path deliberately leaves the
    game up for inspection ("check the game window and relaunch")."""
    from wingman.controller import Controller
    c = _controller()
    assert Controller.operator_stop_requested(c) is False
    c._operator_stop_event.set()
    assert Controller.operator_stop_requested(c) is True


def test_the_backspace_handler_sets_the_operator_stop_flag():
    src = Path("wingman/controller.py").read_text()
    handler = src[src.index("def exit_script_hotkey("):
                  src.index("keyboard_module.on_press_key('backspace'")]
    assert "self._operator_stop_event.set()" in handler


def test_teardown_covers_both_operator_stops():
    """'z' closes at once; Backspace closes after the second press, via standby.
    Guard exits reach neither branch."""
    assert "elif finish_round_exit:" in MAIN_SRC
    assert "elif standby_armed:" in MAIN_SRC
    standby = MAIN_SRC[MAIN_SRC.index("elif standby_armed:"):]
    assert "_close_session()" in standby


def test_guard_and_stall_exits_still_leave_the_session_up():
    """The startup-stall exit sets exit_requested and says the game is left
    running. Keying teardown off exit_requested would silently contradict it."""
    assert "if exit_requested.is_set():" in MAIN_SRC
    # The teardown gates must be the operator flags, never exit_requested.
    # Comments are stripped: the block explains in prose why exit_requested is
    # the wrong signal, and matching that would be checking the wrong thing.
    teardown = MAIN_SRC[MAIN_SRC.index("standby_armed = (ctrl.operator_stop_requested()"):]
    code = "\n".join(ln for ln in teardown.splitlines()
                     if not ln.strip().startswith("#"))
    assert "exit_requested" not in code


# --- ADR 099: two-stage Backspace, standby between the presses ---------------

def _ctrl_with_exit():
    from wingman.controller import Controller
    import threading
    c = Controller.__new__(Controller)
    c._finish_round_event = threading.Event()
    c._operator_stop_event = threading.Event()
    c._close_all_event = threading.Event()
    c._last_exit_press = 0.0
    c._exit_event = threading.Event()
    return c


def test_first_backspace_stops_but_does_not_close():
    from wingman.controller import Controller
    c = _ctrl_with_exit()
    assert Controller.operator_stop_requested(c) is False
    c._operator_stop_event.set()          # what the handler does on press one
    assert Controller.operator_stop_requested(c) is True
    assert Controller.close_all_requested(c) is False, \
        "the first press must never close MetalStorm — the operator is still flying"


def test_second_backspace_requests_the_close():
    from wingman.controller import Controller
    c = _ctrl_with_exit()
    c._operator_stop_event.set()
    c._close_all_event.set()
    assert Controller.close_all_requested(c) is True


def test_the_handler_is_debounced_against_auto_repeat():
    """X auto-repeats a held key at ~25 Hz. Undebounced, one long press reads as
    both stages and closes the game the operator meant to keep."""
    src = Path("wingman/controller.py").read_text()
    handler = src[src.index("def exit_script_hotkey("):
                  src.index("keyboard_module.on_press_key('backspace'")]
    assert "_last_exit_press" in handler
    assert "0.5" in handler


def test_the_second_press_is_distinguished_by_the_first_flag():
    src = Path("wingman/controller.py").read_text()
    handler = src[src.index("def exit_script_hotkey("):
                  src.index("keyboard_module.on_press_key('backspace'")]
    first = handler.index("if self._operator_stop_event.is_set():")
    assert "self._close_all_event.set()" in handler[first:]


def test_standby_keeps_the_hotkey_hooks_alive():
    """Deregistering hooks on the first press would leave nothing listening for
    the second — the whole feature is unreachable."""
    assert "ctrl.cleanup(keep_hotkeys=standby_armed)" in MAIN_SRC
    ctrl_src = Path("wingman/controller.py").read_text()
    assert "def cleanup(self, keep_hotkeys: bool = False)" in ctrl_src
    assert "if keep_hotkeys:" in ctrl_src


def test_standby_still_releases_every_injected_key():
    """The operator flies manually straight after: a key wingman was holding
    would fight them. cleanup() must run its release pass regardless."""
    ctrl_src = Path("wingman/controller.py").read_text()
    body = ctrl_src[ctrl_src.index("def cleanup(self, keep_hotkeys"):]
    release = body.index("all injectable keys released")
    hooks = body.index("if keep_hotkeys:")
    assert release < hooks, "keys must be released before the hook decision"


def test_z_still_closes_immediately_without_standby():
    """'z' means "I am done" — it must not drop into standby."""
    block = MAIN_SRC[MAIN_SRC.index("elif finish_round_exit:"):
                     MAIN_SRC.index("elif standby_armed:")]
    assert "_close_session()" in block


def test_standby_is_not_armed_when_close_game_is_disabled():
    assert "and _close_enabled)" in MAIN_SRC


def test_standby_is_not_armed_for_the_finish_round_exit():
    assert "and not finish_round_exit" in MAIN_SRC
