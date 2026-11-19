"""ADR 173: the special weapon's ring, and when `q` goes out."""

import os
import threading

import pytest
import yaml

from wingman.crop_region import load_crops
from wingman.special_weapon import SpecialWeaponPresser, SpecialWeaponReader
from wingman.state import GameState

_DIR = "test_screenshots/special_weapon"

# Enumerated, not globbed: the folder is a curated corpus (live MiG-23 frames of
# 2026-11-07, picked and checked by eye), and nothing else may slip into it.
_READY = [
    "ready_full_mig23.png",        # 1920x1200, the operator's screenshot
    "ready_haze_1.jpg",
    "ready_haze_2.jpg",
    "ready_cloud_edge.jpg",
    "ready_white_cloud.jpg",
    "ready_dark.jpg",
    "ready_blue.jpg",
]
_NOT_READY = [
    "cooldown_full_mig23.png",     # 1920x1200, the operator's screenshot: 59 s left
    "cooldown_bright_sky.jpg",     # digits on a bright sky
    "active_arc_1.jpg",            # the draining arc just after a press
    "active_arc_2.jpg",
    "active_arc_3.jpg",
    "absent_glare_1.jpg",          # sun glare: the whole spot white
    "absent_glare_2.jpg",
    "absent_terrain.jpg",
    "absent_sky.jpg",
    "absent_locked_session.jpg",   # a MiG-23 round with the ability not unlocked
]


def _config():
    with open("wingman/config.yaml") as f:
        return yaml.safe_load(f)


def _reader(clock=None):
    cfg = _config()
    box = load_crops(cfg["crops"])["SPECIAL_WEAPON"]
    kwargs = {} if clock is None else {"clock": clock}
    return SpecialWeaponReader(box, cfg["special_weapon"], **kwargs)


def _read_twice(name):
    cv2 = pytest.importorskip("cv2")
    path = os.path.join(_DIR, name)
    if not os.path.exists(path):
        pytest.skip(f"{path}: the screenshot corpus lives on VEDA (ADR 100 D7)")
    frame = cv2.imread(path)
    r = _reader()
    r.update(frame)
    r.update(frame)
    return r.ready()


@pytest.mark.parametrize("name", _READY)
def test_a_lit_ring_reads_ready(name):
    assert _read_twice(name) is True


@pytest.mark.parametrize("name", _NOT_READY)
def test_anything_but_a_full_ring_reads_not_ready(name):
    assert _read_twice(name) is False


# --- the reader's debounce -----------------------------------------------------

class _Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


def _bare_reader(clock):
    return SpecialWeaponReader((0.2792, 0.5690, 0.3150, 0.6263), {}, clock=clock)


def test_ready_needs_two_reads_and_one_unlit_read_drops_it():
    clock = _Clock()
    r = _bare_reader(clock)
    assert r.ready() is None, "no read yet"
    r.note_lit(16)
    assert r.ready() is False, "one lit read is not enough"
    r.note_lit(16)
    assert r.ready() is True
    r.note_lit(9)
    assert r.ready() is False, "a taken press shows on the next read"


def test_a_stale_read_is_unknown_not_ready():
    clock = _Clock()
    r = _bare_reader(clock)
    r.note_lit(16)
    r.note_lit(16)
    clock.t += 10.0
    assert r.ready() is None


def test_no_crop_leaves_the_reading_unknown():
    r = SpecialWeaponReader(None, {})
    r.update(object())
    assert r.ready() is None


# --- the press policy ----------------------------------------------------------

def _presser(trigger):
    return SpecialWeaponPresser(trigger, retry_s=3.0, max_presses=2)


def test_an_incoming_edge_with_the_icon_lit_presses_once():
    p = _presser("incoming")
    assert p.tick(0.0, True, True) is True
    assert p.tick(0.5, False, False) is False      # the icon answered
    assert p.tick(10.0, False, False) is False


def test_an_incoming_edge_with_the_icon_unlit_is_dropped_not_queued():
    p = _presser("incoming")
    assert p.tick(0.0, False, True) is False
    assert p.skipped_not_ready == 1
    assert p.tick(1.0, True, False) is False, "the edge is not remembered"


