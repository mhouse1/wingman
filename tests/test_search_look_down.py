"""Look-down search (operator, 2026-09-26): while the pursuit's search roll is
running, tap nose-down so the view covers the fight below. Only above
`pursuit_mode.search_floor_m`, which also replaces "floor in play + margin" as
the dive guard's altitude term, only while the guard is clear, and only while a
fresh flight-path angle is shallower than `search_look_down_min_deg`.

Telemetry is the real TelemetrySnapshot: the flight-path angle comes from its own
pitch_angle_deg(), not a fake of it.
"""

import threading
import time

import wingman.controller as controller_module
from wingman.controller import NOSE_DOWN_KEY, Controller
from wingman.controller_config import ControllerConfig
from wingman.telemetry import TelemetrySignal, TelemetrySnapshot
from tests.test_pursuit_mode import (
    _AnalyzerStub, _CaptureStub, _TrackerStub, _keys, _wait_for_pursuit_to_settle,
)

_SPEED_KPH = 400  # 111 m/s: -20 m/s is about -10 deg, -50 m/s about -27 deg


def _snap(alt, rate=0.0, fresh=True):
    now = time.time()
    ts = now if fresh else now - 60.0
    return TelemetrySnapshot(
        speed=TelemetrySignal(value=_SPEED_KPH, ts=ts, stable_value=_SPEED_KPH),
        altitude=TelemetrySignal(value=int(alt), ts=ts, stable_value=float(alt), rate=rate),
        taken_at_s=now,
    )


class _TelemetryAnalyzer(_AnalyzerStub):
    def __init__(self, snap=None, ammo=2):
        super().__init__(ammo=ammo)
        self.snap = snap

    def get_telemetry(self):
        return self.snap


def _make_ctrl(monkeypatch, analyzer, *, search_floor=2300.0, pulse=0.15, capture=None):
    monkeypatch.setattr(controller_module, "keyboard_module", None)
    ctrl = Controller(
        (0, 0, 1920, 1200),
        analyzer=analyzer,
        exit_event=threading.Event(),
        capture=capture,
        config=ControllerConfig(
            simulate_os_input=True,
            disable_hotkeys=True,
            climb={"alt_floor_m": 4000},
            su30={"alt_floor_m": 3000},
            tracking={"sustained_hold_enabled": True, "pitch_lead_s": 0.0},
            pursuit_mode={"enabled": True, "pursuit_max_duration_s": 0.0,
                          "pursuit_padlock_verify": False, "ammo_zero_grace_s": 0.0,
                          "dive_guard_margin_m": 500.0, "dive_guard_ttg_s": 60.0,
                          "search_floor_m": search_floor,
                          "search_look_down_pulse_s": pulse,
                          "search_look_down_interval_s": 1.0,
                          "search_look_down_min_deg": -20.0},
        ),
    )
    ctrl._set_last_mission("su30")
    return ctrl


def _nose_down_presses(ctrl):
    return [k for k in _keys(ctrl) if k == ("key_press", NOSE_DOWN_KEY)]


class TestSearchFloorInTheGuard:
    def test_the_search_floor_replaces_the_mission_floor_plus_margin(self, monkeypatch):
        ctrl = _make_ctrl(monkeypatch, _TelemetryAnalyzer(_snap(3000)))
        assert ctrl._pursuit_dive_guard() is None, "3000 m is above the 2300 m search floor"
        ctrl._analyzer.snap = _snap(2200)
        assert "2300" in ctrl._pursuit_dive_guard()

    def test_zero_restores_floor_plus_margin(self, monkeypatch):
        ctrl = _make_ctrl(monkeypatch, _TelemetryAnalyzer(_snap(3400)), search_floor=0.0)
        assert "3500" in ctrl._pursuit_dive_guard()

    def test_the_ttg_term_still_applies_above_the_search_floor(self, monkeypatch):
        # 3000 m at -100 m/s: 30 s to ground.
        ctrl = _make_ctrl(monkeypatch, _TelemetryAnalyzer(_snap(3000, rate=-100)))
        assert "time to ground" in ctrl._pursuit_dive_guard()


