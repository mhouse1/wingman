"""Design 021 Phase 1 (shadow): the gun lead ring and the damage text.

The fixtures in tests/fixtures/gun_lead/ are a curated, enumerated corpus from
operator screenshots taken 2026-11-13 11:27-11:28 while hand-flying, checked by
eye, plus one wingman-flown blackout capture (2026-10-12 15:47). Ring windows
are 480 x 360 px crops of 1920 x 1200 frames with the ring at (240, 180). Do not
glob the directory into a new test without checking what was added.
"""

import logging
import threading

import cv2
import pytest
import yaml
from pathlib import Path

import wingman.controller as controller_module
import wingman.gun_damage as gd
import wingman.lead_ring as lr
from wingman.controller import Controller, FIRE_MACHINE_GUN
from wingman.controller_config import ControllerConfig
from wingman.gun_damage import GunDamageLog, parse_damage, read_damage
from wingman.lead_ring import LeadRingWatch, count_dots, find_ring
from wingman.target_range import RangeReading, TargetRangeReader
from wingman.tracker import TargetTracker

_FIX = Path(__file__).parent / "fixtures" / "gun_lead"
# Window -> the ring's colour, by eye. The ring is at (240, 180) in each.
_RINGS = {
    "ring_112707.png": "red", "ring_112710.png": "red", "ring_112711.png": "red",
    "ring_112721.png": "red", "ring_112722.png": "red", "ring_112723.png": "green",
    "ring_112752.png": "red", "ring_112801.png": "red", "ring_112802.png": "red",
    "ring_112810.png": "red", "ring_112833.png": "tan", "ring_wingman_154716.png": "red",
}
_RING_AT = (240, 180)
# Damage crop -> the number on the screen, by eye; None = no damage text.
_DAMAGE = {
    "dmg_112710.png": 30, "dmg_112711.png": 30, "dmg_112712.png": 70,
    "dmg_112723.png": 70, "dmg_112802.png": 80, "dmg_112833.png": 200,
    "dmg_112721.png": None, "dmg_112801.png": None,
    # Live crops, 2026-11-13 12:59 (CRIMSON): read with a junk first digit from
    # the canyon texture (the file name says what was read) before the read
    # started at the number.
    "dmg_live_junk_188_2188.png": 188, "dmg_live_junk_188_3188.png": 188,
    "dmg_live_junk_188_4188.png": 188, "dmg_live_junk_188_6188.png": 188,
    "dmg_live_junk_94_294.png": 94,
}
_MIN_SCORE = 0.45


@pytest.fixture(autouse=True)
def _no_crop_files(monkeypatch, tmp_path):
    monkeypatch.setattr(lr, "_CROP_DIR", tmp_path)
    monkeypatch.setattr(gd, "_CROP_DIR", tmp_path)


# -- the ring ------------------------------------------------------------------

@pytest.mark.parametrize("name,colour", sorted(_RINGS.items()))
def test_the_ring_is_found_where_it_is(name, colour):
    frame = cv2.imread(str(_FIX / name))
    # A marker about where the tracker puts it: below and right of the ring.
    ring = find_ring(frame, (_RING_AT[0] + 60, _RING_AT[1] + 100), 450, _MIN_SCORE, scale=1.0)
    assert ring is not None
    assert abs(ring.x - _RING_AT[0]) <= 2 and abs(ring.y - _RING_AT[1]) <= 2
    assert ring.colour == colour


def test_no_ring_on_the_kill_frame():
    frame = cv2.imread(str(_FIX / "noring_kill_112712.png"))
    assert find_ring(frame, (240, 180), 450, _MIN_SCORE, scale=1.0) is None


def test_no_ring_on_the_operators_1p3km_frame():
    """The ADR 177 example: a target at 1.3 km, past where the ring shows."""
    frame = cv2.imread(str(Path(__file__).parent / "fixtures" / "gun_range"
                           / "f106_1p3km_full.png"))
    cfg = yaml.safe_load((Path(__file__).parents[1] / "wingman" / "config.yaml").read_text())
    obs = TargetTracker(cfg).update(frame)
    assert find_ring(frame, (obs["centroid_x"], obs["centroid_y"]), 450, _MIN_SCORE) is None


