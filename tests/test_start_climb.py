"""ADR 204: the start climb is skipped when the map's level path is clear, and a
start climb a target cut short brings the nose into the 0 to -15 deg band.

Measured before it (2026-11-23 to 25, twelve sessions): no acs life crashed in its
first 20 s with the start climb (0 of 389 battle entries) or without it (0 of 57);
the climb took the nose to a median +60 deg, and 459 of 491 climbs were cut short
by a target and handed to the chase at a median +43 deg, which then overshot to
-30 deg or below in 84 of 455.
"""

import time

import cv2
import numpy as np
import pytest

import wingman.controller as controller_module
from test_mission_acs import (_Analyzer, _SeenTracker, _Tracker, _make_ctrl, _record,
                              _run_mission, _snap, _wait)
from test_terrain_service import _wall_east_top, _write_map
from wingman.start_climb import CLIMB, SKIP, StartClimbTrial
from wingman.terrain_service import TerrainService


# --- the trial -----------------------------------------------------------------

def test_the_arm_is_drawn_per_round_at_the_share():
    class _Rng:
        def __init__(self, values):
            self.values = list(values)

        def random(self):
            return self.values.pop(0)
    trial = StartClimbTrial(0.5, rng=_Rng([0.2, 0.7]))
    assert trial.new_round() == SKIP and trial.skips()
    assert trial.new_round() == CLIMB and not trial.skips()
    assert trial.rounds == 2


def test_a_zero_share_draws_nothing_and_never_skips():
    trial = StartClimbTrial(0.0)
    assert trial.new_round() is None and not trial.skips()


def test_a_share_outside_zero_to_one_is_refused():
    with pytest.raises(ValueError):
        StartClimbTrial(1.5)


# --- TerrainService.level_clear --------------------------------------------------

@pytest.fixture(scope="module")
def texture():
    """The smooth random minimap test_terrain_service uses; level_clear reads only the heights."""
    rng = np.random.default_rng(7)
    small = cv2.GaussianBlur(rng.random((400, 400)).astype(np.float32), (0, 0), 2.0)
    small = (small - small.min()) / (small.max() - small.min()) * 255.0
    return cv2.resize(small, (2048, 2048), interpolation=cv2.INTER_CUBIC).clip(0, 255).astype(np.uint8)


def _service(tmp_path, texture, top, **cfg):
    svc = TerrainService({"maps": {"M": _write_map(tmp_path, texture, top)}, **cfg})
    svc._steer_state = ("M", 10.0, 0.0, 0.0, 250.0, 0.0)      # at the origin, east at 250 m/s
    return svc


def test_open_ground_is_clear(tmp_path, texture):
    svc = _service(tmp_path, texture, np.full((1600, 1600), -232.3, np.float32))
    clear, why = svc.level_clear(1100.0, now=10.0, horizon_s=8.0)
    assert clear is True and "clear for 8s" in why


def test_a_wall_beyond_the_ttc_horizon_but_inside_the_start_horizon_is_not_clear(tmp_path, texture):
    """A 1,000 m wall 1,200 m east at 250 m/s is 4.8 s out: past the ttc fields' 3 s, which
    read clear, and inside the 8 s a level-off has to hold."""
    svc = _service(tmp_path, texture, _wall_east_top(from_x=1200))
    assert svc.level_clear(1100.0, now=10.0, horizon_s=3.0)[0] is True
    clear, why = svc.level_clear(1100.0, now=10.0, horizon_s=8.0)
    assert clear is False and "of the ground in" in why


def test_the_clearance_margin_is_kept_over_the_whole_path(tmp_path, texture):
    """1,100 m over a 1,000 m wall clears it by 100 m, under the 150 m margin."""
    svc = _service(tmp_path, texture, _wall_east_top(from_x=800))
    assert svc.level_clear(1100.0, now=10.0, horizon_s=8.0)[0] is False
    assert svc.level_clear(1200.0, now=10.0, horizon_s=8.0)[0] is True


def test_under_the_margin_where_it_stands_is_not_clear(tmp_path, texture):
    svc = _service(tmp_path, texture, np.full((1600, 1600), 1000.0, np.float32))
    clear, why = svc.level_clear(1100.0, now=10.0, horizon_s=8.0)
    assert clear is False and "under the 150 m margin" in why


def test_no_evidence_is_none(tmp_path, texture):
    svc = _service(tmp_path, texture, np.full((1600, 1600), -232.3, np.float32))
    assert svc.level_clear(None, now=10.0, horizon_s=8.0)[0] is None          # no altitude
    assert svc.level_clear(1100.0, now=13.0, horizon_s=8.0)[0] is None        # fix 3 s old
    svc._steer_state = None
    assert svc.level_clear(1100.0, now=10.0, horizon_s=8.0)[0] is None        # no fix yet


# --- the start decision ----------------------------------------------------------