def test_an_unanswered_press_is_repeated_once_then_given_up():
    p = _presser("incoming")
    assert p.tick(0.0, True, True) is True
    assert p.tick(1.0, True, False) is False       # inside retry_s
    assert p.tick(3.0, True, False) is True        # still READY: dropped, retry
    assert p.tick(6.0, True, False) is False       # two presses, give up
    assert p.tick(20.0, True, False) is False
    assert p.tick(21.0, True, True) is True, "a new edge starts again"


def test_the_ready_trigger_presses_when_lit_and_stops_if_the_ring_stays_lit():
    p = _presser("ready")
    assert p.tick(0.0, True, True) is True
    assert p.tick(3.0, True, True) is True
    assert p.tick(6.0, True, True) is False
    assert p.tick(9.0, True, True) is False
    p.tick(10.0, False, True)                      # the ring went out: the count restarts
    assert p.tick(70.0, True, True) is True


def test_low_flares_presses_while_the_count_is_low_and_the_icon_lit():
    p = _presser("low_flares")
    assert p.tick(0.0, True, False) is False
    assert p.tick(1.0, True, True) is True
    assert p.tick(1.5, False, True) is False       # cooldown: the reload went through


def test_unknown_reading_never_presses():
    for trigger in ("incoming", "low_flares", "ready"):
        assert _presser(trigger).tick(0.0, None, True) is False


def test_an_unknown_trigger_is_refused():
    with pytest.raises(ValueError):
        SpecialWeaponPresser("always")


# --- the handler ---------------------------------------------------------------

class _Analyzer:
    def __init__(self, state=GameState.GAME_BATTLE, ready=True, incoming_ts=0.0):
        self.game_state = state
        self.ready = ready
        self.flares = None
        self._incoming_ts = incoming_ts
        self.low_flares_event = threading.Event()
        self.no_missiles_event = threading.Event()

    def special_weapon_ready(self):
        return self.ready

    def get_ammo_flares(self):
        return self.flares

    def get_incoming_cache_result(self):
        return (self._incoming_ts > 0, 0.0, None)

    def get_incoming_cache_timestamp(self):
        return self._incoming_ts

    def get_incoming_latency_marks(self):
        return (0.0, 0.0, 0.0)


class _Ctrl:
    def __init__(self, profile):
        self.profile = profile
        self.keys = []
        self.lock = threading.Lock()

    def special_weapon_profile(self):
        return self.profile

    def activate_special_weapon(self, **kw):
        with self.lock:
            self.keys.append("q")

    def deploy_flares(self, **kw):
        with self.lock:
            self.keys.append("space")

    def reload_flares(self, **kw):
        self.keys.append("reload")

    def flare_roll(self, flares_done):
        return True

    def pursuit_evade_on_alert(self, ts):
        return (None, False)


class _Stats:
    def __init__(self):
        self.events = []

    def on_event(self, name, ts):
        self.events.append(name)