def test_the_tracked_target_and_its_ring_share_the_dotted_line():
    """Full frame, the real tracker: the ring is found from the tracker's marker
    and all four dots lie on the line between them."""
    frame = cv2.imread(str(_FIX / "full_112810.png"))
    cfg = yaml.safe_load((Path(__file__).parents[1] / "wingman" / "config.yaml").read_text())
    obs = TargetTracker(cfg).update(frame)
    assert obs["visible"]
    marker = (obs["centroid_x"], obs["centroid_y"])
    ring = find_ring(frame, marker, 450, _MIN_SCORE)
    assert ring is not None and abs(ring.x - 970) <= 2 and abs(ring.y - 621) <= 2
    assert count_dots(frame, ring, marker) == 4


def test_the_watch_confirms_on_the_second_picture_not_on_a_repeat(caplog):
    frame = cv2.imread(str(_FIX / "full_112810.png"))
    cfg = yaml.safe_load((Path(__file__).parents[1] / "wingman" / "config.yaml").read_text())
    obs = TargetTracker(cfg).update(frame)
    marker = (obs["centroid_x"], obs["centroid_y"])
    watch = LeadRingWatch(450, _MIN_SCORE, (0.4997, 0.508))
    with caplog.at_level(logging.DEBUG, logger="wingman.lead_ring"):
        assert watch.observe(frame, marker, 0.73, ts=1.0, gun_held=True) is None
        # The same picture time again is the same picture: not a second sighting.
        assert watch.observe(frame, marker, 0.73, ts=1.0, gun_held=True) is None
        ring = watch.observe(frame, marker, 0.73, ts=1.1, gun_held=True)
    assert ring is not None
    # The crosshair is at (959.4, 609.6); the ring at (970, 621).
    assert 14 <= watch.cross_px(frame.shape, ring) <= 18
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("LEADRING:")]
    assert len(lines) == 2 and lines[0].startswith("LEADRING: seen")
    assert lines[1].startswith("LEADRING: confirmed") and "dots=4" in lines[1]
    assert watch.searched[0] == 2 and watch.found[0] == 2 and watch.confirmed[0] == 1


def test_the_watch_logs_none_and_counts_the_band():
    frame = cv2.imread(str(_FIX / "noring_kill_112712.png"))
    watch = LeadRingWatch(450, _MIN_SCORE, (0.4997, 0.508))
    # A 360 px window at its own scale would shrink the template; give it the
    # full-frame size by padding.
    big = cv2.copyMakeBorder(frame, 420, 420, 720, 720, cv2.BORDER_CONSTANT, value=(120, 90, 60))
    assert watch.observe(big, (960, 600), 1.1, ts=1.0, gun_held=False) is None
    assert watch.searched[1] == 1 and watch.found[1] == 0


# -- the damage text -------------------------------------------------------------

@pytest.mark.parametrize("text,value", [
    ("30 DMG", 30), (" 70 DMMG", 70), ("3 80 DMG ", 80), ("1   200 DMG 6", 200),
    ("", None), ("114", None), ("4 6", None), ("DMG", None),
])
def test_parse_takes_the_number_touching_dmg(text, value):
    assert parse_damage(text) == value


@pytest.fixture(scope="module")
def ocr():
    easyocr = pytest.importorskip("easyocr")
    return easyocr.Reader(["en"], gpu=False, verbose=False)


@pytest.mark.slow
@pytest.mark.parametrize("name,value", sorted(_DAMAGE.items()))
def test_real_ocr_reads_the_damage(ocr, name, value):
    crop = cv2.imread(str(_FIX / name))
    # read_damage takes a frame: place the crop where the box is on a 1920x1200.
    frame = cv2.copyMakeBorder(crop, 580, 1200 - 632, 690, 1920 - 900,
                               cv2.BORDER_CONSTANT, value=(0, 0, 0))
    assert frame.shape[:2] == (1200, 1920)
    got, text, _ = read_damage(frame, ocr)
    assert got == value, text


