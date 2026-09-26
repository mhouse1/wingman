"""Review 018 / action item 001 dive guard: in a pursuit, no nose-down command
below the floor in play plus `pursuit_mode.dive_guard_margin_m`, or while
time to ground is under `dive_guard_ttg_s`. Roll, fire and nose-up untouched.
"""

import threading
import time
import types

import wingman.controller as controller_module
from wingman.controller import NOSE_DOWN_KEY, NOSE_UP_KEY, Controller
from wingman.controller_config import ControllerConfig
from tests.test_pursuit_mode import (
    _AnalyzerStub, _CaptureStub, _TrackerStub, _keys, _wait_for_pursuit_to_settle,
)


def _snap(alt, rate=0.0, fresh=True):
    return types.SimpleNamespace(
        altitude=types.SimpleNamespace(stable_value=alt, rate=rate, ts=time.time()),
        altitude_fresh=lambda: fresh)


class _TelemetryAnalyzer(_AnalyzerStub):
    def __init__(self, snap=None, ammo=2):
        super().__init__(ammo=ammo)
        self.snap = snap

    def get_telemetry(self):
        return self.snap


def _make_ctrl(monkeypatch, analyzer, *, margin=500.0, ttg=60.0, tree_floor=4000,
               capture=None):
    monkeypatch.setattr(controller_module, "keyboard_module", None)
    return Controller(
        (0, 0, 1920, 1200),
        analyzer=analyzer,
        exit_event=threading.Event(),
        capture=capture,
        config=ControllerConfig(
            simulate_os_input=True,
            disable_hotkeys=True,
            climb={"alt_floor_m": tree_floor},
            su30={"alt_floor_m": 3000},
            tracking={"sustained_hold_enabled": True, "pitch_lead_s": 0.0},
            pursuit_mode={"enabled": True, "pursuit_max_duration_s": 0.0,
                          "pursuit_padlock_verify": False, "ammo_zero_grace_s": 0.0,
                          "dive_guard_margin_m": margin, "dive_guard_ttg_s": ttg},
        ),
    )


class TestVerdict:
    def test_below_the_tree_floor_plus_margin_withholds(self, monkeypatch):
        ctrl = _make_ctrl(monkeypatch, _TelemetryAnalyzer(_snap(4200)))
        assert "4500" in ctrl._pursuit_dive_guard()

    def test_above_it_allows(self, monkeypatch):
        ctrl = _make_ctrl(monkeypatch, _TelemetryAnalyzer(_snap(4800)))
        assert ctrl._pursuit_dive_guard() is None

    def test_the_mission_floor_in_play_replaces_the_tree_floor(self, monkeypatch):
        ctrl = _make_ctrl(monkeypatch, _TelemetryAnalyzer(_snap(3700)))
        ctrl._set_last_mission("su30")
        assert ctrl._pursuit_dive_guard() is None, "3700 m is above su30's 3000 + 500"
        ctrl._analyzer.snap = _snap(3400)
        assert "3500" in ctrl._pursuit_dive_guard()

    def test_a_short_time_to_ground_withholds_even_when_high(self, monkeypatch):
        # 6000 m at -150 m/s: 40 s to ground.
        ctrl = _make_ctrl(monkeypatch, _TelemetryAnalyzer(_snap(6000, rate=-150)))
        assert "time to ground" in ctrl._pursuit_dive_guard()

    def test_climbing_high_is_never_guarded(self, monkeypatch):
        ctrl = _make_ctrl(monkeypatch, _TelemetryAnalyzer(_snap(6000, rate=+50)))
        assert ctrl._pursuit_dive_guard() is None

    def test_unreadable_telemetry_fails_open(self, monkeypatch):
        ctrl = _make_ctrl(monkeypatch, _TelemetryAnalyzer(_snap(1000, fresh=False)))
        assert ctrl._pursuit_dive_guard() is None
        ctrl._analyzer.snap = None
        assert ctrl._pursuit_dive_guard() is None

    def test_zero_turns_both_terms_off(self, monkeypatch):
        ctrl = _make_ctrl(monkeypatch, _TelemetryAnalyzer(_snap(500, rate=-200)),
                          margin=0, ttg=0)
        assert ctrl._pursuit_dive_guard() is None

    def test_logs_once_per_change(self, monkeypatch, caplog):
        ctrl = _make_ctrl(monkeypatch, _TelemetryAnalyzer(_snap(4200)))
        with caplog.at_level("INFO", logger="wingman.controller"):
            for _ in range(3):
                ctrl._pursuit_dive_guard()
            ctrl._analyzer.snap = _snap(5000)
            ctrl._pursuit_dive_guard()
        lines = [r.getMessage() for r in caplog.records if "DIVE GUARD" in r.getMessage()]
        assert len(lines) == 2 and "withheld" in lines[0] and "allowed again" in lines[1]

    def test_shipped_defaults(self, monkeypatch):
        monkeypatch.setattr(controller_module, "keyboard_module", None)
        ctrl = Controller((0, 0, 1920, 1200), exit_event=threading.Event(),
                          config=ControllerConfig(simulate_os_input=True, disable_hotkeys=True))
        assert ctrl._pursuit_dive_guard_margin_m == 500.0
        assert ctrl._pursuit_dive_guard_ttg_s == 60.0