class _Terrain:
    """Scripted level-path verdicts, one per call (the last repeats)."""

    def __init__(self, verdicts, on_call=None):
        self.verdicts = list(verdicts)
        self.calls = 0
        self.on_call = on_call

    def level_clear(self, altitude, now, horizon_s, max_fix_age_s=2.0):
        if self.on_call is not None:
            self.on_call()
        v = self.verdicts[min(self.calls, len(self.verdicts) - 1)]
        self.calls += 1
        return v

    def climb_wanted(self, max_age_s=2.0):
        return False


def _ctrl(monkeypatch, arm, verdicts, tracker=None, samples=((3100, -10),), **acs):
    ctrl = _make_ctrl(monkeypatch, analyzer=_Analyzer(list(samples)), tracker=tracker or _Tracker(),
                      acs={"yield_to_target": True, "start_skip_share": 0.5,
                           "start_terrain_wait_s": 0.3, **acs})
    ctrl._start_climb_trial.arm = arm
    terrain = _Terrain(verdicts)
    ctrl.set_terrain_service(terrain)
    return ctrl, terrain


def _messages(caplog):
    return [r.getMessage() for r in caplog.records]


def test_a_skip_round_with_a_clear_level_path_does_not_climb(monkeypatch, caplog):
    ctrl, _ = _ctrl(monkeypatch, SKIP, [(True, "level path clear for 8s")])
    log = _record(ctrl, monkeypatch)
    with caplog.at_level("INFO", logger="wingman.controller"):
        _run_mission(ctrl).join(timeout=5.0)
    kinds = [e[0] for e in log]
    assert "pursue" in kinds and "climb" not in kinds
    assert any("skip arm, no climb: level path clear" in m for m in _messages(caplog))


def test_a_climb_round_climbs_as_before_and_logs_the_verdict(monkeypatch, caplog):
    ctrl, _ = _ctrl(monkeypatch, CLIMB, [(True, "level path clear for 8s")])
    log = _record(ctrl, monkeypatch)
    with caplog.at_level("INFO", logger="wingman.controller"):
        _run_mission(ctrl).join(timeout=5.0)
    kinds = [e[0] for e in log]
    assert kinds.index("climb") < kinds.index("pursue")
    assert any("climb arm, climbing; the level path is clear" in m for m in _messages(caplog))


def test_a_skip_round_climbs_when_the_level_path_meets_the_ground(monkeypatch, caplog):
    ctrl, terrain = _ctrl(monkeypatch, SKIP, [(False, "within 150 m of the ground in 4.8s")])
    log = _record(ctrl, monkeypatch)
    start = time.time()
    with caplog.at_level("INFO", logger="wingman.controller"):
        t = _run_mission(ctrl)
        assert _wait(lambda: ("climb", 3000) in log)
    assert time.time() - start < 0.25, "a verdict of not clear does not wait"
    assert terrain.calls == 1
    assert any("skip arm, climbing: the level path is not clear" in m for m in _messages(caplog))
    t.join(timeout=5.0)


def test_a_skip_round_waits_level_for_a_fix_with_ordinary_climbs_held_off(monkeypatch, caplog):
    held = []
    ctrl, terrain = _ctrl(monkeypatch, SKIP, [(None, "no fix"), (None, "no fix"),
                                               (True, "level path clear for 8s")],
                          start_terrain_wait_s=2.0)
    terrain.on_call = lambda: held.append(ctrl.start_watch_remaining_s() > 0.0)
    log = _record(ctrl, monkeypatch)
    with caplog.at_level("INFO", logger="wingman.controller"):
        _run_mission(ctrl).join(timeout=5.0)
    assert terrain.calls == 3
    assert all(held[1:]), "the watch stays armed while it waits for a fix"
    assert "climb" not in [e[0] for e in log]
    assert ctrl.start_watch_remaining_s() == 0.0, "the watch ends with the decision"


def test_a_skip_round_with_no_fix_by_the_end_of_the_wait_climbs(monkeypatch, caplog):
    ctrl, _ = _ctrl(monkeypatch, SKIP, [(None, "no fix with a ground speed")])
    log = _record(ctrl, monkeypatch)
    start = time.time()
    with caplog.at_level("INFO", logger="wingman.controller"):
        t = _run_mission(ctrl)
        assert _wait(lambda: ("climb", 3000) in log)
    assert time.time() - start >= 0.3
    assert any("the level path is unknown" in m for m in _messages(caplog))
    t.join(timeout=5.0)


def test_a_target_that_shows_while_waiting_for_a_fix_goes_to_the_pursuit(monkeypatch, caplog):
    ctrl, _ = _ctrl(monkeypatch, SKIP, [(None, "no fix")], tracker=_SeenTracker(after=1),
                    start_terrain_wait_s=2.0)
    log = _record(ctrl, monkeypatch)
    with caplog.at_level("INFO", logger="wingman.controller"):
        _run_mission(ctrl).join(timeout=5.0)
    kinds = [e[0] for e in log]
    assert "pursue" in kinds and "climb" not in kinds
    assert any("target in view (lock) at the start" in m for m in _messages(caplog))