@pytest.mark.parametrize("name,col", [
    ("dmg_live_junk_188_4188.png", 67), ("dmg_live_junk_188_3188.png", 67),
    ("dmg_live_junk_94_294.png", 81), ("dmg_112710.png", 81),
])
def test_the_number_starts_where_its_white_glyphs_start(name, col):
    assert gd.number_start(cv2.imread(str(_FIX / name))) == col


# -- burst bookkeeping -------------------------------------------------------------

def test_a_burst_gains_the_rises_and_is_logged_after_it_settles(caplog):
    log = GunDamageLog()
    with caplog.at_level(logging.DEBUG, logger="wingman.gun_damage"):
        log.burst_open(0.8, 19.0, now=10.0)
        assert log.reading_wanted(now=10.1)
        for v in (30, 30, None, 70, 70):
            log.damage(v, None)
        log.burst_close(now=11.0)
        assert log.reading_wanted(now=12.9)
        log.tick(now=12.5)
        assert not [r for r in caplog.records if "GUNHIT" in r.getMessage()]
        log.tick(now=13.1)
    assert not log.reading_wanted(now=13.2)
    msg = [r.getMessage() for r in caplog.records if "GUNHIT" in r.getMessage()][0]
    assert "gain=40" in msg and "range=0.80km" in msg and "ring_px=19" in msg
    assert log.bursts[0] == 1 and log.hit_bursts[0] == 1 and log.damage_total[0] == 40


@pytest.mark.parametrize("values,gain", [
    ([94, 94, 94, 941, 94, 94, 94], 0),              # live 13:48:17, a one-reading misread
    ([94, 94, 188, 188, 282, 282], 188),             # live 13:46:15, two hits of 94
    ([794, 94, 94, 94, 494], 0),                     # rev 1 junk digits, single readings
    ([30, 30, 70], 0),                               # the last value seen once is not credited
    ([94, 94, 94, 94, 4794, 4794], 0),               # live 14:23:53, a wrapped read twice
    ([94, 94, 94, 94, 94, 494, 494], 0),             # live 14:23:55
    ([2494, 2494, 94, 94, 94, 94], 0),               # live 13:30:09, the junk first
    ([94, 94, 188, 188, 2188, 2188, 282, 282], 188),  # hits still count around junk
    ([100, 100, 100, 0, 710, 118, 118], 18),         # live 17:20:20: a lone 0 is not the real value
    ([], 0),
])
def test_only_a_value_read_twice_in_a_row_and_not_wrapped_counts(values, gain):
    assert gd.burst_gain(values)[0] == gain


@pytest.mark.parametrize("value,around,expected", [
    (294, 94, True), (4794, 94, True), (941, 94, True), (2188, 188, True),
    (188, 94, False), (282, 188, False), (94, 94, False), (30, 94, False),
    (100, 0, False), (710, 0, False),
])
def test_wrapped_is_the_old_value_with_characters_added(value, around, expected):
    assert gd.wrapped(value, around) is expected


def test_a_fall_starts_the_count_again_and_is_a_drop():
    log = GunDamageLog()
    log.burst_open(1.5, None, now=0.0)
    for v in (70, 70, 20, 20, 50, 50):
        log.damage(v, None)
    log.flush()
    assert log.damage_total[2] == 30 and log.hit_bursts[2] == 1


def test_a_burst_near_a_missile_launch_is_mixed_and_left_out():
    log = GunDamageLog()
    log.missile_fired(now=8.0)
    log.burst_open(0.9, None, now=10.0)
    for v in (30, 30, 230, 230):
        log.damage(v, None)
    log.burst_close(now=11.0)
    log.tick(now=14.0)
    assert log.mixed == 1 and log.bursts == [0, 0, 0, 0]


def test_a_new_burst_finishes_the_settling_one():
    log = GunDamageLog()
    log.burst_open(0.9, None, now=0.0)
    for v in (30, 30, 60, 60):
        log.damage(v, None)
    log.burst_close(now=1.0)
    log.burst_open(0.8, None, now=1.5)
    assert log.bursts[0] == 1 and log.damage_total[0] == 30