def _shipped_abilities():
    """The shipped `special_weapon.abilities` table (ADR 186): the glyph-known
    abilities are config rows now, not code."""
    import yaml
    with open("wingman/config.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f)["special_weapon"]["abilities"]


def _handler(profile, analyzer):
    from wingman.tick_handlers import AmmoEventsHandler
    ctrl = _Ctrl(profile)
    stats = _Stats()
    h = AmmoEventsHandler(analyzer, ctrl, {}, stats_tracker=stats,
                          special_weapon_cfg={"press_retry_s": 3.0, "max_presses": 2,
                                              "abilities": _shipped_abilities()})
    return h, ctrl, stats


_MIG23 = {"ability": "flare_burst", "trigger": "incoming"}
_J20 = {"ability": "flare_reload", "trigger": "low_flares", "flares_at": 1}
_JAS39 = {"ability": "cloak", "trigger": "ready"}


def test_the_mig23_bursts_on_an_incoming_missile_after_its_first_flare():
    h, ctrl, stats = _handler(_MIG23, _Analyzer(incoming_ts=100.0))
    started = threading.Event()
    real_deploy = ctrl.deploy_flares

    def deploy(**kw):
        real_deploy(**kw)
        started.set()

    ctrl.deploy_flares = deploy
    assert h.deploy_flares_on_new_incoming() is True
    assert started.wait(1.0)
    assert "q" in ctrl.keys
    assert "special_weapon_used" in stats.events
    h.tick_events()
    assert ctrl.keys.count("q") == 1, "no second press without a new alert"


def test_the_mig23_does_not_press_without_an_incoming_missile():
    h, ctrl, _ = _handler(_MIG23, _Analyzer())
    h.tick_events()
    assert "q" not in ctrl.keys


def test_an_incoming_missile_in_cooldown_gets_the_flares_only():
    h, ctrl, stats = _handler(_MIG23, _Analyzer(ready=False, incoming_ts=100.0))
    h.deploy_flares_on_new_incoming()
    assert "q" not in ctrl.keys
    assert "special_weapon_skipped" in stats.events


def test_the_j20_reloads_at_one_flare_only_while_the_icon_is_lit():
    a = _Analyzer(state=GameState.GAME_BATTLE_EJECT, ready=False)
    h, ctrl, stats = _handler(_J20, a)
    a.flares = 1
    h.tick_events()
    assert ctrl.keys == [], "icon not lit"
    a.ready = True
    a.flares = 2
    h.tick_events()
    assert ctrl.keys == [], "two flares is not low for the J-20"
    a.flares = 0
    h.tick_events()
    assert ctrl.keys == ["q"]
    assert "flare_reload" in stats.events, "the summary's reload count still counts it"


def test_the_jas39_cloaks_whenever_the_icon_lights():
    a = _Analyzer()
    h, ctrl, _ = _handler(_JAS39, a)
    h.tick_events()
    assert ctrl.keys == ["q"]


def test_a_jet_with_a_special_weapon_skips_the_two_flare_reload():
    a = _Analyzer()
    h, ctrl, _ = _handler(_MIG23, a)
    a.flares = 2
    h.handle_low_flares()
    assert "reload" not in ctrl.keys


def test_a_jet_without_a_profile_keeps_the_two_flare_reload_and_never_presses_q():
    a = _Analyzer()
    h, ctrl, _ = _handler(None, a)
    h.handle_low_flares()
    h.tick_events()
    assert ctrl.keys == ["reload"]


@pytest.mark.parametrize("state", [GameState.GAME_BATTLE_MANUAL, GameState.GAME_LOBBY,
                                   GameState.GAME_END_B])
def test_no_press_outside_battle_and_pursuit(state):
    h, ctrl, _ = _handler(_JAS39, _Analyzer(state=state))
    h.tick_events()
    assert ctrl.keys == []


def test_no_press_in_the_post_respawn_grace():
    h, ctrl, _ = _handler(_JAS39, _Analyzer())
    h.suppress_after_respawn(60.0)
    h.tick_events()
    assert ctrl.keys == []


def test_an_unread_icon_never_presses():
    h, ctrl, _ = _handler(_JAS39, _Analyzer(ready=None))
    h.tick_events()
    assert ctrl.keys == []


def test_the_shipped_profiles_name_the_three_jets():
    from wingman.jet_name import profile_for_jet
    profiles = _config()["jet_profile"]["profiles"]
    assert profile_for_jet("MIG-23 FLOGGER", profiles)["special_weapon"] == _MIG23
    assert profile_for_jet("J-20 MIGHTY DRAGON", profiles)["special_weapon"] == _J20
    assert profile_for_jet("JAS39 GRIPEN", profiles)["special_weapon"] == _JAS39
    # Operator, 2026-11-15: the towed decoy draws missiles, so it answers an incoming one.
    assert profile_for_jet("TORNADO ADV", profiles)["special_weapon"] == {
        "ability": "towed_decoy", "trigger": "incoming"}
    assert "special_weapon" not in (profile_for_jet("F-14 TOMCAT", profiles) or {})


# --- the drop capture (misread evidence) --------------------------------------

def test_a_ready_that_drops_to_a_partial_ring_saves_the_icon(tmp_path, monkeypatch):
    import numpy as np
    import wingman.special_weapon as sw
    reads = []
    monkeypatch.setattr(sw, "ring_sectors", lambda *a, **k: (reads.pop(0), 0))
    r = SpecialWeaponReader((0.2792, 0.5690, 0.3150, 0.6263),
                            {"capture_dir": str(tmp_path), "capture_max": 2})
    frame = np.zeros((1200, 1920, 3), dtype=np.uint8)
    for lit in (16, 16, 12, 16, 16, 0, 16, 16, 13, 16, 16, 11):
        reads.append(lit)
        r.update(frame)
    names = sorted(p.name for p in tmp_path.iterdir())
    assert len(names) == 2, "a drop to 0 (the HUD gone) is not saved, and the cap holds"
    assert names[0].endswith("_lit12.png") and names[1].endswith("_lit13.png")


# --- live crops: a ring under a HUD marker, and taken presses ------------------
# Saved by the drop capture on 2026-11-07 (204 px around the ring, cut at x 468,
# y 615 of a 1920x1200 frame). Both misreads caught were a yellow target marker
# across a lit ring; the presses are the draining arc on its first read, one with
# a coloured glow in its gap.

_CROPS_READY = ["ready_under_yellow_marker_1.png", "ready_under_yellow_marker_2.png",
                "ready_under_yellow_marker_3.png",
                # D1a's run: both remaining misreads were 14 lit, 0 covered
                "misread_bright_cloud_14of16.png", "misread_objective_marker_14of16.png",
                "misread_bright_cloud_2_14of16.png"]
_CROPS_NOT_READY = ["press_arc_1.png", "press_arc_coloured_gap.png", "press_arc_lit13.png"]


def _read_crop_twice(name):
    cv2 = pytest.importorskip("cv2")
    import numpy as np
    path = os.path.join(_DIR, "crops", name)
    if not os.path.exists(path):
        pytest.skip(f"{path}: the screenshot corpus lives on VEDA (ADR 100 D7)")
    crop = cv2.imread(path)
    frame = np.zeros((1200, 1920, 3), dtype=np.uint8)
    frame[615:615 + crop.shape[0], 468:468 + crop.shape[1]] = crop
    r = _reader()
    r.update(frame)
    r.update(frame)
    return r.ready()


@pytest.mark.parametrize("name", _CROPS_READY)
def test_a_lit_ring_under_a_hud_marker_reads_ready(name):
    assert _read_crop_twice(name) is True


@pytest.mark.parametrize("name", _CROPS_NOT_READY)
def test_a_taken_press_reads_not_ready_even_with_a_coloured_gap(name):
    assert _read_crop_twice(name) is False


def test_covered_sectors_need_most_of_the_ring_lit():
    clock = _Clock()
    r = _bare_reader(clock)
    r.note_lit(12, 4)
    r.note_lit(12, 4)
    assert r.ready() is False, "a press's arc with a coloured gap"
    r.note_lit(13, 3)
    r.note_lit(13, 3)
    assert r.ready() is True, "a lit ring under a marker"
    r.note_lit(5, 11)
    assert r.ready() is False, "mostly covered: not evidence of a lit ring"


# --- the refuel ability, recognised by its glyph (operator, 2026-11-15) --------
# Drop crops of 2026-11-07..14 (204 px cut from 1920x1200 frames): the refuel glyph
# on three jets, and nine other glyphs, the nearest being the missiles-and-arrow one.

_REFUEL_GLYPHS = ["refuel_f111_1.png", "refuel_f111_2.png", "refuel_f15.png",
                  "refuel_f20.png"]
_OTHER_GLYPHS = ["other_missiles_arrow_f100.png", "other_j20.png",
                 "other_flares_mig23.png", "other_decoy_tornado.png",
                 "other_loop_su47.png", "other_arrows_f5.png", "other_mirage_miii.png",
                 "other_nuclear_f106.png", "other_mirage_2000.png"]


def _glyph_score(name):
    pytest.importorskip("cv2")
    from wingman.special_weapon import glyph_iou, load_glyph_template
    cfg = _config()
    box = load_crops(cfg["crops"])["SPECIAL_WEAPON"]
    path = os.path.join(_DIR, "glyphs", name)
    template = os.path.join(_DIR, "glyphs", "refuel.png")
    if not (os.path.exists(path) and os.path.exists(template)):
        pytest.skip(f"{path}: the screenshot corpus lives on VEDA (ADR 100 D7)")
    return (glyph_iou(load_glyph_template(path, box), load_glyph_template(template, box)),
            cfg["special_weapon"]["glyph_min_iou"])


@pytest.mark.parametrize("name", _REFUEL_GLYPHS)
def test_the_refuel_glyph_matches_on_any_jet(name):
    score, threshold = _glyph_score(name)
    assert score >= threshold


@pytest.mark.parametrize("name", _OTHER_GLYPHS)
def test_no_other_glyph_reads_as_refuel(name):
    score, threshold = _glyph_score(name)
    assert score < threshold


def test_the_glyph_needs_two_reads_and_a_named_one_sticks_for_the_round():
    clock = _Clock()
    r = _bare_reader(clock)
    assert r.glyph() is None
    r.note_glyph("other")
    r.note_glyph("other")
    assert r.glyph() == "other"
    r.note_glyph("gun_cooldown")
    assert r.glyph() == "other", "one read does not change it"
    r.note_glyph("gun_cooldown")
    assert r.glyph() == "gun_cooldown"
    r.note_glyph("other")
    r.note_glyph("other")
    assert r.glyph() == "gun_cooldown", "the active state hides the glyph; the jet keeps it"
    r.note_glyph(None)
    assert r.glyph() == "gun_cooldown", "cooldown digits say nothing"
    r.note_glyph("refuel")
    r.note_glyph("refuel")
    assert r.glyph() == "refuel", "another named glyph does replace it"
    clock.t += 60.0                      # the lobby: the next round's jet is unknown
    r.note_glyph("other")
    assert r.glyph() is None


def test_a_dim_glyph_with_no_lit_ring_is_still_recognised():
    """2026-11-15 06:58, F-100: the gun cool-down icon is dim with no lit ring while
    the gun is cold, so the jet was never recognised and the gun kept its 2 km gate.
    A real crop darkened to the dim icon's brightness must still match."""
    cv2 = pytest.importorskip("cv2")
    import numpy as np
    from wingman.special_weapon import RingGeometry, glyph_iou, glyph_mask, load_glyph_template
    cfg = _config()
    box = load_crops(cfg["crops"])["SPECIAL_WEAPON"]
    path = os.path.join(_DIR, "glyphs", "gun_cooldown_f100_2.png")
    template = os.path.join(_DIR, "glyphs", "gun_cooldown.png")
    if not (os.path.exists(path) and os.path.exists(template)):
        pytest.skip(f"{path}: the screenshot corpus lives on VEDA (ADR 100 D7)")
    dim = (cv2.imread(path).astype(np.float32) * 0.55).astype(np.uint8)
    g = RingGeometry(box, (1200, 1920), 16)
    pad = g.size // 2
    mask = glyph_mask(dim, g.cx - (g.x0 - pad), g.cy - (g.y0 - pad), g.radius)
    assert glyph_iou(mask, load_glyph_template(template, box)) >= \
        cfg["special_weapon"]["glyph_min_iou"]


class _FuelAnalyzer(_Analyzer):
    """_Analyzer plus the glyph and timed fuel readings the refuel trigger reads."""

    def __init__(self, glyph="refuel", **kw):
        super().__init__(**kw)
        self.glyph = glyph
        self.fuel = (None, 0.0)

    def special_weapon_glyph(self):
        return self.glyph

    def get_afterburner_fuel_reading(self):
        return self.fuel


def _fuel_ticks(h, a, readings):
    for i, pct in enumerate(readings):
        a.fuel = (pct, 1000.0 + i)
        h.tick_events()


def test_a_refuel_jet_refuels_at_half_fuel_after_two_readings():
    a = _FuelAnalyzer()
    h, ctrl, stats = _handler(None, a)
    _fuel_ticks(h, a, [80, 60, 50])
    assert ctrl.keys == [], "one reading at 50 is not yet two"
    _fuel_ticks(h, a, [49])
    assert ctrl.keys == ["q"]
    assert "special_weapon_used" in stats.events


def test_one_fuel_misread_does_not_spend_the_refuel():
    """2026-11-11 15:06:28-31: 92, 2, 75 %."""
    a = _FuelAnalyzer()
    h, ctrl, _ = _handler(None, a)
    _fuel_ticks(h, a, [92, 2, 75, 69])
    assert ctrl.keys == []


def test_the_same_reading_seen_twice_is_one_reading():
    a = _FuelAnalyzer()
    h, ctrl, _ = _handler(None, a)
    a.fuel = (40, 1000.0)
    h.tick_events()
    h.tick_events()
    assert ctrl.keys == [], "two calls on one reading are not two readings"


def test_refuel_waits_for_the_ring():
    a = _FuelAnalyzer(ready=False)
    h, ctrl, _ = _handler(None, a)
    _fuel_ticks(h, a, [40, 38, 36])
    assert ctrl.keys == []


def test_a_jet_without_the_refuel_glyph_gets_no_refuel():
    # Another known glyph (a cold gun's cool-down), not 'other': since ADR 190 an
    # unknown glyph is pressed whenever lit (tests/test_unknown_ability.py).
    a = _FuelAnalyzer(glyph="gun_cooldown")
    a.gun_hot = lambda: False
    h, ctrl, _ = _handler(None, a)
    _fuel_ticks(h, a, [40, 38, 36])
    assert ctrl.keys == []


def test_a_jets_own_profile_wins_over_the_glyph():
    a = _FuelAnalyzer()
    h, ctrl, _ = _handler(_MIG23, a)
    _fuel_ticks(h, a, [40, 38, 36])
    assert ctrl.keys == [], "the MiG-23's weapon answers incoming missiles, not fuel"


def test_a_refuel_jet_skips_the_two_flare_reload():
    a = _FuelAnalyzer()
    h, ctrl, _ = _handler(None, a)
    a.flares = 2
    h.handle_low_flares()
    assert "reload" not in ctrl.keys


# --- the press capture (2026-11-15: Tornado presses not taken) -----------------

def _press_reader(tmp_path, monkeypatch, clock):
    import numpy as np
    import wingman.special_weapon as sw
    reads = []
    monkeypatch.setattr(sw, "ring_sectors", lambda *a, **k: (reads.pop(0), 0))
    r = SpecialWeaponReader((0.2792, 0.5690, 0.3150, 0.6263),
                            {"capture_dir": str(tmp_path), "capture_max": 10,
                             "refuel_glyph": ""}, clock=clock)
    frame = np.zeros((1200, 1920, 3), dtype=np.uint8)

    def read(lit):
        reads.append(lit)
        r.update(frame)
    return r, read


def test_a_drop_to_nothing_just_after_a_press_is_saved(tmp_path, monkeypatch):
    clock = _Clock()
    r, read = _press_reader(tmp_path, monkeypatch, clock)
    read(16)
    read(16)
    r.note_press()
    clock.t += 1.2
    read(0)
    assert [p.name.endswith("_lit00.png") for p in tmp_path.iterdir()] == [True]


def test_a_drop_to_nothing_with_no_press_is_not_saved(tmp_path, monkeypatch):
    """A death or a round end: the HUD goes, as it does dozens of times a session."""
    clock = _Clock()
    r, read = _press_reader(tmp_path, monkeypatch, clock)
    read(16)
    read(16)
    clock.t += 30
    read(0)
    assert list(tmp_path.iterdir()) == []


def test_a_retry_saves_the_ring_the_first_press_left_lit(tmp_path, monkeypatch):
    clock = _Clock()
    r, read = _press_reader(tmp_path, monkeypatch, clock)
    read(16)
    read(16)
    r.note_press()
    clock.t += 3.1
    read(16)
    r.note_press()
    names = [p.name for p in tmp_path.iterdir()]
    assert len(names) == 1 and names[0].endswith("_retry.png")


# --- the gun cool-down ability and the gun heat bar (ADR 173 D2b) ---------------
# Operator, 2026-11-15: "for jets with machine gun cool down special it should auto
# activate when machine gun overheats".

def _gun_glyph_score(name):
    pytest.importorskip("cv2")
    from wingman.special_weapon import glyph_iou, load_glyph_template
    cfg = _config()
    box = load_crops(cfg["crops"])["SPECIAL_WEAPON"]
    path = os.path.join(_DIR, "glyphs", name)
    template = os.path.join(_DIR, "glyphs", "gun_cooldown.png")
    if not (os.path.exists(path) and os.path.exists(template)):
        pytest.skip(f"{path}: the screenshot corpus lives on VEDA (ADR 100 D7)")
    return (glyph_iou(load_glyph_template(path, box), load_glyph_template(template, box)),
            cfg["special_weapon"]["glyph_min_iou"])


def test_the_gun_cooldown_glyph_matches_another_f100_crop():
    score, threshold = _gun_glyph_score("gun_cooldown_f100_2.png")
    assert score >= threshold


@pytest.mark.parametrize("name", _REFUEL_GLYPHS + [g for g in _OTHER_GLYPHS
                                                   if "f100" not in g])
def test_no_other_glyph_reads_as_gun_cooldown(name):
    score, threshold = _gun_glyph_score(name)
    assert score < threshold


# Design 021's 13 hand-flown gun frames of 2026-11-13 and the operator's MiG-23 pair.
_GUN_FRAMES = {
    "gun_lead/screenshot_20261012_070158.png": None,          # no bar
    "gun_lead/screenshot_20261113_112707.png": None,
    "gun_lead/screenshot_20261113_112710.png": "heating",
    "gun_lead/screenshot_20261113_112711.png": "heating",
    "gun_lead/screenshot_20261113_112712.png": "heating",
    "gun_lead/screenshot_20261113_112721.png": "heating",
    "gun_lead/screenshot_20261113_112722.png": "heating",
    "gun_lead/screenshot_20261113_112723.png": "heating",
    "gun_lead/screenshot_20261113_112752.png": "heating",
    "gun_lead/screenshot_20261113_112801.png": "heating",
    "gun_lead/screenshot_20261113_112802.png": "heating",
    "gun_lead/screenshot_20261113_112810.png": "heating",
    "gun_lead/screenshot_20261113_112833.png": "overheated",   # "OVERHEAT" on screen
    "special_weapon/cooldown_full_mig23.png": "overheated",   # "OVERHEAT DAMAGE TAKEN"
    "special_weapon/ready_full_mig23.png": None,
}


@pytest.mark.parametrize("name,expect", sorted(_GUN_FRAMES.items()))
def test_the_gun_heat_bar_reads_heating_and_overheated(name, expect):
    cv2 = pytest.importorskip("cv2")
    from wingman.gun_heat import read_heat
    path = os.path.join("test_screenshots", name)
    if not os.path.exists(path):
        pytest.skip(f"{path}: the screenshot corpus lives on VEDA (ADR 100 D7)")
    box = load_crops(_config()["crops"])["GUN_HEAT"]
    reading = read_heat(cv2.imread(path), box)
    if expect is None:
        assert reading is None
    else:
        fill, hue = reading
        assert fill > 0
        assert (hue <= 11) == (expect == "overheated")


def test_hot_on_one_read_at_80_percent_or_red_and_one_clears_it():
    """Operator, 2026-11-15: "we should activate when the overheat indicator reaches
    80%"; a red (overheated) bar counts at any width."""
    from wingman.gun_heat import GunHeatReader
    clock = _Clock()
    r = GunHeatReader((0.3958, 0.665, 0.6042, 0.695), hot_at=0.8, clock=clock)
    assert r.hot() is None
    r.note((0.65, 16.0))
    r.note((0.65, 16.0))
    assert r.hot() is False, "the widest orange fill seen, under 80%"
    r.note((0.86, 16.0))
    assert r.hot() is True, ("one read at 80% is enough: at 12:09:16 on 2026-11-15 "
                             "the next read was already red, overheated")
    r.note((0.40, 16.0))
    assert r.hot() is False, "cooled under 80%"
    r.note((0.53, 7.0))
    assert r.hot() is True, "red, overheated, at any width"
    r.note(None)
    assert r.hot() is False, "no bar on screen"


class _GunAnalyzer(_FuelAnalyzer):
    def __init__(self, **kw):
        super().__init__(glyph="gun_cooldown", **kw)
        self.hot = False

    def gun_hot(self):
        return self.hot


def test_a_gun_cooldown_jet_presses_when_the_gun_runs_hot():
    a = _GunAnalyzer()
    h, ctrl, stats = _handler(None, a)
    h.tick_events()
    assert ctrl.keys == []
    a.hot = True
    h.tick_events()
    assert ctrl.keys == ["q"]
    assert "special_weapon_used" in stats.events


def test_gun_cooldown_waits_for_the_ring():
    a = _GunAnalyzer(ready=False)
    h, ctrl, _ = _handler(None, a)
    a.hot = True
    h.tick_events()
    assert ctrl.keys == []


def test_a_refuel_jet_does_not_press_on_a_hot_gun():
    a = _FuelAnalyzer()
    a.gun_hot = lambda: True
    h, ctrl, _ = _handler(None, a)
    h.tick_events()
    assert ctrl.keys == []


# --- ADR 173 D2e: instrumentation for the gun heat and the unknown glyph ---------

def test_the_heat_detail_sees_a_full_bar_with_its_centre_covered():
    """2026-11-07's MiG-23 overheat: the run through the centre reads 0.50, but the red
    reaches nearly both ends, so the two measures must both be logged."""
    cv2 = pytest.importorskip("cv2")
    from wingman.gun_heat import read_heat_detail
    path = "test_screenshots/special_weapon/cooldown_full_mig23.png"
    if not os.path.exists(path):
        pytest.skip(f"{path}: the screenshot corpus lives on VEDA (ADR 100 D7)")
    d = read_heat_detail(cv2.imread(path), load_crops(_config()["crops"])["GUN_HEAT"])
    assert d["fill"] < 0.6
    assert d["reach_l"] > 0.9 and d["reach_r"] > 0.9


def test_an_unknown_glyph_is_logged_and_saved_once_a_round(tmp_path, monkeypatch, caplog):
    import logging
    import numpy as np
    import wingman.special_weapon as sw
    clock = _Clock()
    monkeypatch.setattr(sw, "ring_sectors", lambda *a, **k: (0, 0))
    monkeypatch.setattr(sw, "glyph_mask", lambda *a, **k: np.zeros((40, 40), bool))
    r = SpecialWeaponReader((0.2792, 0.5690, 0.3150, 0.6263),
                            {"capture_dir": str(tmp_path), "capture_max": 10}, clock=clock)
    if not r._templates:
        pytest.skip("glyph templates live on VEDA (ADR 100 D7)")
    frame = np.zeros((1200, 1920, 3), dtype=np.uint8)
    with caplog.at_level(logging.DEBUG, logger="wingman.special_weapon"):
        for _ in range(25):                      # 37.5 s of reads, 1.5 s apart
            r.update(frame)
            clock.t += 1.5
    lines = [m for m in caplog.messages if m.startswith("GLYPH: unknown")]
    assert 3 <= len(lines) <= 5
    saved = [p.name for p in tmp_path.iterdir()]
    assert len(saved) == 1 and "_unknown_" in saved[0]


def test_the_live_1209_burst_is_hot_before_it_overheats():
    """ADR 173 D2f, F-100 on 2026-11-15: GUNHEAT read 0.38, 0.66, 0.86 (orange), then
    0.80 red (overheated) 1.5 s later. The press must be due on the 0.86 read."""
    from wingman.gun_heat import GunHeatReader
    r = GunHeatReader((0.3958, 0.665, 0.6042, 0.695), hot_at=0.8, clock=_Clock())
    hot = []
    for reading in [(0.05, 16.0), (0.38, 16.0), (0.66, 16.0), (0.86, 16.0)]:
        r.note(reading)
        hot.append(r.hot())
    assert hot == [False, False, False, True]
