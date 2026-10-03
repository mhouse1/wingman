"""CR-018-10: takeover and shutdown stop the flight writers from one registry.

Before this, release_for_manual_takeover() and cleanup() each kept their own
list of writers to stop, and the lists drifted three times: the disengage roll
was missing from takeover until 2026-09-09 and from cleanup until CR-019-04, and
the afterburner evade was missing from both until CR-018-07. cleanup() also
waited for only three of the hold threads.

The structural test is the point: a new thread in controller.py fails it until
it is added to Controller._flight_writers() or exempted here with a reason.
"""

import ast
import logging
import pathlib
import threading
import time
import types

import wingman.controller as controller_module
from wingman.analyzer import GameState
from wingman.controller import Controller
from wingman.controller_config import ControllerConfig

# Threads that are not flight writers, or end without their own stop.
_EXEMPT = {
    "_execute_key_press": "a bounded tap of hold_seconds that ends on _mission_cancel, "
                          "and refuses non-flare keys in GAME_BATTLE_MANUAL at entry",
    "padlock_target_switch": "two padlock taps through _execute_key_press",
    "click_grid_region": "a mouse click, not flight input",
    "click_crop": "a mouse click, not flight input",
    "_start_game_starting_loop": "the lobby 'u' loop and its OCR scans; it exits when "
                                 "the FSM leaves GAME_STARTING and holds no flight key",
}


def _real_ctrl(monkeypatch, state=GameState.GAME_BATTLE):
    monkeypatch.setattr(controller_module, "keyboard_module", None)
    return Controller(
        (0, 0, 1920, 1200),
        analyzer=types.SimpleNamespace(game_state=state),
        exit_event=threading.Event(),
        config=ControllerConfig(simulate_os_input=True, disable_hotkeys=True),
    )


# The hotkey handlers start mission threads for the controller (CR-018-13).
_SCANNED = ("wingman/controller.py", "wingman/hotkeys.py")


def _hold_tactic_attrs(tree):
    """Controller attributes built as HoldTactic(...) (CR-018-10 Phase B)."""
    attrs = set()
    for node in ast.walk(tree):
        if (isinstance(node, ast.Assign) and isinstance(node.value, ast.Call)
                and getattr(node.value.func, "id", None) == "HoldTactic"):
            for t in node.targets:
                if isinstance(t, ast.Attribute):
                    attrs.add(t.attr)
    return attrs


def _is_thread_start(node, tactic_attrs):
    """A threading.Thread( call, or self.<tactic>.start( on a HoldTactic, which
    starts the tactic's thread inside wingman/hold_tactic.py."""
    if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
        return False
    if node.func.attr == "Thread":
        return True
    target = node.func.value
    return (node.func.attr == "start" and isinstance(target, ast.Attribute)
            and target.attr in tactic_attrs)


def _thread_sites():
    """(file:line, enclosing function names) for every place a thread starts."""
    sites = []
    for path in _SCANNED:
        tree = ast.parse(pathlib.Path(path).read_text())
        tactic_attrs = _hold_tactic_attrs(tree)
        parents = {c: n for n in ast.walk(tree) for c in ast.iter_child_nodes(n)}
        for node in ast.walk(tree):
            if _is_thread_start(node, tactic_attrs):
                chain, n = [], node
                while n in parents:
                    n = parents[n]
                    if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        chain.append(n.name)
                sites.append((f"{path}:{node.lineno}", tuple(chain)))
    return sites


def test_every_thread_the_controller_starts_is_registered_or_exempt(monkeypatch):
    ctrl = _real_ctrl(monkeypatch)
    covered = {m for w in ctrl._flight_writers() for m in w.spawned_by} | set(_EXEMPT)
    missing = [f"{line}: {'/'.join(reversed(chain))}"
               for line, chain in _thread_sites()
               if not covered.intersection(chain)]
    assert missing == [], (
        "a thread with no registry entry: add it to Controller._flight_writers(), "
        f"or to _EXEMPT here with the reason it needs no stop: {missing}")


def test_every_name_the_registry_claims_exists(monkeypatch):
    """A renamed method would otherwise leave a stale name covering nothing."""
    ctrl = _real_ctrl(monkeypatch)
    defined = {name for _, chain in _thread_sites() for name in chain}
    for writer in ctrl._flight_writers():
        for method in writer.spawned_by:
            assert method in defined, f"{writer.name}: no thread is started in {method}"


def test_takeover_stops_every_writer(monkeypatch):
    ctrl = _real_ctrl(monkeypatch)
    calls = []
    monkeypatch.setattr(ctrl, "cancel_mission", lambda: calls.append("cancel_mission"))
    monkeypatch.setattr(ctrl, "release_tracking_holds",
                        lambda why=None: calls.append(f"tracking:{why}"))
    for name in ("stop_search_and_destroy_loop", "stop_boresight_engage_loop",
                 "stop_cloak_loop"):
        monkeypatch.setattr(ctrl, name, lambda n=name: calls.append(n))
    ctrl.release_for_manual_takeover()
    for event in ("_eject_stop", "_me_stop", "_climb_stop", "_boundary_turn_stop",
                  "_sg_stop", "_disengage_stop", "_ab_evade_stop"):
        assert getattr(ctrl, event).is_set(), event
    assert ctrl._eject_stop_reason == "manual takeover"
    assert calls == ["cancel_mission", "tracking:manual takeover",
                     "stop_search_and_destroy_loop", "stop_boresight_engage_loop",
                     "stop_cloak_loop"]


def test_one_failing_stop_does_not_skip_the_rest(monkeypatch):
    ctrl = _real_ctrl(monkeypatch)
    stopped = []

    def _boom():
        raise RuntimeError("loop lock wedged")

    monkeypatch.setattr(ctrl, "stop_search_and_destroy_loop", _boom)
    monkeypatch.setattr(ctrl, "stop_cloak_loop", lambda: stopped.append("cloak"))
    ctrl.release_for_manual_takeover()
    assert stopped == ["cloak"]


def test_cleanup_waits_for_a_hold_it_used_to_abandon(monkeypatch):
    """cleanup() used to join only the eject, boundary-turn and missile-evade
    threads (and the afterburner evade from CR-018-07). A climb hold was left to
    daemon death, so its finally, which releases its keys with the echo grace,
    could be cut off by process exit."""
    ctrl = _real_ctrl(monkeypatch)
    finished = threading.Event()

    def _hold():
        try:
            ctrl._climb_stop.wait(timeout=10.0)
            time.sleep(0.2)                   # the finally's own work
        finally:
            finished.set()

    ctrl._climb_thread = threading.Thread(target=_hold, daemon=True)
    ctrl._climb_thread.start()
    ctrl.cleanup()
    assert finished.is_set(), "cleanup() returned before the climb hold's finally ran"


def test_a_thread_that_ignores_its_stop_cannot_hang_shutdown(monkeypatch, caplog):
    ctrl = _real_ctrl(monkeypatch)
    monkeypatch.setattr(controller_module, "_WRITER_JOIN_BUDGET_S", 0.3)
    release = threading.Event()
    stuck = threading.Thread(target=lambda: release.wait(timeout=10.0), daemon=True)
    stuck.start()
    ctrl._sg_thread = stuck
    t0 = time.time()
    try:
        with caplog.at_level(logging.WARNING, logger="wingman.controller"):
            ctrl.cleanup()
        assert time.time() - t0 < 2.0
        assert "spawn guard thread still running after its stop" in caplog.text
    finally:
        release.set()