def test_the_worker_reads_damage_only_when_asked():
    calls = []

    class Fake:
        def recognize(self, img, detail=1, allowlist=None):
            calls.append(allowlist)
            return [([], "70 DMG", 0.9)]
    rd = TargetRangeReader(reader_factory=Fake)
    rd.damage_log = GunDamageLog()
    rd.damage_log.burst_open(0.8, None)
    rd.submit_damage(cv2.imread(str(_FIX / "full_112810.png")))
    import time
    deadline = time.time() + 3.0
    while not calls and time.time() < deadline:
        time.sleep(0.01)
    rd.stop()
    assert calls == [gd.ALLOWLIST]


# -- the controller ---------------------------------------------------------------

def _make_ctrl(monkeypatch):
    monkeypatch.setattr(controller_module, "keyboard_module", None)
    ctrl = Controller(
        (0, 0, 1920, 1200), exit_event=threading.Event(),
        config=ControllerConfig(simulate_os_input=True, disable_hotkeys=True),
    )
    ctrl._pursuit_gun_on_centre = True
    ctrl._gun_max_range_km = 2.0
    ctrl._gun_range_max_age_s = 1.5
    ctrl._target_range = TargetRangeReader()
    ctrl._lead_ring = LeadRingWatch(450, _MIN_SCORE, (0.4997, 0.508))
    ctrl._gun_damage = GunDamageLog()
    ctrl._target_range.damage_log = ctrl._gun_damage
    keys = []
    monkeypatch.setattr(ctrl, "_press_tracking_key", lambda k, a: keys.append(("down", k)))
    monkeypatch.setattr(ctrl, "_release_tracking_key", lambda k, a: keys.append(("up", k)))
    return ctrl, keys


def test_the_shadow_does_not_change_when_the_gun_fires(monkeypatch):
    """Phase 1 logs only: a ring well off the crosshair does not stop the gun
    opening on a centred target, and the burst records the ring's distance."""
    import time
    ctrl, keys = _make_ctrl(monkeypatch)
    frame = cv2.imread(str(_FIX / "full_112810.png"))
    cfg = yaml.safe_load((Path(__file__).parents[1] / "wingman" / "config.yaml").read_text())
    obs = TargetTracker(cfg).update(frame)
    ctrl._gun_cluster = obs["cluster"]
    ctrl._target_range._latest = RangeReading(km=0.73, text="0.73km", cluster=obs["cluster"],
                                              ts=time.monotonic(), cost_ms=20.0)
    ctrl._gun_lead_shadow(frame, obs, 1.0)
    ctrl._gun_lead_shadow(frame, obs, 1.1)
    assert ctrl._gun_ring_px is not None and 14 <= ctrl._gun_ring_px <= 18
    ctrl._pursuit_gun_tick(True, 0.01, 0.01)
    assert keys == [("down", FIRE_MACHINE_GUN)]
    assert ctrl._gun_damage._burst is not None and ctrl._gun_damage._burst.km == 0.73
    ctrl.release_gun_hold(why="test")
    assert keys[-1] == ("up", FIRE_MACHINE_GUN)
    assert ctrl._gun_damage._burst.closed is not None
    ctrl._target_range.stop()


def test_the_shared_config_turns_the_shadow_on():
    from wingman.config_schema import schema_default
    assert schema_default("pursuit_mode.gun_lead.enabled") is True
    cfg = yaml.safe_load((Path(__file__).parents[1] / "wingman" / "config.yaml").read_text())
    assert cfg["pursuit_mode"]["gun_lead"]["enabled"] is True


# -- Phase 3: steering on the ring ------------------------------------------------

def _ring_ctrl(monkeypatch, arm):
    """A controller that has confirmed the ring of full_112810 (at (970, 621),
    about 16 px from the crosshair) on pictures 1.0 and 1.1, in a round of `arm`."""
    import random
    import time
    from wingman.lead_ring import RingAimTrial
    ctrl, keys = _make_ctrl(monkeypatch)
    ctrl._ring_aim_trial = RingAimTrial(1.0 if arm == "ring" else 1e-9, rng=random.Random(1))
    ctrl._ring_aim_trial.new_round()
    assert ctrl._ring_aim_trial.arm == arm
    ctrl._ring_fire_px, ctrl._ring_memory_s, ctrl._ring_min_dots = 40.0, 0.45, 2
    ctrl._ring_steer_max_km = 1.2
    frame = cv2.imread(str(_FIX / "full_112810.png"))
    cfg = yaml.safe_load((Path(__file__).parents[1] / "wingman" / "config.yaml").read_text())
    obs = TargetTracker(cfg).update(frame)
    ctrl._gun_cluster = obs["cluster"]
    ctrl._target_range._latest = RangeReading(km=0.73, text="0.73km", cluster=obs["cluster"],
                                              ts=time.monotonic(), cost_ms=20.0)
    ctrl._gun_lead_shadow(frame, obs, 1.0)
    ctrl._gun_lead_shadow(frame, obs, 1.1)
    return ctrl, keys, frame


