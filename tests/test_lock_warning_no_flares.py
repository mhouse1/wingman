"""ADR 201 (operator, 2026-11-20): "flares should not deploy on lock warning, it
should only deploy on INCOMING detected". LOCK WARNING still raises the alert that
starts and holds the flare roll (ADR 184); only INCOMING drops flares."""

import threading
import time

from wingman.analyzer import incoming_debounced, is_lock_warning_only
from wingman.state import GameState


def test_lock_warning_text_is_lock_warning_only():
    assert is_lock_warning_only("ocr_fallback", "LOCKWARNING")
    assert is_lock_warning_only("ocr_fallback", "OCKWARNIN")


def test_incoming_text_and_the_template_are_incoming():
    assert not is_lock_warning_only("ocr_fallback", "INCOMING")
    assert not is_lock_warning_only("ocr_fallback", "INCOMING_")
    assert not is_lock_warning_only("template", None)
    assert not is_lock_warning_only("none", None)


def test_incoming_right_after_a_lock_warning_is_not_debounced():
    assert incoming_debounced(10.2, 10.0, 0.5, last_was_lock_warning=False,
                              lock_warning=False)
    assert incoming_debounced(10.2, 10.0, 0.5, last_was_lock_warning=True,
                              lock_warning=True)
    assert not incoming_debounced(10.2, 10.0, 0.5, last_was_lock_warning=True,
                                  lock_warning=False)
    assert not incoming_debounced(10.6, 10.0, 0.5, last_was_lock_warning=False,
                                  lock_warning=False)


class _Analyzer:
    game_state = GameState.GAME_BATTLE

    def __init__(self, lock_warning):
        self.lock_warning = lock_warning
        self.incoming_ts = 0.0
        self.low_flares_event = threading.Event()
        self.no_missiles_event = threading.Event()

    def get_incoming_cache_result(self):
        return (self.incoming_ts > 0, 1.0, "ocr_fallback")

    def get_incoming_cache_timestamp(self):
        return self.incoming_ts

    def incoming_is_lock_warning(self):
        return self.lock_warning

    def get_ammo_missiles(self):
        return 4

    def get_ammo_missiles_read_seq(self):
        return 0

    def get_ammo_flares(self):
        return 10

    def special_weapon_glyph(self):
        return None

    def special_weapon_ready(self):
        return None


class _Ctrl:
    def __init__(self):
        self.flares = 0
        self.rolls = 0
        self.lock = threading.Lock()

    def flares_wait_for_special_grey(self):
        return False

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


def _alert(lock_warning):
    from wingman.tick_handlers import AmmoEventsHandler

    analyzer, ctrl = _Analyzer(lock_warning), _Ctrl()
    h = AmmoEventsHandler(analyzer, ctrl, {})
    analyzer.incoming_ts = 100.0
    assert h.deploy_flares_on_new_incoming() is True
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline and ctrl.flares < 3:
        time.sleep(0.05)
    time.sleep(0.1)
    return ctrl


def test_lock_warning_rolls_and_drops_no_flares(caplog):
    with caplog.at_level("INFO", logger="wingman.tick_handlers"):
        ctrl = _alert(lock_warning=True)
    assert ctrl.flares == 0
    assert ctrl.rolls == 1, "LOCK WARNING still starts the flare roll (ADR 184)"
    assert "flares held: LOCK WARNING, not INCOMING (ADR 201)" in caplog.text


def test_incoming_drops_the_burst():
    ctrl = _alert(lock_warning=False)
    assert ctrl.flares == 3
    assert ctrl.rolls == 1
