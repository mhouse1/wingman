"""ADR 186: a special ability fired on its lead diamond (the F-106's nuke).

Operator, 2026-11-17: the nuke "needs the boresight to align with the diamond
before launching ... when nuke is available it should select the target closes to
the center of the screen, in this case as shown in screenshot_20261117_024050.png
it should select the su27". And the abilities must be "agnostic to jet type".

The fixtures in tests/fixtures/nuke/ are the operator's five `v` screenshots of
2026-11-17 02:38-02:41 (tests/test-output/screenshot_20261117_*.png), saved as
JPEG at quality 95 and checked by eye. tests/fixtures is untracked: `git add -f`.

- charging_023810: the ring part way round, no diamond;
- ready_one_diamond_023941: icon lit, one yellow diamond at (950, 523);
- ready_su27_and_fgr4_024050: icon lit, the FGR4's diamond at (491, 589) and the
  Su-27's at (946, 562), the Su-27's nearer the crosshair (960, 610);
- ready_no_diamond_024121: icon lit, no diamond (a yellow crown ring is not one);
- ready_one_diamond_2_024123: icon lit, one diamond at (732, 595).
"""

import threading
from pathlib import Path

import cv2
import numpy as np
import pytest
import yaml

import wingman.controller as controller_module
from tests.perception_fake import PerceptionFake
from wingman.aimed_ability import AimedAbility, AimView
from wingman.config_schema import SPECIAL_WEAPON_TRIGGERS
from wingman.controller import SPECIAL_ABILITY, Controller
from wingman.controller_config import ControllerConfig
from wingman.crop_region import load_crops
from wingman.rocket_lead import Diamond, diamond_template
from wingman.special_weapon import (
    TRIGGERS,
    RingGeometry,
    glyph_iou,
    glyph_mask,
    load_glyph_template,
    resolve_profile,
    ring_sectors,
)
from wingman.state import GameState

_ROOT = Path(__file__).parents[1]
_FIX = Path(__file__).parent / "fixtures" / "nuke"
_CFG = yaml.safe_load((_ROOT / "wingman" / "config.yaml").read_text(encoding="utf-8"))
_SW = _CFG["special_weapon"]
_CROSSHAIR = _CFG["pursuit_mode"]["gun_lead"]["crosshair_pct"]
_NUKE = {"ability": "nuke", "trigger": "lead_aligned", "lead": "yellow"}
_READY = ["ready_one_diamond_023941.jpg", "ready_su27_and_fgr4_024050.jpg",
          "ready_no_diamond_024121.jpg", "ready_one_diamond_2_024123.jpg"]
_ALL = ["charging_023810.jpg"] + _READY


def _need(name):
    path = _FIX / name
    if not path.exists():
        pytest.skip(f"fixture {name} not on this machine (tests/fixtures is untracked)")
    return cv2.imread(str(path))


def _box():
    return load_crops(_CFG["crops"])["SPECIAL_WEAPON"][:4]


def _aim_twice(frame):
    """Two pictures of the same frame: the diamond needs two in a row."""
    a = AimedAbility(_SW, _CROSSHAIR)
    a.view(frame, 1.0, _NUKE, True)
    return a.view(frame, 1.1, _NUKE, True)


# -- the glyph and the ring ---------------------------------------------------------

@pytest.mark.parametrize("name", _ALL)
def test_the_nuke_glyph_is_recognised_lit_or_charging(name):
    frame = _need(name)
    g = RingGeometry(_box(), frame.shape, 16)
    template = load_glyph_template(str(_ROOT / _SW["abilities"]["nuke"]["glyph"]), _box())
    assert glyph_iou(glyph_mask(frame, g.cx, g.cy, g.radius), template) >= _SW["glyph_min_iou"]


@pytest.mark.parametrize("other", [
    "refuel.png", "gun_cooldown.png", "other_missiles_arrow_f100.png", "other_j20.png",
    "other_flares_mig23.png", "other_decoy_tornado.png", "other_loop_su47.png",
    "other_arrows_f5.png", "other_mirage_miii.png", "other_mirage_2000.png"])
def test_no_other_glyph_reads_as_the_nuke(other):
    glyphs = _ROOT / "test_screenshots" / "special_weapon" / "glyphs"
    if not (glyphs / "nuke.png").exists():
        pytest.skip("glyph crops not on this machine (test_screenshots is untracked)")
    nuke = load_glyph_template(str(glyphs / "nuke.png"), _box())
    assert glyph_iou(load_glyph_template(str(glyphs / other), _box()), nuke) < _SW["glyph_min_iou"]


def test_charging_is_not_a_full_ring_and_ready_is():
    lit = []
    for name in _ALL:
        frame = _need(name)
        lit.append(ring_sectors(frame, RingGeometry(_box(), frame.shape, 16))[0])
    assert lit[0] < _SW["min_lit_sectors"]
    assert all(n >= _SW["min_lit_sectors"] for n in lit[1:])