def test_the_ring_arm_steers_on_the_ring_against_the_crosshair(monkeypatch):
    ctrl, _, frame = _ring_ctrl(monkeypatch, "ring")
    aim = ctrl._ring_aim(frame, True, 1.1)
    assert aim is not None
    # The ring at (970, 621) against the crosshair at (959.4, 609.6).
    assert aim[0] == pytest.approx(10.6 / 960, abs=0.003)
    assert aim[1] == pytest.approx(11.4 / 600, abs=0.004)
    assert 14 <= ctrl._ring_aim_px <= 18
    # Kept over one missed picture, not past ring_memory_s.
    assert ctrl._ring_aim(frame, True, 1.1 + 0.4) is not None
    assert ctrl._ring_aim(frame, True, 1.1 + 0.5) is None
    assert ctrl._ring_aim_px is None
    ctrl._target_range.stop()


def test_the_marker_arm_and_a_lost_target_keep_the_marker(monkeypatch):
    ctrl, _, frame = _ring_ctrl(monkeypatch, "marker")
    assert ctrl._ring_aim(frame, True, 1.1) is None
    ctrl._target_range.stop()
    ctrl, _, frame = _ring_ctrl(monkeypatch, "ring")
    assert ctrl._ring_aim(frame, False, 1.1) is None
    ctrl._target_range.stop()


def test_a_ring_past_steer_max_km_or_without_a_range_is_not_steered_at(monkeypatch):
    """Rev 2, live 19:53: control point A's dashed circle matched as a ring at
    1.9 km and a ring round steered on it. The ring is steered at only while
    the tracked target reads under steer_max_km."""
    ctrl, _, frame = _ring_ctrl(monkeypatch, "ring")
    assert ctrl._lead_km == 0.73
    ctrl._lead_km = 1.9
    assert ctrl._ring_aim(frame, True, 1.1) is None
    ctrl._lead_km = None
    assert ctrl._ring_aim(frame, True, 1.1) is None
    ctrl._lead_km = 1.19
    assert ctrl._ring_aim(frame, True, 1.1) is not None
    ctrl._target_range.stop()


def test_a_ring_without_the_dots_to_this_target_is_not_steered_at(monkeypatch):
    ctrl, _, frame = _ring_ctrl(monkeypatch, "ring")
    ctrl._lead_ring.latest_dots = 1
    assert ctrl._ring_aim(frame, True, 1.1) is None
    ctrl._target_range.stop()


def test_the_ring_arm_fires_on_the_ring_not_the_marker(monkeypatch):
    """The marker 0.3 off the centre is out of the gun box; the ring on the
    crosshair opens the gun, and the burst says it opened on the ring."""
    ctrl, keys, frame = _ring_ctrl(monkeypatch, "ring")
    ctrl._ring_aim(frame, True, 1.1)
    ctrl._pursuit_gun_tick(True, 0.3, 0.3)
    assert keys == [("down", FIRE_MACHINE_GUN)]
    burst = ctrl._gun_damage._burst
    assert burst.arm == "ring" and burst.aim == "ring"
    # The ring moves off: 59 px holds (1.5 times 40), 61 px lets go.
    ctrl._ring_aim_px = 59.0
    ctrl._pursuit_gun_tick(True, 0.3, 0.3)
    assert keys == [("down", FIRE_MACHINE_GUN)]
    ctrl._ring_aim_px = 61.0
    ctrl._pursuit_gun_tick(True, 0.3, 0.3)
    assert keys[-1] == ("up", FIRE_MACHINE_GUN)
    ctrl._target_range.stop()


