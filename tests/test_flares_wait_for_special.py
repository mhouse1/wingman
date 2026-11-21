"""ADR 198 (operator, 2026-11-20): "flares only deploy when the kf21 special is
greyed out". On a jet with `flares_only_when_special_grey`, an incoming alert
drops no flares while the special weapon's icon reads READY."""

import threading
import time
from pathlib import Path

import yaml

from wingman.jet_name import profile_for_jet
from wingman.state import GameState

_ROOT = Path(__file__).parents[1]


class _Analyzer:
    game_state = GameState.GAME_BATTLE

    def __init__(self, ready):
        self.ready = ready
        self.incoming_ts = 0.0
        self.low_flares_event = threading.Event()
        self.no_missiles_event = threading.Event()

    def get_incoming_cache_result(self):
        return (self.incoming_ts > 0, None, None)

    def get_incoming_cache_timestamp(self):
        return self.incoming_ts

    def get_ammo_missiles(self):
        return 4

    def get_ammo_missiles_read_seq(self):
        return 0

    def get_ammo_flares(self):
        return 10

    def special_weapon_glyph(self):
        return None

    def special_weapon_ready(self):
        return self.ready


class _Ctrl:
    def __init__(self, waits):
        self.waits = waits
        self.flares = 0
        self.rolls = 0
        self.lock = threading.Lock()

    def flares_wait_for_special_grey(self):
        return self.waits

    def special_weapon_profile(self):
        return None

    def auxiliary_feature_profile(self):
        return None

    def deploy_flares(self, **kw):
        with self.lock:
            self.flares += 1

    def flare_roll(self, flares_done):
        self.rolls += 1
        return True

    def pursuit_evade_on_alert(self, ts):
        return (None, False)


def _alert(waits, ready):
    from wingman.tick_handlers import AmmoEventsHandler

    analyzer, ctrl = _Analyzer(ready), _Ctrl(waits)
    h = AmmoEventsHandler(analyzer, ctrl, {})
    analyzer.incoming_ts = 100.0
    assert h.deploy_flares_on_new_incoming() is True
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline and ctrl.flares < 3:
        time.sleep(0.05)
    time.sleep(0.1)
    return ctrl


def test_no_flares_while_the_special_is_lit():
    ctrl = _alert(waits=True, ready=True)
    assert ctrl.flares == 0
    assert ctrl.rolls == 1, "the roll still answers the alert"


def test_flares_go_out_once_the_special_is_greyed_out():
    assert _alert(waits=True, ready=False).flares == 3


def test_flares_go_out_with_no_read_of_the_icon():
    assert _alert(waits=True, ready=None).flares == 3


def test_any_other_jet_drops_flares_with_the_special_lit():
    assert _alert(waits=False, ready=True).flares == 3


def test_the_log_names_the_held_flares(caplog):
    with caplog.at_level("INFO", logger="wingman.tick_handlers"):
        _alert(waits=True, ready=True)
    assert "flares held: the special weapon is lit (ADR 198)" in caplog.text


def test_the_shipped_kf21_profile_waits_for_the_grey_icon():
    cfg = yaml.safe_load((_ROOT / "wingman" / "config.yaml").read_text(encoding="utf-8"))
    profiles = cfg["jet_profile"]["profiles"]
    assert profile_for_jet("KF-21 BORAMAE", profiles)["flares_only_when_special_grey"] is True
    assert not profile_for_jet("MIG-23", profiles).get("flares_only_when_special_grey")
