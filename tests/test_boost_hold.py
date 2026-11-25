"""The MiG-21's boost, `q` held until the fuel reads 0% (ADR 203).

Operator, 2026-11-24, on the `v` screenshot of 17:55:10 (a double chevron pointing
up): "activation requires holding the 'q' button, it should be held until fuel
reports 0%". Under unknown_trigger it was tapped twice and the icon stayed READY.
"""

from __future__ import annotations

import threading
import time
import types
from pathlib import Path

import cv2
import pytest
import yaml

import wingman.controller as controller_module
from tests.test_special_weapon import _Ctrl, _Stats
from wingman.controller import Controller
from wingman.controller_config import ControllerConfig
from wingman.crop_region import load_crops
from wingman.special_weapon import (
    FuelEmptyWatch,
    RingGeometry,
    glyph_iou,
    glyph_mask,
    load_glyph_template,
    resolve_profile,
)
from wingman.state import GameState

_ROOT = Path(__file__).parents[1]
_CFG = yaml.safe_load((_ROOT / "wingman" / "config.yaml").read_text(encoding="utf-8"))
_SW = _CFG["special_weapon"]
_SHOT = _ROOT / "tests" / "fixtures" / "boost_hold" / "ready_175510.png"


# --- the row and the fuel watch -----------------------------------------------

def test_the_row_holds_it_until_the_fuel_is_empty():
    assert resolve_profile(None, "boost_hold", _SW) == {
        "ability": "boost_hold", "trigger": "hold_to_empty", "fuel_at": 0}


def test_the_watch_lets_go_on_two_empty_readings_counted_per_reading():
    watch = FuelEmptyWatch(0)
    assert watch.observe(40, 1.0) is False
    assert watch.observe(0, 2.0) is False
    assert watch.observe(0, 2.0) is False, "the same reading counts once"
    assert watch.observe(0, 3.0) is True


def test_one_misread_zero_does_not_end_the_hold():
    watch = FuelEmptyWatch(0)
    assert watch.observe(0, 1.0) is False
    assert watch.observe(35, 2.0) is False
    assert watch.observe(0, 3.0) is False
    assert watch.observe(None, 0.0) is False, "an unread gauge is no reading"


# --- the tick handler ---------------------------------------------------------

class _Analyzer:
    game_state = GameState.GAME_BATTLE

    def __init__(self, fuel, ready=True):
        self.fuel = fuel
        self.ts = 1.0
        self.ready = ready
        self.low_flares_event = threading.Event()
        self.no_missiles_event = threading.Event()

    def get_afterburner_fuel_reading(self):
        return (None, 0.0) if self.fuel is None else (self.fuel, self.ts)

    def special_weapon_ready(self):
        return self.ready

    def special_weapon_glyph(self):
        return "boost_hold"

    def get_ammo_flares(self):
        return 10

    def get_incoming_cache_result(self):
        return (False, None, None)

    def get_incoming_cache_timestamp(self):
        return 0.0

    def note_special_weapon_press(self):
        pass


class _HoldCtrl(_Ctrl):
    def __init__(self):
        super().__init__(None)
        self.holds = []
        self.active = False

    def special_hold_active(self):
        return self.active

    def hold_special_weapon(self, until, max_s, label=""):
        self.holds.append((until, max_s, label))
        return True


def _handler(analyzer):
    from wingman.tick_handlers import AmmoEventsHandler

    ctrl = _HoldCtrl()
    h = AmmoEventsHandler(analyzer, ctrl, {}, stats_tracker=_Stats(),
                          special_weapon_cfg=_SW)
    return h, ctrl


def test_ready_with_fuel_holds_q_rather_than_tapping_it():
    h, ctrl = _handler(_Analyzer(fuel=100))
    h.tick_events()
    assert ctrl.keys == [], "no tap"
    assert len(ctrl.holds) == 1
    _until, max_s, label = ctrl.holds[0]
    assert max_s == _SW["hold_max_s"]
    assert label == "special_weapon_boost_hold"


def test_no_hold_with_the_fuel_empty_or_unread():
    for fuel in (0, None):
        h, ctrl = _handler(_Analyzer(fuel=fuel))
        h.tick_events()
        assert ctrl.holds == [] and ctrl.keys == [], fuel


def test_no_hold_with_the_icon_not_ready():
    h, ctrl = _handler(_Analyzer(fuel=100, ready=False))
    h.tick_events()
    assert ctrl.holds == []