def _pursue(monkeypatch, snap, err_y):
    analyzer = _TelemetryAnalyzer(snap)
    ctrl = _make_ctrl(monkeypatch, analyzer, capture=_CaptureStub())
    ctrl.set_target_tracker(_TrackerStub(error_norm=0.0, error_norm_y=err_y))
    ctrl.pursue_and_engage(weapon_already_switched=True)
    time.sleep(0.5)
    ctrl.stop_eject_sequence("respawn_detected")
    _wait_for_pursuit_to_settle(ctrl)
    return _keys(ctrl)


def test_a_low_pursuit_follows_a_visible_target_down(monkeypatch):
    """Reversed 2026-09-26 (operator: "when target is detected turn off altitude
    floor"). Until then a pursuit below floor + margin never pressed nose-down,
    target or not. The altitude term now applies only with no target in view."""
    keys = _pursue(monkeypatch, _snap(3000), err_y=+0.5)
    assert ("key_press", NOSE_DOWN_KEY) in keys


def test_a_high_pursuit_still_presses_nose_down(monkeypatch):
    keys = _pursue(monkeypatch, _snap(6000), err_y=+0.5)
    assert ("key_press", NOSE_DOWN_KEY) in keys


def test_a_low_pursuit_still_pulls_nose_up(monkeypatch):
    keys = _pursue(monkeypatch, _snap(3000), err_y=-0.5)
    assert ("key_press", NOSE_UP_KEY) in keys


# ---------------------------------------------------------------------------
# Pull-out: a steep descent under the ttg term is tapped toward level
# ---------------------------------------------------------------------------

