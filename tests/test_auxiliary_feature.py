"""The jet's auxiliary feature on `w` (was the wing sweep), operator 2026-11-16:
the KF-21 presses it once a life when its missile count reaches 3 or lower, or
on the first incoming missile alert if that comes first, which sends its drone
into defense mode."""

import threading

import yaml

from wingman.jet_name import profile_for_jet
from wingman.keybindings import AUXILIARY_FEATURE_KEY
from wingman.state import GameState

_KF21 = {"trigger": "low_missiles", "missiles_at": 3, "on_incoming": True}


class _Analyzer:
    """Only what AmmoEventsHandler.tick_events reads."""

    def __init__(self, state=GameState.GAME_BATTLE):
        self.game_state = state
        self.missiles = 4
        self.seq = 0
        self.low_flares_event = threading.Event()
        self.no_missiles_event = threading.Event()

    def read(self, missiles):
        """One new missile count from OCR."""
        self.missiles = missiles
        self.seq += 1

    def get_ammo_missiles(self):
        return self.missiles

    def get_ammo_missiles_read_seq(self):
        return self.seq

    def get_ammo_flares(self):
        return 10

    incoming_ts = 0.0

    def get_incoming_cache_result(self):
        return (self.incoming_ts > 0, None, None)

    def get_incoming_cache_timestamp(self):
        return self.incoming_ts

    def special_weapon_glyph(self):
        return None

    def special_weapon_ready(self):
        return None


class _Ctrl:
    def __init__(self, profile):
        self.profile = profile
        self.presses = 0

    def auxiliary_feature_profile(self):
        return self.profile

    def special_weapon_profile(self):
        return None

    def activate_auxiliary_feature(self, **kw):
        self.presses += 1

    def deploy_flares(self, **kw):
        pass

    def flare_roll(self, flares_done):
        return True

    def pursuit_evade_on_alert(self, ts):
        return (None, False)


def _handler(profile=_KF21, state=GameState.GAME_BATTLE):
    from wingman.tick_handlers import AmmoEventsHandler
    analyzer = _Analyzer(state)
    ctrl = _Ctrl(profile)
    return AmmoEventsHandler(analyzer, ctrl, {}), analyzer, ctrl


def test_the_key_is_w_and_wingman_may_inject_it():
    from wingman.controller import INJECTABLE_KEYS
    assert AUXILIARY_FEATURE_KEY == "w"
    assert AUXILIARY_FEATURE_KEY in INJECTABLE_KEYS


def test_the_kf21_presses_it_once_two_readings_reach_three():
    h, a, ctrl = _handler()
    a.read(4)
    h.tick_events()
    a.read(3)
    h.tick_events()
    assert ctrl.presses == 0, "one reading at 3 is not enough"
    a.read(3)
    h.tick_events()
    assert ctrl.presses == 1


def test_one_reading_counts_once_however_many_ticks_see_it():
    h, a, ctrl = _handler()
    a.read(2)
    for _ in range(5):
        h.tick_events()
    assert ctrl.presses == 0


def test_a_misread_between_low_readings_restarts_the_count():
    h, a, ctrl = _handler()
    for count in (3, 4, 3):
        a.read(count)
        h.tick_events()
    assert ctrl.presses == 0


def test_it_goes_out_once_a_life_and_again_after_a_respawn():
    h, a, ctrl = _handler()
    for count in (3, 2, 4, 4, 1, 0):    # a resupply brings it back to 4 in between
        a.read(count)
        h.tick_events()
    assert ctrl.presses == 1
    h.suppress_after_respawn(0.0)
    for count in (3, 3):
        a.read(count)
        h.tick_events()
    assert ctrl.presses == 2


def test_not_during_the_post_respawn_grace():
    h, a, ctrl = _handler()
    h.suppress_after_respawn(60.0)
    for count in (0, 0, 0):    # the last life's count, still on screen
        a.read(count)
        h.tick_events()
    assert ctrl.presses == 0


