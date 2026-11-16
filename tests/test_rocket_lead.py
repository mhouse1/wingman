"""ADR 180: a weapon that is held on its lead (the rocket pod) holds `f` on the
lead diamond instead of tapping. Which weapon is selected comes from its name
(tests/test_weapon_name.py).

The fixtures in tests/fixtures/rockets/ are crops of the operator's `v`
screenshots of 2026-11-15 (tests/test-output/screenshot_20261115_*.png), checked
by eye, enumerated here. Diamond windows are 480 x 360 px with the lead diamond
at (240, 180); the `nodiamond` window is the best non-diamond match seen, the
crosshair over a tilted missile box with the AIM-9M selected, at (240, 180).
"""

import threading
from pathlib import Path

import cv2
import numpy as np
import pytest
import yaml

import wingman.controller as controller_module
from tests.perception_fake import PerceptionFake
from wingman.controller import FIRE_ACTIVE_WEAPON, Controller
from wingman.controller_config import ControllerConfig
from wingman.lead_ring import ring_template
from wingman.rocket_lead import Diamond, RocketLead, diamond_template, find_diamonds
from wingman.weapon_name import SelectedWeapon

_FIX = Path(__file__).parent / "fixtures" / "rockets"
_CFG = yaml.safe_load((Path(__file__).parents[1] / "wingman" / "config.yaml").read_text())
_GREEN = (140, 235, 160)    # BGR, inside the HUD-green range
_ROCKETS = SelectedWeapon("LAU-3 Hydra Rocket Pod", "rocket", "hold_on_lead")
_AIM9 = SelectedWeapon("AIM-9M Sidewinder", "heat_seeking", "tap")


def _need(name):
    path = _FIX / name
    if not path.exists():
        pytest.skip(f"fixture {name} not on this machine (tests/fixtures is untracked)")
    return cv2.imread(str(path))


# -- the lead diamond ---------------------------------------------------------------

@pytest.mark.parametrize("name", ["diamond_140836.png", "diamond_140847.png"])
def test_the_lead_diamond_is_found_where_it_is(name):
    frame = _need(name)
    found = find_diamonds(frame, (300, 250), 450, 0.6, scale=1.0)
    assert found and abs(found[0].x - 240) <= 2 and abs(found[0].y - 180) <= 2


def test_the_crosshair_over_a_missile_box_is_not_a_diamond():
    frame = _need("nodiamond_crosshair_box_070334.png")
    assert find_diamonds(frame, (240, 180), 450, 0.6, scale=1.0) == []


def _paint(frame, mask, at, colour=_GREEN):
    h = mask.shape[0] // 2
    roi = frame[at[1] - h:at[1] - h + mask.shape[0], at[0] - h:at[0] - h + mask.shape[1]]
    roi[mask > 0] = colour


def test_a_drawn_diamond_is_found_at_the_frames_own_scale():
    frame = np.zeros((600, 960, 3), np.uint8)
    _paint(frame, diamond_template(0.5), (500, 300))
    found = find_diamonds(frame, (450, 280), 450, 0.6)
    assert found and abs(found[0].x - 500) <= 1 and abs(found[0].y - 300) <= 1


def test_the_gun_lead_ring_is_not_a_diamond():
    frame = np.zeros((1200, 1920, 3), np.uint8)
    _paint(frame, ring_template(1.0), (900, 500))
    assert find_diamonds(frame, (900, 500), 450, 0.6) == []


def test_the_panels_own_diamond_is_not_searched():
    """The selected slot's icon is a diamond too, at the bottom of the picture."""
    frame = np.zeros((1200, 1920, 3), np.uint8)
    _paint(frame, diamond_template(1.0), (1605, 1127))
    assert find_diamonds(frame, (1605, 1100), 450, 0.6) == []


def _lead_frame(at=(1000, 640)):
    frame = np.zeros((1200, 1920, 3), np.uint8)
    _paint(frame, diamond_template(1.0), at)
    return frame


def test_two_pictures_confirm_a_diamond_and_a_repeat_does_not():
    lead = RocketLead(0.6, 450, 0.45)
    frame = _lead_frame()
    assert lead.observe(frame, (1100, 700), 1.0) is None
    assert lead.observe(frame, (1100, 700), 1.0) is None       # the same picture
    d = lead.observe(frame, (1100, 700), 1.1)
    assert isinstance(d, Diamond) and (d.x, d.y) == (1000, 640)
    assert lead.aim(1.5) is not None and lead.aim(1.6) is None  # memory_s 0.45


# -- the controller -----------------------------------------------------------------

class _Weapon(PerceptionFake):
    def __init__(self, weapon):
        self.weapon = weapon

    def selected_weapon(self):
        return self.weapon


def _make_ctrl(monkeypatch, weapon=_ROCKETS):
    monkeypatch.setattr(controller_module, "keyboard_module", None)
    ctrl = Controller(
        (0, 0, 1920, 1200), exit_event=threading.Event(),
        config=ControllerConfig.from_config(_CFG, simulate_os_input=True,
                                            disable_hotkeys=True),
    )
    ctrl._analyzer = _Weapon(weapon)
    keys = []
    monkeypatch.setattr(ctrl, "_press_tracking_key", lambda k, a: keys.append(("down", k)))
    monkeypatch.setattr(ctrl, "_release_tracking_key", lambda k, a: keys.append(("up", k)))
    taps = []
    monkeypatch.setattr(ctrl, "_execute_key_press",
                        lambda key, **kw: taps.append((key, kw.get("action_name"))))
    ctrl.taps = taps
    return ctrl, keys