class TestPullout:
    def test_a_steep_descent_under_the_ttg_term_taps_nose_up(self, monkeypatch, caplog):
        # 3900 m at -103 m/s: 38 s to ground, the 20:26:37 reading.
        ctrl = _make_ctrl(monkeypatch, _TelemetryAnalyzer(_snap(3900, rate=-103)))
        with caplog.at_level("INFO", logger="wingman.controller"):
            assert ctrl._pursuit_dive_guard()
            assert ctrl._dive_guard_pullout() is True
        time.sleep(0.1)
        assert ("key_press", NOSE_UP_KEY) in _keys(ctrl)
        assert any("pull-out pulse" in r.getMessage() for r in caplog.records)

    def test_taps_are_spaced_by_the_interval(self, monkeypatch):
        ctrl = _make_ctrl(monkeypatch, _TelemetryAnalyzer(_snap(3900, rate=-103)))
        ctrl._pursuit_dive_guard()
        assert ctrl._dive_guard_pullout() is True
        assert ctrl._dive_guard_pullout() is False, "a second tap inside the interval"

    def test_a_shallow_descent_is_left_alone(self, monkeypatch):
        # 900 m at -20 m/s: 45 s to ground, but already near level.
        ctrl = _make_ctrl(monkeypatch, _TelemetryAnalyzer(_snap(900, rate=-20)))
        assert ctrl._pursuit_dive_guard()
        assert ctrl._dive_guard_pullout() is False

    def test_the_altitude_term_alone_does_not_pull_out(self, monkeypatch):
        """Low but level: nose-down is withheld, nothing is pulled."""
        ctrl = _make_ctrl(monkeypatch, _TelemetryAnalyzer(_snap(3200, rate=-5)))
        assert "altitude" in ctrl._pursuit_dive_guard()
        assert ctrl._dive_guard_pullout() is False

    def test_the_ttg_term_is_recorded_even_when_the_altitude_term_also_holds(self, monkeypatch):
        ctrl = _make_ctrl(monkeypatch, _TelemetryAnalyzer(_snap(3200, rate=-150)))
        assert "altitude" in ctrl._pursuit_dive_guard()
        assert ctrl._dive_guard_pullout() is True

    def test_no_tap_while_the_chase_holds_nose_up(self, monkeypatch):
        ctrl = _make_ctrl(monkeypatch, _TelemetryAnalyzer(_snap(3900, rate=-103)))
        ctrl._pursuit_dive_guard()
        ctrl._pitch_held = "up"
        assert ctrl._dive_guard_pullout() is False

    def test_zero_pulse_turns_it_off(self, monkeypatch):
        ctrl = _make_ctrl(monkeypatch, _TelemetryAnalyzer(_snap(3900, rate=-103)))
        ctrl._dive_guard_pullout_pulse_s = 0.0
        ctrl._pursuit_dive_guard()
        assert ctrl._dive_guard_pullout() is False

    def test_shipped_defaults(self, monkeypatch):
        monkeypatch.setattr(controller_module, "keyboard_module", None)
        ctrl = Controller((0, 0, 1920, 1200), exit_event=threading.Event(),
                          config=ControllerConfig(simulate_os_input=True, disable_hotkeys=True))
        assert (ctrl._dive_guard_pullout_pulse_s, ctrl._dive_guard_pullout_interval_s,
                ctrl._dive_guard_level_rate_mps) == (0.4, 1.0, 30.0)


def test_a_diving_pursuit_pulls_out_with_no_target_visible(monkeypatch):
    """The guard runs every steering tick, not only when a target is below."""
    keys = _pursue_visible(monkeypatch, _snap(3900, rate=-103), visible=False)
    assert ("key_press", NOSE_UP_KEY) in keys
    assert ("key_press", NOSE_DOWN_KEY) not in keys


def test_a_diving_pursuit_with_the_target_below_keeps_chasing(monkeypatch):
    """Reversed 2026-09-26 (operator, after the 02:39 capture: "it shouldnt have
    nosed up" when it had targets). With a target in view neither guard term
    applies; ADR 086's 30 s recovery is the backstop."""
    keys = _pursue_visible(monkeypatch, _snap(3900, rate=-103), visible=True)
    assert ("key_press", NOSE_UP_KEY) not in keys
    assert ("key_press", NOSE_DOWN_KEY) in keys


def test_a_recovery_in_progress_is_not_contested(monkeypatch):
    """ADR 148: while the tree's dive recovery flies, the chase writes no pitch."""
    analyzer = _TelemetryAnalyzer(_snap(3900, rate=-103))
    ctrl = _make_ctrl(monkeypatch, analyzer, capture=_CaptureStub())
    monkeypatch.setattr(ctrl, "pursuit_recovery_active", lambda: True)
    ctrl.set_target_tracker(_TrackerStub(error_norm=0.0, error_norm_y=0.5))
    ctrl.pursue_and_engage(weapon_already_switched=True)
    time.sleep(0.5)
    ctrl.stop_eject_sequence("respawn_detected")
    _wait_for_pursuit_to_settle(ctrl)
    assert ("key_press", NOSE_UP_KEY) not in _keys(ctrl)


def _pursue_visible(monkeypatch, snap, visible):
    analyzer = _TelemetryAnalyzer(snap)
    ctrl = _make_ctrl(monkeypatch, analyzer, capture=_CaptureStub())
    ctrl.set_target_tracker(_TrackerStub(visible=visible, error_norm=0.0, error_norm_y=0.5))
    ctrl.pursue_and_engage(weapon_already_switched=True)
    time.sleep(0.5)
    ctrl.stop_eject_sequence("respawn_detected")
    _wait_for_pursuit_to_settle(ctrl)
    return _keys(ctrl)
