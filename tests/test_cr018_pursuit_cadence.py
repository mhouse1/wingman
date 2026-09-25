"""Review 018 CR-018-01 and CR-018-05.

CR-018-01: the pursuit loop steers every `pursuit_mode.steer_interval_s` and
reads ammo / fires every `engage_interval_s`, and a sustained pitch hold is let
go early when the error, extrapolated `tracking.pitch_lead_s` ahead at its
measured rate, is inside the deadband or past centre.

CR-018-05: `TargetTracker.update()`/`reset()` are serialised; a caller that
cannot take the lock gets the last observation back instead of blocking.
"""

import threading
import time

import numpy as np

import wingman.controller as controller_module
from wingman.controller import FIRE_ACTIVE_WEAPON, NOSE_DOWN_KEY, Controller
from wingman.controller_config import ControllerConfig
from wingman.tracker import TargetTracker
from tests.test_pursuit_mode import (
    _AnalyzerStub, _CaptureStub, _TrackerStub, _keys, _wait_for_pursuit_to_settle,
)


def _make_ctrl(monkeypatch, *, pitch_lead_s=0.15, steer=0.1, engage=0.3, capture=None,
               analyzer=None):
    monkeypatch.setattr(controller_module, "keyboard_module", None)
    return Controller(
        (0, 0, 1920, 1200),
        analyzer=analyzer,
        exit_event=threading.Event(),
        capture=capture,
        config=ControllerConfig(
            simulate_os_input=True,
            disable_hotkeys=True,
            tracking={"sustained_hold_enabled": True, "pitch_lead_s": pitch_lead_s},
            pursuit_mode={
                "enabled": True,
                "pursuit_max_duration_s": 0.0,
                "pursuit_padlock_verify": False,
                "ammo_zero_grace_s": 0.0,
                "search_resume_delay_s": 0.0,
                "search_resume_centre_delay_s": 0.0,
                "steer_interval_s": steer,
                "engage_interval_s": engage,
            },
        ),
    )


def _seed(ctrl, age_s, err_y):
    """Pretend the previous pitch sample was `err_y`, `age_s` seconds ago."""
    ctrl._pitch_last_sample = (time.time() - age_s, err_y)


# ---------------------------------------------------------------------------
# CR-018-01: pitch lead release
# ---------------------------------------------------------------------------

class TestPitchLeadRelease:
    def test_a_fast_closing_error_releases_the_hold_before_the_deadband(self, monkeypatch):
        ctrl = _make_ctrl(monkeypatch)
        assert ctrl.orient_pitch_to_target(0.40, sustained_hold=True) == "down"
        # 0.40 -> 0.20 in 0.1 s: -2.0/s, so 0.15 s ahead is -0.10, past centre.
        _seed(ctrl, 0.1, 0.40)
        assert ctrl.orient_pitch_to_target(0.20, sustained_hold=True) is None
        assert ("key_release", NOSE_DOWN_KEY) in _keys(ctrl)
        assert ctrl._pitch_held is None

    def test_a_closing_error_presses_nothing_when_nothing_is_held(self, monkeypatch):
        ctrl = _make_ctrl(monkeypatch)
        _seed(ctrl, 0.1, 0.40)
        assert ctrl.orient_pitch_to_target(0.20, sustained_hold=True) is None
        assert ("key_press", NOSE_DOWN_KEY) not in _keys(ctrl)

    def test_a_slow_closing_error_keeps_holding(self, monkeypatch):
        ctrl = _make_ctrl(monkeypatch)
        ctrl.orient_pitch_to_target(0.40, sustained_hold=True)
        # 0.40 -> 0.35 in 0.1 s: 0.15 s ahead is 0.275, still well outside.
        _seed(ctrl, 0.1, 0.40)
        assert ctrl.orient_pitch_to_target(0.35, sustained_hold=True) == "down"
        assert ("key_release", NOSE_DOWN_KEY) not in _keys(ctrl)

    def test_a_growing_error_keeps_holding(self, monkeypatch):
        ctrl = _make_ctrl(monkeypatch)
        _seed(ctrl, 0.1, 0.20)
        assert ctrl.orient_pitch_to_target(0.30, sustained_hold=True) == "down"

    def test_a_stale_sample_gives_no_rate(self, monkeypatch):
        """The ambient tick runs at 1.5 s; a rate over that gap means nothing."""
        ctrl = _make_ctrl(monkeypatch)
        _seed(ctrl, 1.0, 0.40)
        assert ctrl.orient_pitch_to_target(0.20, sustained_hold=True) == "down"

    def test_zero_lead_restores_release_only_in_the_deadband(self, monkeypatch):
        ctrl = _make_ctrl(monkeypatch, pitch_lead_s=0.0)
        _seed(ctrl, 0.1, 0.40)
        assert ctrl.orient_pitch_to_target(0.20, sustained_hold=True) == "down"

    def test_the_early_release_logs_its_own_reason(self, monkeypatch, caplog):
        ctrl = _make_ctrl(monkeypatch)
        ctrl.orient_pitch_to_target(0.40, sustained_hold=True)
        _seed(ctrl, 0.1, 0.40)
        with caplog.at_level("DEBUG", logger="wingman.controller"):
            ctrl.orient_pitch_to_target(0.20, sustained_hold=True)
        assert any("HOLD[pitch]: down -> None (lead" in r.getMessage() for r in caplog.records)

    def test_release_tracking_holds_forgets_the_rate_sample(self, monkeypatch):
        ctrl = _make_ctrl(monkeypatch)
        ctrl.orient_pitch_to_target(0.40, sustained_hold=True)
        ctrl.release_tracking_holds()
        assert ctrl._pitch_last_sample is None

    def test_shipped_default(self, monkeypatch):
        monkeypatch.setattr(controller_module, "keyboard_module", None)
        ctrl = Controller((0, 0, 1920, 1200), exit_event=threading.Event(),
                          config=ControllerConfig(simulate_os_input=True, disable_hotkeys=True))
        assert ctrl._pitch_lead_s == 0.15
        assert ctrl._pursuit_steer_interval_s == 0.1
        assert ctrl._pursuit_engage_interval_s == 0.3