def test_the_ring_arm_holds_fire_while_the_ring_is_off_the_crosshair(monkeypatch):
    ctrl, keys, frame = _ring_ctrl(monkeypatch, "ring")
    # Move the crosshair: the ring is now about 125 px from it.
    ctrl._lead_ring.crosshair_pct = (0.45, 0.45)
    assert ctrl._ring_aim(frame, True, 1.1) is not None
    ctrl._pursuit_gun_tick(True, 0.0, 0.0)      # the marker dead centre
    assert keys == []
    ctrl._target_range.stop()


def test_the_marker_arm_fires_on_the_marker_rule(monkeypatch):
    ctrl, keys, frame = _ring_ctrl(monkeypatch, "marker")
    ctrl._ring_aim(frame, True, 1.1)
    ctrl._pursuit_gun_tick(True, 0.3, 0.3)
    assert keys == []
    ctrl._pursuit_gun_tick(True, 0.01, 0.01)
    assert keys == [("down", FIRE_MACHINE_GUN)]
    assert ctrl._gun_damage._burst.arm == "marker" and ctrl._gun_damage._burst.aim == "marker"
    ctrl._target_range.stop()


def test_the_ring_on_the_crosshair_is_tallied_by_arm(monkeypatch):
    """Phase 3's measure: of 2 pictures under 1.2 km, the ring was confirmed on
    the crosshair in the second."""
    ctrl, _, _ = _ring_ctrl(monkeypatch, "ring")
    assert ctrl._ring_on_tally == {"ring": [2, 1]}
    ctrl._target_range.stop()


def test_the_round_draw_logs_and_tags_the_arm(monkeypatch, caplog):
    import random
    from wingman.lead_ring import RingAimTrial
    ctrl, _ = _make_ctrl(monkeypatch)
    notes = []
    ctrl._round_note = lambda name, ts: notes.append(name)
    ctrl._ring_aim_trial = RingAimTrial(0.5, rng=random.Random(3))
    with caplog.at_level(logging.INFO, logger="wingman.controller"):
        for _ in range(40):
            ctrl.gun_aim_new_round()
    assert set(notes) == {"gun_arm_ring", "gun_arm_marker"} and len(notes) == 40
    assert any("GUNAIM TRIAL: round 40" in r.getMessage() for r in caplog.records)
    ctrl._target_range.stop()


def test_the_trial_is_off_at_share_zero_and_checks_its_share():
    from wingman.lead_ring import RingAimTrial
    off = RingAimTrial(0.0)
    assert off.new_round() is None and off.arm is None and off.rounds == 0
    assert RingAimTrial(1.0).new_round() == "ring"
    with pytest.raises(ValueError):
        RingAimTrial(1.5)


def test_the_gun_summary_splits_by_arm():
    log = GunDamageLog()
    log.burst_open(0.8, 20.0, now=10.0, arm="ring", aim="ring")
    log.damage(30, None)
    log.damage(30, None)
    log.damage(60, None)
    log.damage(60, None)
    log.burst_close(now=11.0)
    log.tick(now=14.0)
    log.burst_open(0.8, None, now=20.0, arm="marker")
    log.burst_close(now=20.5)
    log.tick(now=23.0)
    s = log.summary()
    assert "marker arm 1 bursts, 0 gained, 0.5s, 0 dmg" in s
    assert "ring arm 1 bursts, 1 gained, 1.0s, 30 dmg" in s


def test_the_shared_config_runs_the_phase3_trial_in_half_the_rounds():
    from wingman.config_schema import schema_default
    assert schema_default("pursuit_mode.gun_lead.steer_share") == 0.0
    cfg = yaml.safe_load((Path(__file__).parents[1] / "wingman" / "config.yaml").read_text())
    gl = cfg["pursuit_mode"]["gun_lead"]
    assert gl["steer_share"] == 0.5 and gl["fire_px"] == 60
    assert gl["ring_memory_s"] == 0.45 and gl["ring_min_dots"] == 2
    assert gl["steer_max_km"] == 1.2 == schema_default("pursuit_mode.gun_lead.steer_max_km")


