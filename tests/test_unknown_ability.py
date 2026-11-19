"""Phantom missiles, and any special weapon no row knows, pressed whenever lit (ADR 190).

Operator, 2026-11-18: "the special weapon for phantom missiles should be activated
whenever it appears, implement it such that anytime an undefined special weapon
appears activate it automatically so this would make the ACS system more adaptable
for new unknown abilities".

The fixtures in tests/fixtures/phantom_missiles/ are the operator's three `v`
screenshots of 07:31-07:33 (JPEG of the saved PNGs). tests/fixtures is gitignored,
so the tests that need a picture skip on a machine without them.
"""

from __future__ import annotations

from pathlib import Path

import cv2
import pytest
import yaml

from tests.test_special_weapon import _Analyzer, _handler
from wingman.crop_region import load_crops
from wingman.special_weapon import (
    RingGeometry,
    SpecialWeaponReader,
    ability_profile,
    glyph_iou,
    glyph_mask,
    load_glyph_template,
    resolve_profile,
)

_ROOT = Path(__file__).parents[1]
_FIX = Path(__file__).parent / "fixtures" / "phantom_missiles"
_GLYPHS = _ROOT / "test_screenshots" / "special_weapon" / "glyphs"
_CFG = yaml.safe_load((_ROOT / "wingman" / "config.yaml").read_text(encoding="utf-8"))
_SW = _CFG["special_weapon"]


def _need(name):
    path = _FIX / name
    if not path.exists():
        pytest.skip(f"fixture {name} not on this machine (tests/fixtures is untracked)")
    return cv2.imread(str(path))


def _box():
    return load_crops(_CFG["crops"])["SPECIAL_WEAPON"][:4]


def _template():
    path = _ROOT / _SW["abilities"]["phantom_missiles"]["glyph"]
    if not path.exists():
        pytest.skip("glyph crops not on this machine (test_screenshots is untracked)")
    return load_glyph_template(str(path), _box())


def _score(frame):
    g = RingGeometry(_box(), frame.shape, 16)
    return glyph_iou(glyph_mask(frame, g.cx, g.cy, g.radius), _template())


class _Glyph(_Analyzer):
    def __init__(self, glyph, **kw):
        super().__init__(**kw)
        self.glyph = glyph

    def special_weapon_glyph(self):
        return self.glyph


def _cfg(**over):
    return {"press_retry_s": 3.0, "max_presses": 2,
            "abilities": _SW["abilities"], **over}


# -- the phantom missiles glyph ------------------------------------------------------

@pytest.mark.parametrize("name", ["ready_073143.jpg", "ready_073256.jpg"])
def test_the_phantom_missiles_glyph_is_recognised(name):
    assert _score(_need(name)) >= _SW["glyph_min_iou"]


def test_the_countdown_is_not_the_glyph():
    assert _score(_need("countdown_073300.jpg")) < _SW["glyph_min_iou"]


@pytest.mark.parametrize("other", sorted(
    p.name for p in _GLYPHS.glob("*.png") if p.name != "phantom_missiles.png"))
def test_no_other_glyph_reads_as_phantom_missiles(other):
    other_mask = load_glyph_template(str(_GLYPHS / other), _box())
    assert glyph_iou(other_mask, _template()) < _SW["glyph_min_iou"]


def test_the_reader_names_phantom_missiles_on_two_lit_reads():
    if not (_ROOT / _SW["abilities"]["phantom_missiles"]["glyph"]).exists():
        pytest.skip("glyph crops not on this machine (test_screenshots is untracked)")
    reader = SpecialWeaponReader(_box(), _SW)
    frame = _need("ready_073143.jpg")
    reader.update(frame)
    reader.update(frame)
    assert reader.glyph() == "phantom_missiles"
    assert reader.ready() is True


def test_phantom_missiles_press_q_whenever_ready():
    h, ctrl, stats = _handler(None, _Glyph("phantom_missiles"))
    h.tick_events()
    assert ctrl.keys == ["q"]
    assert "special_weapon_used" in stats.events


# -- any glyph no row knows -----------------------------------------------------------

def test_an_unknown_glyph_gets_the_unknown_trigger():
    assert ability_profile("other", _SW) == {"ability": "unknown", "trigger": "ready"}


def test_the_shipped_unknown_trigger_is_ready():
    assert _SW["unknown_trigger"] == "ready"


def test_unknown_trigger_none_leaves_an_unknown_glyph_alone():
    assert ability_profile("other", {"unknown_trigger": "none"}) is None


def test_an_aimed_trigger_is_never_the_unknown_rule():
    # lead_aligned needs a lead colour from its row; an unknown glyph has none.
    assert ability_profile("other", {"unknown_trigger": "lead_aligned"}) is None


def test_a_jets_own_profile_wins_over_an_unknown_glyph():
    mig23 = {"ability": "flare_burst", "trigger": "incoming"}
    assert resolve_profile(mig23, "other", _SW) == mig23


def test_an_unread_glyph_is_not_unknown():
    # None: no two reads agree yet, or the ring is dim. Nothing is pressed.
    assert resolve_profile(None, None, _SW) is None


def test_the_battle_tick_presses_an_unknown_ability_whenever_ready():
    from wingman.tick_handlers import AmmoEventsHandler

    from tests.test_special_weapon import _Ctrl, _Stats

    ctrl, stats = _Ctrl(None), _Stats()
    h = AmmoEventsHandler(_Glyph("other"), ctrl, {}, stats_tracker=stats,
                          special_weapon_cfg=_cfg(unknown_trigger="ready"))
    h.tick_events()
    assert ctrl.keys == ["q"]


def test_no_press_for_an_unknown_ability_while_the_icon_counts_down():
    h, ctrl, _ = _handler(None, _Glyph("other", ready=False))
    for _ in range(3):
        h.tick_events()
    assert ctrl.keys == []


def test_no_press_for_an_unknown_ability_when_turned_off():
    from wingman.tick_handlers import AmmoEventsHandler

    from tests.test_special_weapon import _Ctrl, _Stats

    ctrl = _Ctrl(None)
    h = AmmoEventsHandler(_Glyph("other"), ctrl, {}, stats_tracker=_Stats(),
                          special_weapon_cfg=_cfg(unknown_trigger="none"))
    h.tick_events()
    assert ctrl.keys == []