def test_a_new_round_may_press_it_again():
    h, a, ctrl = _handler()
    h.on_state_change(GameState.GAME_BATTLE)
    for count in (3, 3):
        a.read(count)
        h.tick_events()
    h.on_state_change(GameState.GAME_END_B)
    h.on_state_change(GameState.GAME_BATTLE)
    for count in (3, 3):
        a.read(count)
        h.tick_events()
    assert ctrl.presses == 2


def test_it_also_goes_out_in_a_pursuit():
    h, a, ctrl = _handler(state=GameState.GAME_BATTLE_EJECT)
    for count in (1, 1):
        a.read(count)
        h.tick_events()
    assert ctrl.presses == 1


def test_not_outside_battle():
    h, a, ctrl = _handler(state=GameState.GAME_LOBBY)
    for count in (0, 0):
        a.read(count)
        h.tick_events()
    assert ctrl.presses == 0


def test_a_jet_without_the_profile_never_presses_it():
    h, a, ctrl = _handler(profile=None)
    for count in (0, 0, 0):
        a.read(count)
        h.tick_events()
    assert ctrl.presses == 0


def test_the_shipped_config_gives_the_kf21_the_feature_at_three():
    with open("wingman/config.yaml") as f:
        cfg = yaml.safe_load(f)
    profiles = cfg["jet_profile"]["profiles"]
    profile = profile_for_jet("KF-21 BORAMAE", profiles)
    assert profile["auxiliary_feature"] == _KF21
    for name in ("J-20 MIGHTY DRAGON", "MIG-23 FLOGGER", "F-14 TOMCAT"):
        assert "auxiliary_feature" not in (profile_for_jet(name, profiles) or {})


def test_the_controller_reads_the_profile_of_the_jet_on_record():
    from wingman.controller import Controller
    ctrl = Controller.__new__(Controller)
    ctrl._jet_profiles = {"kf21": {"match": "KF-21", "auxiliary_feature": dict(_KF21)}}
    ctrl._jet_source = lambda: "KF-21 BORAMAE"
    assert ctrl.auxiliary_feature_profile() == _KF21
    ctrl._jet_source = lambda: "F-14 TOMCAT"
    assert ctrl.auxiliary_feature_profile() is None


def test_the_press_is_a_tap_that_survives_a_cancelled_mission():
    from wingman.controller import Controller
    ctrl = Controller.__new__(Controller)
    calls = []
    ctrl._execute_key_press = lambda key, **kw: calls.append((key, kw))
    ctrl.activate_auxiliary_feature()
    (key, kw), = calls
    assert key == "w"
    assert kw["hold_seconds"] == 0.1 and kw["ignore_cancel"] is True


def test_the_first_incoming_alert_presses_it_before_any_missile_is_fired():
    h, a, ctrl = _handler()
    a.incoming_ts = 100.0
    assert h.deploy_flares_on_new_incoming() is True
    assert ctrl.presses == 1
    a.incoming_ts = 200.0
    h.deploy_flares_on_new_incoming()
    for count in (3, 2):
        a.read(count)
        h.tick_events()
    assert ctrl.presses == 1, "once a life, whichever trigger came first"


def test_an_incoming_alert_after_the_missile_count_sent_it_does_not_press_again():
    h, a, ctrl = _handler()
    for count in (3, 3):
        a.read(count)
        h.tick_events()
    a.incoming_ts = 100.0
    h.deploy_flares_on_new_incoming()
    assert ctrl.presses == 1


def test_an_incoming_alert_in_the_post_respawn_grace_does_not_press_it():
    h, a, ctrl = _handler()
    h.suppress_after_respawn(60.0)
    a.incoming_ts = 100.0
    h.deploy_flares_on_new_incoming()
    assert ctrl.presses == 0


def test_without_on_incoming_an_alert_does_not_press_it():
    h, a, ctrl = _handler(profile={"trigger": "low_missiles", "missiles_at": 3})
    a.incoming_ts = 100.0
    h.deploy_flares_on_new_incoming()
    assert ctrl.presses == 0