# ---------------------------------------------------------------------------
# CR-018-01: steering cadence decoupled from the fire / ammo cadence
# ---------------------------------------------------------------------------

def test_the_pursuit_steers_more_often_than_it_fires(monkeypatch):
    tracker = _TrackerStub()
    ctrl = _make_ctrl(monkeypatch, capture=_CaptureStub(), analyzer=_AnalyzerStub(ammo=2))
    ctrl.set_target_tracker(tracker)
    ctrl.pursue_and_engage(defer_switch_until_empty=True)
    time.sleep(1.2)
    ctrl.stop_eject_sequence("respawn_detected")
    _wait_for_pursuit_to_settle(ctrl)
    fires = _keys(ctrl).count(("key_press", FIRE_ACTIVE_WEAPON))
    assert fires >= 2, "the pursuit must still fire"
    # 0.1 s steering against 0.3 s engage: about three scans per fire. Loose
    # bound, for a loaded test box.
    assert tracker.updates >= 2 * fires, (tracker.updates, fires)


def test_the_engage_interval_is_never_shorter_than_the_steer_interval(monkeypatch):
    ctrl = _make_ctrl(monkeypatch, steer=0.2, engage=0.05)
    assert ctrl._pursuit_engage_interval_s == 0.2


# ---------------------------------------------------------------------------
# CR-018-05: TargetTracker serialised across callers
# ---------------------------------------------------------------------------

def _tracker():
    t = TargetTracker({"tracking": {"enabled": True}})
    t._LOCK_TIMEOUT_S = 0.02
    return t


def test_update_while_another_thread_holds_the_lock_returns_a_miss(caplog):
    t = _tracker()
    t._lock.acquire()
    try:
        with caplog.at_level("WARNING", logger="wingman.tracker"):
            obs = t.update(np.zeros((1200, 1920, 3), dtype=np.uint8))
    finally:
        t._lock.release()
    assert obs["visible"] is False and obs["error_norm"] is None
    assert any("lock busy" in r.getMessage() for r in caplog.records)


def test_update_while_locked_returns_a_copy_of_the_last_observation():
    t = _tracker()
    frame = np.zeros((1200, 1920, 3), dtype=np.uint8)
    first = t.update(frame)
    t._lock.acquire()
    try:
        again = t.update(frame)
    finally:
        t._lock.release()
    assert again == first
    again["visible"] = "mutated"
    assert t._last_obs["visible"] != "mutated"


def test_reset_while_locked_is_skipped_not_blocked():
    t = _tracker()
    t.update(np.zeros((1200, 1920, 3), dtype=np.uint8))
    mode_before = t.mode
    t._lock.acquire()
    try:
        t0 = time.time()
        t.reset()
        assert time.time() - t0 < 0.5
    finally:
        t._lock.release()
    assert t.mode == mode_before
    t.reset()
    assert t.mode.name == "SEARCHING" and t._last_obs is None