def test_a_dotted_ring_is_confirmed_across_a_fast_move_and_an_undotted_one_is_not(monkeypatch):
    """Rev 3, live 20:50: at close range the ring moved 80 to 160 px between
    pictures in 15 of 38 dotted pairs. Both pictures dotted: 160 px confirms.
    Otherwise the 80 px rule stands."""
    import numpy as np
    seq = iter([lr.LeadRing(500, 500, 0.7, "green"), lr.LeadRing(620, 540, 0.7, "green"),
                lr.LeadRing(500, 500, 0.7, "green"), lr.LeadRing(620, 540, 0.7, "green")])
    dots = iter([4, 3, 4, 1])
    monkeypatch.setattr(lr, "find_ring", lambda *a, **k: next(seq))
    monkeypatch.setattr(lr, "count_dots", lambda *a, **k: next(dots))
    frame = np.zeros((1200, 1920, 3), dtype=np.uint8)
    watch = LeadRingWatch(450, _MIN_SCORE, (0.4997, 0.508))
    assert watch.observe(frame, (560, 600), 0.6, ts=1.0, gun_held=False) is None
    assert watch.observe(frame, (680, 640), 0.6, ts=1.2, gun_held=False) is not None  # 126 px
    assert watch.observe(frame, (560, 600), 0.6, ts=1.4, gun_held=False) is not None  # 126 px back
    assert watch.observe(frame, (680, 640), 0.6, ts=1.6, gun_held=False) is None      # 1 dot


@pytest.mark.parametrize("values,gun", [
    # 21:51:19 (live, mixed): 98, then 109 and 131 read once each, then 153
    # read again and again. Counted: 98 to 153, a gun-sized 55.
    ([98, 98, 98, 98, 98, 98, 98, 109, 131, 153, 4433, 153, 153, 153, 453, 153, 153, 153], 55),
    # A missile hit of 94 between gun hits: only the gun's rises count.
    ([30, 30, 52, 52, 146, 146, 168, 168], 44),
    # 19:56:40 (live): "1" read as "7" twice, a rise of 600, is not the gun.
    ([113, 113, 713, 713, 1113, 113, 113], 0),
    ([], 0),
])
def test_the_guns_share_is_the_small_rises(values, gun):
    """Rev 6: a burst near a missile launch keeps the gun's rises (60 or less)."""
    assert gd.gun_gain(values) == gun


def test_a_mixed_burst_still_counts_its_gun_damage_by_arm():
    log = GunDamageLog()
    log.missile_fired(now=9.5)
    log.burst_open(0.8, 20.0, now=10.0, arm="ring", aim="ring")
    for v in (98, 98, 120, 120, 214, 214):
        log.damage(v, None)
    log.burst_close(now=11.0)
    log.tick(now=14.0)
    assert log.mixed == 1 and log.by_arm == {}
    assert log.gun_by_arm == {"ring": [1, 1, 1.0, 22]}
    assert "gun share, ring arm: 1 bursts, 1 with gun hits, 1.0s, 22 dmg" in log.summary()


def test_the_shipped_fire_radius_opens_close_and_lets_go_late(monkeypatch):
    """Rev 4: fire_px 60 (operator: "we just need to get it close", the aim
    assist does the rest). 55 px opens the gun, 89 holds it, 91 lets go."""
    cfg = yaml.safe_load((Path(__file__).parents[1] / "wingman" / "config.yaml").read_text())
    ctrl, keys, frame = _ring_ctrl(monkeypatch, "ring")
    ctrl._ring_fire_px = float(cfg["pursuit_mode"]["gun_lead"]["fire_px"])
    ctrl._ring_aim(frame, True, 1.1)
    ctrl._ring_aim_px = 55.0
    ctrl._pursuit_gun_tick(True, 0.3, 0.3)
    assert keys == [("down", FIRE_MACHINE_GUN)]
    ctrl._ring_aim_px = 89.0
    ctrl._pursuit_gun_tick(True, 0.3, 0.3)
    assert keys == [("down", FIRE_MACHINE_GUN)]
    ctrl._ring_aim_px = 91.0
    ctrl._pursuit_gun_tick(True, 0.3, 0.3)
    assert keys[-1] == ("up", FIRE_MACHINE_GUN)
    ctrl._target_range.stop()