class TestLookDownPulse:
    def test_a_level_aircraft_gets_a_nose_down_tap(self, monkeypatch):
        ctrl = _make_ctrl(monkeypatch, _TelemetryAnalyzer(_snap(3500)))
        assert ctrl._search_look_down() is True
        assert len(_nose_down_presses(ctrl)) == 1

    def test_taps_are_spaced_by_the_interval(self, monkeypatch):
        ctrl = _make_ctrl(monkeypatch, _TelemetryAnalyzer(_snap(3500)))
        assert ctrl._search_look_down() is True
        ctrl._analyzer.snap = _snap(3500)          # a new sample, but inside the interval
        assert ctrl._search_look_down() is False
        assert len(_nose_down_presses(ctrl)) == 1

    def test_one_tap_per_altitude_sample(self, monkeypatch):
        """Live 2026-09-26 02:05:52 and 53: two taps on one +12 deg sample."""
        ctrl = _make_ctrl(monkeypatch, _TelemetryAnalyzer(_snap(3500)))
        assert ctrl._search_look_down() is True
        ctrl._search_look_down_next_ts = 0.0       # the interval has passed
        assert ctrl._search_look_down() is False, "same sample, second tap"
        ctrl._analyzer.snap = _snap(3480, rate=-5)  # a new reading lands
        time.sleep(0.001)
        ctrl._analyzer.snap = _snap(3480, rate=-5)
        assert ctrl._search_look_down() is True
        assert len(_nose_down_presses(ctrl)) == 2

    def test_a_shallow_descent_still_gets_a_tap(self, monkeypatch):
        ctrl = _make_ctrl(monkeypatch, _TelemetryAnalyzer(_snap(3500, rate=-20)))
        assert ctrl._search_look_down() is True

    def test_no_tap_once_the_angle_reaches_the_limit(self, monkeypatch):
        ctrl = _make_ctrl(monkeypatch, _TelemetryAnalyzer(_snap(3500, rate=-50)))
        assert ctrl._search_look_down() is False
        assert _nose_down_presses(ctrl) == []

    def test_no_angle_reading_means_no_tap(self, monkeypatch):
        ctrl = _make_ctrl(monkeypatch, _TelemetryAnalyzer(_snap(3500, fresh=False)))
        assert ctrl._search_look_down() is False
        ctrl._analyzer.snap = None
        assert ctrl._search_look_down() is False
        assert _nose_down_presses(ctrl) == []

    def test_zero_pulse_turns_it_off(self, monkeypatch):
        ctrl = _make_ctrl(monkeypatch, _TelemetryAnalyzer(_snap(3500)), pulse=0.0)
        assert ctrl._search_look_down() is False

    def test_shipped_config_values(self):
        import yaml
        with open("wingman/config.yaml") as fh:
            pm = yaml.safe_load(fh)["pursuit_mode"]
        assert (pm["search_floor_m"], pm["search_look_down_pulse_s"],
                pm["search_look_down_interval_s"], pm["search_look_down_min_deg"]) \
            == (2300, 0.15, 1.0, -20)


def _pursue_unseen(monkeypatch, snap):
    """A pursuit that never sees a target: the roll searches from the first tick."""
    ctrl = _make_ctrl(monkeypatch, _TelemetryAnalyzer(snap), capture=_CaptureStub())
    ctrl.set_target_tracker(_TrackerStub(visible=False))
    ctrl.pursue_and_engage(weapon_already_switched=True)
    time.sleep(0.5)
    ctrl.stop_eject_sequence("respawn_detected")
    _wait_for_pursuit_to_settle(ctrl)
    return ctrl


def test_a_searching_pursuit_above_the_floor_looks_down(monkeypatch):
    ctrl = _pursue_unseen(monkeypatch, _snap(3500))
    assert _nose_down_presses(ctrl), "the search roll ran with no nose-down tap"


def test_a_searching_pursuit_below_the_floor_does_not(monkeypatch):
    ctrl = _pursue_unseen(monkeypatch, _snap(2200))
    assert _nose_down_presses(ctrl) == []


def test_a_searching_pursuit_already_diving_does_not(monkeypatch):
    ctrl = _pursue_unseen(monkeypatch, _snap(3500, rate=-50))
    assert _nose_down_presses(ctrl) == []


class TestNoAltitudeFloorWithATarget:
    """Operator, 2026-09-26: "when target is detected turn off altitude floor"."""

    def test_a_visible_target_lifts_the_altitude_term(self, monkeypatch):
        ctrl = _make_ctrl(monkeypatch, _TelemetryAnalyzer(_snap(2000)))
        assert "2300" in ctrl._pursuit_dive_guard()
        assert ctrl._pursuit_dive_guard(target_visible=True) is None

    def test_the_ttg_term_is_lifted_with_a_target_too(self, monkeypatch):
        """Operator after the 02:39 capture: "it shouldnt have nosed up". 2373 m at
        -80 m/s (32 s) tripped the ttg term there and its pull-out lost the target."""
        ctrl = _make_ctrl(monkeypatch, _TelemetryAnalyzer(_snap(2373, rate=-80)))
        assert "time to ground" in ctrl._pursuit_dive_guard()
        assert ctrl._pursuit_dive_guard(target_visible=True) is None
        assert ctrl._dive_guard_ttg_tripped is False


def test_a_low_pursuit_with_the_target_below_presses_nose_down(monkeypatch):
    ctrl = _make_ctrl(monkeypatch, _TelemetryAnalyzer(_snap(2000)), capture=_CaptureStub())
    ctrl.set_target_tracker(_TrackerStub(error_norm=0.0, error_norm_y=0.5))
    ctrl.pursue_and_engage(weapon_already_switched=True)
    time.sleep(0.5)
    ctrl.stop_eject_sequence("respawn_detected")
    _wait_for_pursuit_to_settle(ctrl)
    assert _nose_down_presses(ctrl), "a target below at 2000 m was not followed down"


def test_a_diving_chase_with_a_target_is_not_pulled_out(monkeypatch):
    """02:39:26.8 replayed: target below, 2373 m, -80 m/s. No nose-up pulse."""
    from wingman.controller import NOSE_UP_KEY
    ctrl = _make_ctrl(monkeypatch, _TelemetryAnalyzer(_snap(2373, rate=-80)),
                      capture=_CaptureStub())
    ctrl.set_target_tracker(_TrackerStub(error_norm=0.0, error_norm_y=0.3))
    ctrl.pursue_and_engage(weapon_already_switched=True)
    time.sleep(0.5)
    ctrl.stop_eject_sequence("respawn_detected")
    _wait_for_pursuit_to_settle(ctrl)
    assert ("key_press", NOSE_UP_KEY) not in _keys(ctrl)
    assert _nose_down_presses(ctrl)