# -- the diamond and the target nearest the centre -----------------------------------

def test_the_su27_nearest_the_centre_is_the_target():
    """The operator's example: the FGR4's diamond and the Su-27's, and the Su-27 is
    the one nearer the crosshair."""
    view = _aim_twice(_need("ready_su27_and_fgr4_024050.jpg"))
    assert view is not None
    assert abs(view.diamond.x - 946) <= 3 and abs(view.diamond.y - 562) <= 3


@pytest.mark.parametrize("name,at", [("ready_one_diamond_023941.jpg", (950, 523)),
                                     ("ready_one_diamond_2_024123.jpg", (732, 595))])
def test_a_lone_diamond_is_the_target(name, at):
    view = _aim_twice(_need(name))
    assert view is not None
    assert abs(view.diamond.x - at[0]) <= 3 and abs(view.diamond.y - at[1]) <= 3


def test_no_diamond_is_no_aim():
    assert _aim_twice(_need("ready_no_diamond_024121.jpg")) is None


def test_one_picture_is_not_enough():
    a = AimedAbility(_SW, _CROSSHAIR)
    assert a.view(_need("ready_su27_and_fgr4_024050.jpg"), 1.0, _NUKE, True) is None


def test_nothing_is_searched_while_the_icon_is_not_ready():
    frame = _need("ready_su27_and_fgr4_024050.jpg")
    a = AimedAbility(_SW, _CROSSHAIR)
    for ts, ready in ((1.0, False), (1.1, False), (1.2, None)):
        assert a.view(frame, ts, _NUKE, ready) is None


def test_the_aim_is_the_diamonds_error_against_the_crosshair():
    view = _aim_twice(_need("ready_su27_and_fgr4_024050.jpg"))
    cx, cy = _CROSSHAIR[0] * 1920, _CROSSHAIR[1] * 1200
    assert view.err == pytest.approx((view.diamond.x - cx) / 960, abs=1e-6)
    assert view.err_y == pytest.approx((view.diamond.y - cy) / 600, abs=1e-6)


def _yellow_frame(at):
    frame = np.full((1200, 1920, 3), (90, 60, 30), np.uint8)
    m = diamond_template(1.0)
    h = m.shape[0] // 2
    roi = frame[at[1] - h:at[1] - h + m.shape[0], at[0] - h:at[0] - h + m.shape[1]]
    roi[m > 0] = (60, 215, 245)       # BGR yellow, as the HUD draws it
    return frame


def test_a_green_lead_is_not_the_nukes():
    """The rocket pod's lead and the missile box are green; the nuke's is yellow."""
    frame = _yellow_frame((900, 560))
    frame[frame[..., 1] == 215] = (140, 235, 160)
    assert _aim_twice(frame) is None


# -- the press -------------------------------------------------------------------------

def _view(px):
    return AimView(Diamond(960.0, 610.0, 0.8), 0.0, 0.0, float(px))


def test_q_goes_out_once_the_diamond_is_on_the_crosshair():
    a = AimedAbility(_SW, _CROSSHAIR)
    fire_px = _SW["aimed"]["fire_px"]
    assert a.press_now(10.0, True, _view(fire_px + 30)) is False
    assert a.press_now(10.2, True, None) is False
    assert a.press_now(10.4, True, _view(fire_px - 5)) is True


def test_no_press_without_a_ready_icon():
    a = AimedAbility(_SW, _CROSSHAIR)
    assert a.press_now(10.0, False, _view(5)) is False
    assert a.press_now(10.1, None, _view(5)) is False


def test_an_unanswered_press_is_repeated_once_after_the_retry():
    a = AimedAbility(_SW, _CROSSHAIR)
    retry = _SW["press_retry_s"]
    assert a.press_now(10.0, True, _view(5)) is True
    assert a.press_now(10.0 + retry / 2, True, _view(5)) is False
    assert a.press_now(10.0 + retry + 0.1, True, _view(5)) is True
    assert a.press_now(10.0 + 3 * retry, True, _view(5)) is False, "max_presses"


# -- one ability, any jet --------------------------------------------------------------

def test_the_trigger_lists_agree():
    assert TRIGGERS == SPECIAL_WEAPON_TRIGGERS


def test_any_jet_drawing_the_nuke_glyph_gets_the_nuke_rule():
    profile = resolve_profile(None, "nuke", _SW)
    assert profile["ability"] == "nuke" and AimedAbility.applies(profile)
    assert profile["lead"] == "yellow" and "glyph" not in profile


def test_a_jets_own_profile_wins_over_its_glyph():
    mig23 = {"ability": "flare_burst", "trigger": "incoming"}
    assert resolve_profile(mig23, "nuke", _SW) == mig23


