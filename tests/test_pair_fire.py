"""Pair firing, a special ability pressed whenever it is available (ADR 188).

Operator, 2026-11-17: "implement new special ability for pair firing, it should
press the 'q' key and activate whenever available". It is one row in
`special_weapon.abilities` (glyph pair_fire.png, trigger ready), so any jet whose
icon draws the glyph gets it.

The fixtures in tests/fixtures/pair_fire/ are the operator's six `v` screenshots of
21:07-21:19 (JPEG of the saved PNGs). tests/fixtures is gitignored, so the tests
that need a picture skip on a machine without them.
"""

from __future__ import annotations

from pathlib import Path

import cv2
import pytest
import yaml

from wingman.crop_region import load_crops
from wingman.special_weapon import (
    RingGeometry,
    glyph_iou,
    glyph_mask,
    load_glyph_template,
    resolve_profile,
    ring_sectors,
)

_ROOT = Path(__file__).parents[1]
_FIX = Path(__file__).parent / "fixtures" / "pair_fire"
_GLYPHS = _ROOT / "test_screenshots" / "special_weapon" / "glyphs"
_CFG = yaml.safe_load((_ROOT / "wingman" / "config.yaml").read_text(encoding="utf-8"))
_SW = _CFG["special_weapon"]
# The glyph drawn: ring lit (two), and lit on a snow background (one).
_GLYPH_DRAWN = ["ready_210714.jpg", "ready_210722.jpg", "ready_on_snow_211903.jpg"]
# The countdown after a press draws a digit over the glyph; the dim icon greys it.
_GLYPH_HIDDEN = ["countdown_210725.jpg", "countdown_211935.jpg", "dim_210717.jpg"]


def _need(name):
    path = _FIX / name
    if not path.exists():
        pytest.skip(f"fixture {name} not on this machine (tests/fixtures is untracked)")
    return cv2.imread(str(path))


def _box():
    return load_crops(_CFG["crops"])["SPECIAL_WEAPON"][:4]


def _template():
    path = _ROOT / _SW["abilities"]["pair_fire"]["glyph"]
    if not path.exists():
        pytest.skip("glyph crops not on this machine (test_screenshots is untracked)")
    return load_glyph_template(str(path), _box())


def _score(frame):
    g = RingGeometry(_box(), frame.shape, 16)
    return glyph_iou(glyph_mask(frame, g.cx, g.cy, g.radius), _template())


@pytest.mark.parametrize("name", _GLYPH_DRAWN)
def test_the_pair_fire_glyph_is_recognised(name):
    assert _score(_need(name)) >= _SW["glyph_min_iou"]


@pytest.mark.parametrize("name", _GLYPH_HIDDEN)
def test_the_countdown_and_the_dim_icon_are_not_the_glyph(name):
    assert _score(_need(name)) < _SW["glyph_min_iou"]


@pytest.mark.parametrize("other", sorted(
    p.name for p in _GLYPHS.glob("*.png") if p.name != "pair_fire.png"))
def test_no_other_glyph_reads_as_pair_fire(other):
    other_mask = load_glyph_template(str(_GLYPHS / other), _box())
    assert glyph_iou(other_mask, _template()) < _SW["glyph_min_iou"]


def test_the_ring_is_full_on_the_ready_screenshots():
    for name in ("ready_210714.jpg", "ready_210722.jpg"):
        frame = _need(name)
        g = RingGeometry(_box(), frame.shape, 16)
        lit, _ = ring_sectors(frame, g)
        assert lit >= _SW.get("min_lit_sectors", 14), name


def test_any_jet_drawing_the_glyph_presses_whenever_ready():
    profile = resolve_profile(None, "pair_fire", _SW)
    assert profile == {"ability": "pair_fire", "trigger": "ready"}


def test_the_battle_tick_presses_q_on_a_ready_icon():
    from tests.test_special_weapon import _Analyzer, _handler

    class _PairFire(_Analyzer):
        def special_weapon_glyph(self):
            return "pair_fire"

    h, ctrl, stats = _handler(None, _PairFire())
    h.tick_events()
    assert ctrl.keys == ["q"]
    assert "special_weapon_used" in stats.events


def test_no_press_while_the_icon_counts_down():
    from tests.test_special_weapon import _Analyzer, _handler

    class _PairFire(_Analyzer):
        def special_weapon_glyph(self):
            return "pair_fire"

    h, ctrl, _ = _handler(None, _PairFire(ready=False))
    for _ in range(3):
        h.tick_events()
    assert ctrl.keys == []
