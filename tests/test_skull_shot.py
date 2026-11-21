"""The F-20's skull ability, pressed at any charge level (ADR 199).

Operator, 2026-11-20: "for that specific ability it should be used anytime there is
a charge level". Its ring fills over about 90 s and starts over each life; lives
lasted 20-45 s, so the ring never read READY and `q` never went out.

tests/fixtures/skull_shot/charging_083026.jpg is the operator's `v` screenshot of
08:30:26 (the ring about a third lit). tests/fixtures is gitignored, so the test
that needs it skips on a machine without it.
"""

from __future__ import annotations

import threading
from pathlib import Path

import cv2
import pytest
import yaml

from tests.test_special_weapon import _Ctrl, _Stats
from wingman.crop_region import load_crops
from wingman.special_weapon import (
    RingGeometry,
    SpecialWeaponReader,
    glyph_iou,
    glyph_mask,
    load_glyph_template,
    resolve_profile,
    ring_sectors,
)
from wingman.state import GameState

_ROOT = Path(__file__).parents[1]
_CFG = yaml.safe_load((_ROOT / "wingman" / "config.yaml").read_text(encoding="utf-8"))
_SW = _CFG["special_weapon"]
_FIX = Path(__file__).parent / "fixtures" / "skull_shot" / "charging_083026.jpg"


class _Analyzer:
    game_state = GameState.GAME_BATTLE

    def __init__(self, charge, ready=False):
        self.charge = charge
        self.ready = ready
        self.low_flares_event = threading.Event()
        self.no_missiles_event = threading.Event()

    def special_weapon_charge(self):
        return self.charge

    def special_weapon_ready(self):
        return self.ready

    def special_weapon_glyph(self):
        return "skull_shot"

    def get_ammo_flares(self):
        return 10

    def get_incoming_cache_result(self):
        return (False, None, None)

    def get_incoming_cache_timestamp(self):
        return 0.0

    def note_special_weapon_press(self):
        pass


def _handler(analyzer):
    from wingman.tick_handlers import AmmoEventsHandler

    ctrl = _Ctrl(None)
    h = AmmoEventsHandler(analyzer, ctrl, {}, stats_tracker=_Stats(),
                          special_weapon_cfg=_SW)
    return h, ctrl


def test_the_row_presses_it_on_any_charge():
    assert resolve_profile(None, "skull_shot", _SW) == {
        "ability": "skull_shot", "trigger": "charged", "charge_at": 1}


def test_one_lit_sector_presses_it_with_the_icon_not_ready():
    h, ctrl = _handler(_Analyzer(charge=1, ready=False))
    h.tick_events()
    assert ctrl.keys == ["q"]


def test_an_empty_ring_does_not_press_it():
    h, ctrl = _handler(_Analyzer(charge=0))
    h.tick_events()
    assert ctrl.keys == []


def test_no_fresh_read_does_not_press_it():
    h, ctrl = _handler(_Analyzer(charge=None))
    h.tick_events()
    assert ctrl.keys == []


def test_a_charge_that_stays_is_pressed_again_after_the_retry_not_every_tick():
    h, ctrl = _handler(_Analyzer(charge=6))
    for _ in range(5):
        h.tick_events()
    assert ctrl.keys == ["q"], "the retry waits press_retry_s"


def test_the_log_says_charged(caplog):
    h, _ctrl = _handler(_Analyzer(charge=3))
    with caplog.at_level("INFO", logger="wingman.tick_handlers"):
        h.tick_events()
    assert "Special weapon skull_shot: trigger charged, icon charged" in caplog.text


def test_the_reader_reports_the_charge_and_forgets_it():
    now = [100.0]
    reader = SpecialWeaponReader(None, {}, clock=lambda: now[0])
    assert reader.charge() is None
    reader.note_lit(6)
    assert reader.charge() == 6
    assert reader.ready() is False
    now[0] += 10.0
    assert reader.charge() is None, "a stale read is no charge"
    reader.note_lit(4)
    reader.reset()
    assert reader.charge() is None


def test_the_screenshot_names_the_glyph_and_reads_a_partial_charge():
    if not _FIX.exists():
        pytest.skip("fixture not on this machine (tests/fixtures is untracked)")
    path = _ROOT / _SW["abilities"]["skull_shot"]["glyph"]
    if not path.exists():
        pytest.skip("glyph crops not on this machine (test_screenshots is untracked)")
    box = load_crops(_CFG["crops"])["SPECIAL_WEAPON"][:4]
    frame = cv2.imread(str(_FIX))
    g = RingGeometry(box, frame.shape, 16)
    assert glyph_iou(glyph_mask(frame, g.cx, g.cy, g.radius),
                     load_glyph_template(str(path), box)) >= _SW["glyph_min_iou"]
    lit, _covered = ring_sectors(frame, g)
    assert 1 <= lit < _SW.get("min_lit_sectors", 14), lit
