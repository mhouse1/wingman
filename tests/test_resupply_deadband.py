"""pursuit_mode.resupply_priority.deadband: the resupply marker is held closer to
the centre than a target is.

Operator, 2026-10-13: "it just missed the resupply". From 03:56:59 to 03:57:12
the marker sat about 50 px right of the bore sight and 10 to 60 px below it, at
the edge of the steering's 0.05 deadband (48 by 30 px, about the size of the
marker's own ring), and then swept away with no rearm. The resupply point is
flown through, so a marker parked at the edge of that box is a pass beside it.
"""

import threading
import time

import numpy as np
import yaml

import wingman.controller as controller_module
from wingman.config_schema import schema_default
from wingman.controller import NOSE_UP_KEY, ROLL_RIGHT_KEY, Controller
from wingman.controller_config import ControllerConfig
from wingman.resupply import ResupplyMarker
from tests.test_pursuit_mode import (
    _AnalyzerStub, _CaptureStub, _TrackerStub, _keys, _wait_for_pursuit_to_settle,
)

CENTRE_X, CENTRE_Y = 960, 600
# 38 px right of the centre and 21 px above it: 0.040 and -0.035 of a half-frame,
# inside the target's 0.05 on both axes and outside the resupply marker's 0.025.
OFF_X, OFF_Y = 38, -21


class _BlankCapture(_CaptureStub):
    def grab_from_thread(self):
        self.grabs += 1
        return np.zeros((1200, 1920, 3), dtype=np.uint8)


def _make_ctrl(monkeypatch, *, ammo, resupply=None):
    monkeypatch.setattr(controller_module, "keyboard_module", None)
    return Controller(
        (0, 0, 1920, 1200),
        analyzer=_AnalyzerStub(ammo=ammo),
        exit_event=threading.Event(),
        capture=_BlankCapture(),
        config=ControllerConfig(
            simulate_os_input=True,
            disable_hotkeys=True,
            tracking={"sustained_hold_enabled": True},
            telemetry={
                "eject_closed_loop": {"enabled": False, "legacy_nose_hold_s": 0.05,
                                      "eject_max_s": 0.2, "heatdive_enabled": False},
                "stale_after_s": 0.3,
            },
            pursuit_mode={
                "enabled": True,
                "pursuit_max_duration_s": 5.0,
                "pursuit_padlock_verify": False,
                "ammo_zero_grace_s": 0.0,
                "search_resume_delay_s": 0.0,
                "search_resume_centre_delay_s": 0.0,
                "empty_confirm_reads": 1,
                "search_climb_alt_m": 0.0,
                "resupply_priority": {"enabled": True, "actuate": True,
                                      "rearm_climb_s": 0.0, **(resupply or {})},
            },
        ),
    )


def _fly(monkeypatch, caplog, *, marker, tracker, ammo, resupply=None, seconds=0.9):
    """Run the pursuit for `seconds`; returns (keys pressed, log text)."""
    caplog.set_level("DEBUG", logger="wingman.controller")
    monkeypatch.setattr(controller_module, "find_resupply_marker",
                        lambda _frame, **_kw: marker)
    ctrl = _make_ctrl(monkeypatch, ammo=ammo, resupply=resupply)
    ctrl.set_target_tracker(tracker)
    ctrl.pursue_and_engage()
    try:
        time.sleep(seconds)
        keys, logs = _keys(ctrl), caplog.text
    finally:
        ctrl._eject_stop.set()
        _wait_for_pursuit_to_settle(ctrl)
    return keys, logs


def _marker(dx, dy):
    return ResupplyMarker(CENTRE_X + dx, CENTRE_Y + dy, 55, 55, 900, 0)


def _no_target():
    return _TrackerStub(visible=False, error_norm=None, error_norm_y=None)


def test_a_resupply_marker_inside_the_targets_deadband_is_still_steered_at(monkeypatch, caplog):
    """38 px right and 21 px up: "centred" for a target, a pass beside the
    resupply point."""
    keys, logs = _fly(monkeypatch, caplog, marker=_marker(OFF_X, OFF_Y),
                      tracker=_no_target(), ammo=0)
    assert "rearm focus begins" in logs
    assert ("key_press", ROLL_RIGHT_KEY) in keys
    assert ("key_press", NOSE_UP_KEY) in keys


def test_a_resupply_marker_inside_its_own_deadband_is_left_alone(monkeypatch, caplog):
    """15 px right and 9 px up: 0.016 and -0.015."""
    keys, logs = _fly(monkeypatch, caplog, marker=_marker(15, -9),
                      tracker=_no_target(), ammo=0)
    assert "rearm focus begins" in logs
    assert ("key_press", ROLL_RIGHT_KEY) not in keys
    assert ("key_press", NOSE_UP_KEY) not in keys


def test_a_target_the_same_distance_off_keeps_its_wider_deadband(monkeypatch, caplog):
    """Missiles aboard and no marker: the tracker's target at 0.040 and -0.035
    is inside tracking's 0.05, as before."""
    target = _TrackerStub(visible=True, error_norm=OFF_X / 960, error_norm_y=OFF_Y / 600)
    keys, logs = _fly(monkeypatch, caplog, marker=None, tracker=target, ammo=2)
    assert "rearm focus begins" not in logs
    assert ("key_press", ROLL_RIGHT_KEY) not in keys
    assert ("key_press", NOSE_UP_KEY) not in keys


def test_the_old_box_can_be_restored(monkeypatch, caplog):
    keys, logs = _fly(monkeypatch, caplog, marker=_marker(OFF_X, OFF_Y),
                      tracker=_no_target(), ammo=0, resupply={"deadband": 0.05})
    assert "rearm focus begins" in logs
    assert ("key_press", ROLL_RIGHT_KEY) not in keys
    assert ("key_press", NOSE_UP_KEY) not in keys


def test_shipped_value_is_the_default(monkeypatch):
    with open("wingman/config.yaml", encoding="utf-8") as f:
        shipped = yaml.safe_load(f)
    value = shipped["pursuit_mode"]["resupply_priority"]["deadband"]
    assert value == schema_default("pursuit_mode.resupply_priority.deadband") == 0.025
    # Tighter than a target's, which is unchanged.
    assert value < shipped["tracking"]["deadband"] == 0.05
    ctrl = _make_ctrl(monkeypatch, ammo=0)
    assert ctrl._resupply_deadband == 0.025