_OBS = {"visible": True, "centroid_x": 1100.0, "centroid_y": 700.0}


def test_the_shipped_config_builds_the_rocket_rule(monkeypatch):
    ctrl, _ = _make_ctrl(monkeypatch)
    assert ctrl._rocket_lead is not None
    assert ctrl._rocket_fire_px == 60 and ctrl._rocket_steer


def test_nothing_taps_f_while_the_rocket_pod_is_selected(monkeypatch, caplog):
    """Operator, 2026-11-15: "when rockets are engaged it should disable the
    FIRE_ACTIVE_WEAPON continuous interval". Every tapping loop comes through
    fire_active_weapon."""
    import logging
    ctrl, _ = _make_ctrl(monkeypatch)
    with caplog.at_level(logging.INFO, logger="wingman.controller"):
        ctrl.fire_active_weapon(block=False, ignore_cancel=True)
        ctrl.fire_active_weapon(block=True)
    assert ctrl.taps == []
    assert caplog.text.count("held on the lead diamond, not tapped") == 1


@pytest.mark.parametrize("weapon", [
    _AIM9,
    SelectedWeapon("R-77 Adder", "radar", "tap"),
    SelectedWeapon("Some New Weapon", None, "tap"),     # a name in no class
    None,                                               # no fresh read of the name
])
def test_every_other_weapon_is_tapped_as_before(monkeypatch, weapon):
    ctrl, keys = _make_ctrl(monkeypatch, weapon)
    ctrl.fire_active_weapon(block=False, ignore_cancel=True)
    assert ctrl.taps == [(FIRE_ACTIVE_WEAPON, "fire_active_weapon")]
    frame = _lead_frame()
    assert ctrl._rocket_view(frame, _OBS, 1.0) is None
    assert ctrl._rocket_view(frame, _OBS, 1.1) is None      # no steering on a diamond
    assert ctrl._pursuit_rocket_tick(True) is False
    assert keys == []


def test_with_the_rule_off_the_rocket_pod_is_tapped(monkeypatch):
    ctrl, _ = _make_ctrl(monkeypatch)
    ctrl._rocket_lead = None            # pursuit_mode.rocket_lead.enabled: false
    ctrl.fire_active_weapon(block=False)
    assert ctrl.taps == [(FIRE_ACTIVE_WEAPON, "fire_active_weapon")]


def test_the_chase_aims_at_the_diamond_and_holds_f_on_it(monkeypatch):
    ctrl, keys = _make_ctrl(monkeypatch)
    frame = _lead_frame()           # about 51 px from the crosshair
    assert ctrl._rocket_view(frame, _OBS, 1.0) is None
    aim = ctrl._rocket_view(frame, _OBS, 1.1)
    assert aim is not None and aim[0] > 0 and aim[1] > 0     # right of and below it
    assert 45 <= ctrl._rocket_aim_px <= 56
    assert ctrl._pursuit_rocket_tick(True) is True
    assert keys == [("down", FIRE_ACTIVE_WEAPON)]
    # Held to 1.5 times fire_px (60): 89 px holds, 91 lets go.
    ctrl._rocket_aim_px = 89.0
    ctrl._pursuit_rocket_tick(True)
    assert keys == [("down", FIRE_ACTIVE_WEAPON)]
    ctrl._rocket_aim_px = 91.0
    ctrl._pursuit_rocket_tick(True)
    assert keys[-1] == ("up", FIRE_ACTIVE_WEAPON)
    assert ctrl._rocket_holds == 1


def test_a_diamond_off_the_crosshair_steers_but_does_not_fire(monkeypatch):
    ctrl, keys = _make_ctrl(monkeypatch)
    frame = _lead_frame(at=(1200, 700))     # about 257 px out
    ctrl._rocket_view(frame, _OBS, 1.0)
    assert ctrl._rocket_view(frame, _OBS, 1.1) is not None
    assert ctrl._pursuit_rocket_tick(True) is True
    assert keys == []


def test_switching_away_from_the_pod_lets_go_of_f(monkeypatch):
    ctrl, keys = _make_ctrl(monkeypatch)
    frame = _lead_frame()
    ctrl._rocket_view(frame, _OBS, 1.0)
    ctrl._rocket_view(frame, _OBS, 1.1)
    ctrl._pursuit_rocket_tick(True)
    ctrl._analyzer.weapon = _AIM9
    ctrl._rocket_view(frame, _OBS, 1.2)
    assert ctrl._pursuit_rocket_tick(True) is False
    assert keys == [("down", FIRE_ACTIVE_WEAPON), ("up", FIRE_ACTIVE_WEAPON)]


def test_a_takeover_or_cancel_releases_the_rocket_hold(monkeypatch):
    ctrl, keys = _make_ctrl(monkeypatch)
    frame = _lead_frame()
    ctrl._rocket_view(frame, _OBS, 1.0)
    ctrl._rocket_view(frame, _OBS, 1.1)
    ctrl._pursuit_rocket_tick(True)
    ctrl.release_tracking_holds(why="test")
    assert ("up", FIRE_ACTIVE_WEAPON) in keys
    assert ctrl._rocket_held_since is None


def test_a_lost_target_lets_go(monkeypatch):
    ctrl, keys = _make_ctrl(monkeypatch)
    frame = _lead_frame()
    ctrl._rocket_view(frame, _OBS, 1.0)
    ctrl._rocket_view(frame, _OBS, 1.1)
    ctrl._pursuit_rocket_tick(True)
    ctrl._pursuit_rocket_tick(False)
    assert keys[-1] == ("up", FIRE_ACTIVE_WEAPON)