def test_no_second_hold_while_one_is_held():
    h, ctrl = _handler(_Analyzer(fuel=100))
    ctrl.active = True
    h.tick_events()
    assert ctrl.holds == []


def test_the_hold_ends_when_the_fuel_reads_zero_twice():
    analyzer = _Analyzer(fuel=100)
    h, ctrl = _handler(analyzer)
    h.tick_events()
    until = ctrl.holds[0][0]
    assert until() is None
    analyzer.fuel, analyzer.ts = 0, 2.0
    assert until() is None
    analyzer.ts = 3.0
    assert until() == "fuel 0%"


# --- the controller's hold ----------------------------------------------------

def _real_ctrl(monkeypatch, state=GameState.GAME_BATTLE):
    monkeypatch.setattr(controller_module, "keyboard_module", None)
    return Controller(
        (0, 0, 1920, 1200),
        analyzer=types.SimpleNamespace(game_state=state),
        exit_event=threading.Event(),
        config=ControllerConfig(simulate_os_input=True, disable_hotkeys=True),
    )


def _q_intents(ctrl):
    return [i["action_type"] for i in ctrl.get_action_intents() if i.get("key") == "q"]


def _wait_done(ctrl, budget=3.0):
    deadline = time.time() + budget
    while ctrl.special_hold_active() and time.time() < deadline:
        time.sleep(0.01)
    assert not ctrl.special_hold_active()


def test_the_controller_holds_q_until_told_to_let_go(monkeypatch, caplog):
    ctrl = _real_ctrl(monkeypatch)
    calls = []

    def until():
        calls.append(1)
        return "fuel 0%" if len(calls) >= 4 else None

    with caplog.at_level("INFO", logger="wingman.controller"):
        assert ctrl.hold_special_weapon(until, 5.0) is True
        assert ctrl.hold_special_weapon(until, 5.0) is False, "one hold at a time"
        _wait_done(ctrl)
    assert _q_intents(ctrl) == ["key_press", "key_release"]
    assert "Special weapon released (fuel 0%" in caplog.text


def test_the_hold_lets_go_at_its_cap(monkeypatch, caplog):
    ctrl = _real_ctrl(monkeypatch)
    with caplog.at_level("INFO", logger="wingman.controller"):
        ctrl.hold_special_weapon(lambda: None, 0.2)
        _wait_done(ctrl)
    assert _q_intents(ctrl) == ["key_press", "key_release"]
    assert "Special weapon released (cap" in caplog.text


def test_the_hold_lets_go_when_the_battle_ends(monkeypatch, caplog):
    ctrl = _real_ctrl(monkeypatch)
    with caplog.at_level("INFO", logger="wingman.controller"):
        ctrl.hold_special_weapon(lambda: None, 5.0)
        ctrl._analyzer.game_state = GameState.GAME_END_B
        _wait_done(ctrl)
    assert "Special weapon released (not in battle" in caplog.text


def test_takeover_stops_the_hold(monkeypatch):
    ctrl = _real_ctrl(monkeypatch)
    ctrl.hold_special_weapon(lambda: None, 5.0)
    ctrl.release_for_manual_takeover()
    _wait_done(ctrl)
    assert _q_intents(ctrl) == ["key_press", "key_release"]


def test_no_hold_starts_outside_battle_or_in_takeover(monkeypatch):
    for state in (GameState.GAME_LOBBY, GameState.GAME_BATTLE_MANUAL):
        ctrl = _real_ctrl(monkeypatch, state)
        assert ctrl.hold_special_weapon(lambda: None, 5.0) is False
        assert _q_intents(ctrl) == []


# --- the glyph ----------------------------------------------------------------

def test_the_screenshot_names_the_glyph():
    path = _ROOT / _SW["abilities"]["boost_hold"]["glyph"]
    if not _SHOT.exists() or not path.exists():
        pytest.skip("screenshot or glyph crop not on this machine (both untracked)")
    box = load_crops(_CFG["crops"])["SPECIAL_WEAPON"][:4]
    frame = cv2.imread(str(_SHOT))
    g = RingGeometry(box, frame.shape, 16)
    template = load_glyph_template(str(path), box)
    assert glyph_iou(glyph_mask(frame, g.cx, g.cy, g.radius), template) >= _SW["glyph_min_iou"]
    for other, row in _SW["abilities"].items():
        if other == "boost_hold":
            continue
        other_t = load_glyph_template(str(_ROOT / row["glyph"]), box)
        if other_t is not None:
            assert glyph_iou(other_t, template) < _SW["glyph_min_iou"], other