def test_the_round_start_draws_and_tags_the_arm(monkeypatch):
    ctrl = _make_ctrl(monkeypatch, acs={"start_skip_share": 1.0})
    notes = []
    ctrl._round_note = lambda name, ts: notes.append(name)
    ctrl.start_climb_new_round()
    assert ctrl._start_climb_trial.arm == SKIP and notes == ["start_arm_skip"]


def test_the_shipped_default_is_off():
    from wingman.config_schema import schema_default
    assert schema_default("acs_mission.start_skip_share") == 0.0
    assert schema_default("acs_mission.recover_nose_max_s") == 0.0


# --- the nose band after a climb a target cut short ------------------------------

class _Live(_Analyzer):
    """Telemetry whose flight path the nose-down pulses move, 30 deg a pulse."""

    def __init__(self, alt, angle):
        super().__init__([(alt, angle)])
        self.alt, self.angle = alt, angle

    def get_telemetry(self):
        self._n += 1
        return _snap(self.alt, self.angle, ts=1000.0 + self._n)


def _cut_short(monkeypatch, angle, **acs):
    analyzer = _Live(1500, angle)                       # never reaches 3000 m
    ctrl = _make_ctrl(monkeypatch, analyzer=analyzer, tracker=_SeenTracker(after=1),
                      acs={"yield_to_target": True, "recover_nose_max_s": 1.0, **acs})
    log = _record(ctrl, monkeypatch)

    def _down(*a, **k):
        log.append(("nose_down",))
        analyzer.angle -= 30.0
    monkeypatch.setattr(ctrl, "nose_down", _down)
    return ctrl, log


def test_a_climb_cut_short_nose_high_is_brought_into_the_band_before_the_pursuit(monkeypatch, caplog):
    ctrl, log = _cut_short(monkeypatch, angle=45.0)
    with caplog.at_level("INFO", logger="wingman.controller"):
        _run_mission(ctrl).join(timeout=5.0)
    kinds = [e[0] for e in log]
    # +45 -> +15 -> -15: two pulses, the second landing on the band's edge.
    assert kinds.count("nose_down") == 2
    assert kinds.index("climb") < kinds.index("nose_down") < kinds.index("pursue")
    assert "nose_up" not in kinds
    msgs = _messages(caplog)
    assert any("climb cut short by a target with the nose at +45 deg" in m for m in msgs)
    assert ctrl._nose_recover_until == 0.0, "the hold-off ends with the step"


def test_a_nose_already_in_the_band_is_left_alone(monkeypatch, caplog):
    ctrl, log = _cut_short(monkeypatch, angle=-5.0)
    with caplog.at_level("INFO", logger="wingman.controller"):
        _run_mission(ctrl).join(timeout=5.0)
    kinds = [e[0] for e in log]
    assert "pursue" in kinds and "nose_down" not in kinds
    assert any("already in" in m for m in _messages(caplog))


def test_the_band_step_is_bounded_and_then_hands_over(monkeypatch):
    """A path that never comes down (the pulses do nothing) still reaches the chase."""
    ctrl, log = _cut_short(monkeypatch, angle=60.0, recover_nose_max_s=0.2)
    monkeypatch.setattr(ctrl, "nose_down", lambda *a, **k: log.append(("nose_down",)))
    start = time.time()
    _run_mission(ctrl).join(timeout=5.0)
    assert "pursue" in [e[0] for e in log]
    assert time.time() - start < 2.0


def test_with_the_band_step_off_the_chase_takes_over_at_once(monkeypatch):
    ctrl, log = _cut_short(monkeypatch, angle=45.0, recover_nose_max_s=0.0)
    _run_mission(ctrl).join(timeout=5.0)
    kinds = [e[0] for e in log]
    assert "pursue" in kinds and "nose_down" not in kinds


def _held_off(caplog):
    return any("bringing the nose back" in m for m in _messages(caplog))


def test_an_ordinary_climb_is_held_off_while_the_nose_comes_down(monkeypatch, caplog):
    monkeypatch.setattr(controller_module, "keyboard_module", None)
    ctrl = _make_ctrl(monkeypatch, acs={"recover_nose_max_s": 6.0})
    ctrl._nose_recover_until = time.time() + 5.0
    with caplog.at_level("INFO", logger="wingman.controller"):
        ctrl.climb_mode()
    assert _held_off(caplog) and not ctrl._climbing.is_set()


def test_a_hard_emergency_still_climbs_while_the_nose_comes_down(monkeypatch, caplog):
    monkeypatch.setattr(controller_module, "keyboard_module", None)
    ctrl = _make_ctrl(monkeypatch, acs={"recover_nose_max_s": 6.0})
    ctrl._nose_recover_until = time.time() + 5.0
    with caplog.at_level("INFO", logger="wingman.controller"):
        ctrl.climb_mode(emergency=True)
    assert not _held_off(caplog)
    ctrl._climb_stop.set()
