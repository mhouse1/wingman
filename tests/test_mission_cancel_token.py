"""CR-018-19 (carried from CR-016-01): a cancel that races a mission's start is kept.

Every mission cleared `_mission_cancel` on entry, so a cancel issued between the
launch and the new thread reaching that line was swallowed and the mission flew
anyway. The fixture teardown in test_mission_cancel.py had worked around it by
re-asserting the cancel until the lock freed. Automatic launches now take a token
before the thread starts, and the entry refuses when a cancel has overtaken it.
"""

import pathlib
import threading
import time

import pytest

import wingman.controller as controller_module
from tests.perception_fake import PerceptionFake
from wingman.controller import Controller
from wingman.controller_config import ControllerConfig
from wingman.state import GameState


class _Battle(PerceptionFake):
    game_state = GameState.GAME_BATTLE


@pytest.fixture
def ctrl(monkeypatch):
    monkeypatch.setattr(controller_module, "keyboard_module", None)
    c = Controller((0, 0, 1920, 1200), analyzer=_Battle(), exit_event=threading.Event(),
                   config=ControllerConfig(simulate_os_input=True, disable_hotkeys=True))
    yield c
    c.cancel_mission()
    deadline = time.time() + 5.0
    while c._mission_lock.locked() and time.time() < deadline:
        time.sleep(0.05)


@pytest.mark.parametrize("mission", ["mission_j20", "mission_su30", "mission_f111",
                                     "mission_jas39", "mission_loiter"])
def test_a_cancel_after_the_request_stops_the_mission_before_it_starts(ctrl, mission):
    """The race itself: token taken at the launch, cancel, then the entry."""
    token = ctrl.mission_token()
    ctrl.cancel_mission()
    t0 = time.time()
    getattr(ctrl, mission)(token=token)
    assert time.time() - t0 < 1.0, "a refused mission must return at once"
    assert not ctrl._mission_lock.locked(), "the refusal must not keep the lock"
    assert ctrl._mission_cancel.is_set(), "the cancel must survive the refused entry"


def test_a_current_token_starts_the_mission(ctrl):
    token = ctrl.mission_token()
    t = threading.Thread(target=ctrl.mission_j20, kwargs={"token": token}, daemon=True)
    t.start()
    deadline = time.time() + 2.0
    while not ctrl.is_mission_running() and time.time() < deadline:
        time.sleep(0.02)
    assert ctrl.is_mission_running()
    ctrl.cancel_mission()
    t.join(timeout=5.0)
    assert not t.is_alive()


def test_an_operator_launch_is_newer_than_any_cancel_before_it(ctrl):
    """token=None: the hotkey press itself is the request."""
    ctrl.cancel_mission()
    t = threading.Thread(target=ctrl.mission_j20, daemon=True)
    t.start()
    deadline = time.time() + 2.0
    while not ctrl.is_mission_running() and time.time() < deadline:
        time.sleep(0.02)
    assert ctrl.is_mission_running()
    ctrl.cancel_mission()
    t.join(timeout=5.0)


class _RecordingThread:
    launched = []

    def __init__(self, target=None, kwargs=None, daemon=None, **_):
        self._target, self._kwargs = target, kwargs or {}

    def start(self):
        _RecordingThread.launched.append((self._target, self._kwargs))


def test_the_default_launch_takes_its_token_before_the_thread_starts(ctrl, monkeypatch):
    monkeypatch.setattr(controller_module.threading, "Thread", _RecordingThread)
    _RecordingThread.launched = []
    ctrl.cancel_mission()
    generation = ctrl.mission_token()
    ctrl._start_default_mission()
    (_, kwargs), = _RecordingThread.launched
    assert kwargs == {"token": generation}


def test_the_respawn_restart_takes_its_token_before_the_thread_starts(ctrl, monkeypatch):
    ctrl._set_last_mission("su30")
    monkeypatch.setattr(controller_module.threading, "Thread", _RecordingThread)
    _RecordingThread.launched = []
    generation = ctrl.mission_token()
    assert ctrl.restart_last_mission() is True
    (target, kwargs), = _RecordingThread.launched
    assert target == ctrl.mission_su30 and kwargs == {"token": generation}


def test_the_flag_is_cleared_in_exactly_one_place():
    src = pathlib.Path("wingman/controller.py").read_text(encoding="utf-8")
    assert src.count("self._mission_cancel.clear()") == 1
    body = src[src.index("def _claim_mission_cancel"):src.index("def _await_mission")]
    assert "self._mission_cancel.clear()" in body