def test_the_battle_tick_leaves_the_nuke_to_the_pursuit():
    from tests.test_special_weapon import _Analyzer, _handler

    class _NukeAnalyzer(_Analyzer):
        def special_weapon_glyph(self):
            return "nuke"

    h, ctrl, _ = _handler(None, _NukeAnalyzer())
    for _ in range(3):
        h.tick_events()
    assert ctrl.keys == [], "no `q` on a READY icon alone: the nuke needs its diamond"


# -- the Controller: precedence and the press ------------------------------------------

class _Fake(PerceptionFake):
    game_state = GameState.GAME_BATTLE_EJECT

    def __init__(self, glyph="nuke", ready=True):
        self.glyph, self.ready = glyph, ready

    def special_weapon_glyph(self):
        return self.glyph

    def special_weapon_ready(self):
        return self.ready


def _ctrl(monkeypatch, analyzer):
    monkeypatch.setattr(controller_module, "keyboard_module", None)
    return Controller((0, 0, 1920, 1200), analyzer=analyzer, exit_event=threading.Event(),
                      capture=None,
                      config=ControllerConfig(simulate_os_input=True, disable_hotkeys=True,
                                              special_weapon=_SW))


def test_the_ability_aim_comes_before_the_rocket_and_the_ring(monkeypatch):
    ctrl = _ctrl(monkeypatch, _Fake())
    monkeypatch.setattr(ctrl, "_ability_aim", lambda f, ts: (0.1, 0.2))
    monkeypatch.setattr(ctrl, "_rocket_view", lambda f, o, ts: (0.3, 0.4))
    monkeypatch.setattr(ctrl, "_ring_aim", lambda f, v, ts: (0.5, 0.6))
    assert ctrl._pursuit_aim(None, {}, True, 1.0) == ((0.1, 0.2), "ability")
    monkeypatch.setattr(ctrl, "_ability_aim", lambda f, ts: None)
    assert ctrl._pursuit_aim(None, {}, True, 1.0) == ((0.3, 0.4), "rocket")
    monkeypatch.setattr(ctrl, "_rocket_view", lambda f, o, ts: None)
    assert ctrl._pursuit_aim(None, {}, True, 1.0) == ((0.5, 0.6), "ring")
    monkeypatch.setattr(ctrl, "_ring_aim", lambda f, v, ts: None)
    assert ctrl._pursuit_aim(None, {}, True, 1.0) == (None, None)


def test_every_aim_source_sees_every_picture(monkeypatch):
    """Each keeps its own two-picture confirmation, so a winner ahead of it must
    not starve it of pictures."""
    ctrl = _ctrl(monkeypatch, _Fake())
    seen = []
    monkeypatch.setattr(ctrl, "_ability_aim", lambda f, ts: seen.append("a") or (0.1, 0.1))
    monkeypatch.setattr(ctrl, "_rocket_view", lambda f, o, ts: seen.append("r"))
    monkeypatch.setattr(ctrl, "_ring_aim", lambda f, v, ts: seen.append("g"))
    ctrl._pursuit_aim(None, {}, True, 1.0)
    assert seen == ["a", "r", "g"]


def _presses(ctrl):
    return [i for i in ctrl.get_action_intents()
            if i["action_type"] == "key_press" and i["key"] == SPECIAL_ABILITY]


def test_the_pursuit_presses_q_with_the_diamond_on_the_crosshair(monkeypatch):
    fake = _Fake()
    ctrl = _ctrl(monkeypatch, fake)
    frame = _yellow_frame((962, 612))
    assert ctrl._ability_aim(frame, 1.0) is None, "one picture is not enough"
    assert ctrl._pursuit_ability_tick() is False
    assert ctrl._ability_aim(frame, 1.1) is not None
    assert ctrl._pursuit_ability_tick() is True
    assert len(_presses(ctrl)) == 1
    assert fake.special_weapon_presses == 1


def test_the_pursuit_steers_to_a_diamond_off_the_crosshair_without_pressing(monkeypatch):
    ctrl = _ctrl(monkeypatch, _Fake())
    frame = _yellow_frame((1200, 450))
    for ts in (1.0, 1.1):
        aim = ctrl._ability_aim(frame, ts)
    assert aim is not None and aim[0] > 0 and aim[1] < 0
    assert ctrl._pursuit_ability_tick() is False
    assert _presses(ctrl) == []


@pytest.mark.parametrize("glyph,ready", [("refuel", True), ("other", True), ("nuke", False)])
def test_no_aim_and_no_press_for_another_ability_or_an_unlit_icon(monkeypatch, glyph, ready):
    ctrl = _ctrl(monkeypatch, _Fake(glyph=glyph, ready=ready))
    frame = _yellow_frame((962, 612))
    for ts in (1.0, 1.1):
        assert ctrl._ability_aim(frame, ts) is None
    assert ctrl._pursuit_ability_tick() is False
    assert _presses(ctrl) == []
