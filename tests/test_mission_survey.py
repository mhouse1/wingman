"""mission_survey: straight passes across the arena for mapping (Design 017 phase 4b)."""

import math
import threading
import time
import unittest.mock as mock

import pytest
import yaml

from wingman.config_schema import schema_default
from wingman.survey import SurveyCommand, SurveyPlan, heading_error
from wingman.telemetry import TelemetrySignal, TelemetrySnapshot

CONFIG_PATH = "wingman/config.yaml"


# --- the plan: decisions only -------------------------------------------------

def _plan(**cfg):
    # 5000 m, not the shipped 3500: these tests are about the decisions, and
    # their altitudes were written against this figure.
    base = {"target_alt": 5000, "hysteresis_m": 500, "altitude_deadband_m": 150,
            "heading_deg": 0, "sweep": "right", "heading_deadband_deg": 6,
            "heading_release_deg": 3, "turn_release_deg": 25, "turn_max_s": 40,
            "edge_turn_frac": 0.5, "edge_holdoff_s": 0, "boundary_max_age_s": 4.0,
            "min_leg_s": 20, "level_band_deg": 25, "climb_rate_ms": 60, "descent_rate_ms": 30,
            "alt_tau_s": 10, "rate_deadband_ms": 15, "rate_per_press_s": 150,
            "rate_gain": 1.0,       # the press sizes below were written for the whole gap
            "pitch_interval_s": 3.0, "press_min_s": 0.08, "press_max_s": 0.35,
            "turn_climb_max_ms": 20, "takeover_alt_m": 1200, "edge_cone_cos": 0.7,
            "edge_close_frac": 0.35, "edge_closing_frac": 0.03}
    base.update(cfg)
    return SurveyPlan(base)


def _meet(plan, t, alt, heading, pitch=None, rate=None, dist=0.4, forward=0.38, lateral=None):
    """The rim getting nearer over three readings, the last at `t` and, unless
    said otherwise, 0.4 radii away and nearly dead ahead. Returns the last command."""
    cmd = None
    for back in (3.0, 1.5, 0.0):
        d = dist + back * 0.05
        cmd = plan.step(t - back, alt, heading, (d, forward * d / dist, t - back, lateral),
                        pitch, rate)
    return cmd


def test_heading_error_is_the_short_way_round():
    assert heading_error(10, 350) == 20
    assert heading_error(350, 10) == -20
    assert heading_error(180, 0) == -180 or heading_error(180, 0) == 180
    assert heading_error(90, 90) == 0


def test_without_a_fresh_altitude_it_commands_nothing():
    cmd = _plan().step(0.0, None, 0.0)
    assert cmd == SurveyCommand("no-altitude", 0.0, 0)


def test_below_the_band_it_climbs_and_does_not_bank():
    cmd = _plan().step(0.0, 4000, 90.0, None, 0.0, 0.0)
    assert cmd.state == "climb" and cmd.pitch == "up"
    assert cmd.roll is None and cmd.pull is False


def test_inside_the_band_it_cruises():
    assert _plan().step(0.0, 4600, 0.0, None, 0.0, 0.0).state == "cruise"


def test_pitch_is_one_press_sized_to_the_rate_gap_then_a_wait():
    """This aircraft keeps the nose where a press leaves it. Presses on a timer
    added up: nine nose-down presses made a 74 degree dive (second flight,
    2026-10-04 09:44) and nose-up presses a vertical zoom (third, 09:59)."""
    plan = _plan(pitch_interval_s=3.0, rate_per_press_s=150)
    first = plan.step(0.0, 2000, 0.0, None, 0.0, 0.0)             # wants +60 m/s, has 0
    assert first.pitch == "up" and first.pitch_s == pytest.approx(0.35), "60/150 s, held to the cap"
    assert [plan.step(t, 2000, 0.0, None, 0.0, 0.0).pitch for t in (0.3, 1.5, 2.9)] == [None] * 3
    again = plan.step(3.1, 2000, 0.0, None, 8.0, 30.0)            # now climbing at 30: half the gap
    assert again.pitch == "up" and again.pitch_s == pytest.approx(0.2)


def test_no_press_once_the_climb_rate_is_the_one_wanted():
    plan = _plan()
    assert plan.step(0.0, 2000, 0.0, None, 12.0, 55.0).pitch is None, "within the deadband of +60"
    assert plan.step(5.0, 2000, 0.0, None, 20.0, 110.0).pitch == "down", "climbing too fast"


def test_without_a_climb_rate_nothing_is_pressed():
    """A press on no reading is the timer again."""
    assert _plan().step(0.0, 2000, 0.0, None, None, None).pitch is None


def test_low_down_just_after_a_spawn_it_climbs_without_waiting_for_a_rate():
    """Fifth flight, 2026-10-04 10:41: the aircraft sat at 550 m for ten seconds."""
    cmd = _plan().step(0.0, 550, 358.0, None, None, None)
    assert cmd.state == "climb" and cmd.pitch == "up" and cmd.pitch_s == pytest.approx(0.35)


def test_level_at_the_target_holds_the_altitude_by_the_climb_rate():
    plan = _plan()
    assert plan.step(0.0, 5000, 0.0, None, 0.0, 5.0).pitch is None
    sinking = plan.step(5.0, 5000, 0.0, None, -8.0, -40.0)
    assert sinking.pitch == "up" and sinking.pitch_s == pytest.approx(40 / 150)
    rising = plan.step(10.0, 5000, 0.0, None, 8.0, 40.0)
    assert rising.pitch == "down"


def test_above_the_target_it_comes_down_gently():
    plan = _plan(descent_rate_ms=30)
    cmd = plan.step(0.0, 5600, 0.0, None, 0.0, 0.0)                # wants -30 m/s, held to the limit
    assert cmd.pitch == "down" and cmd.pitch_s == pytest.approx(30 / 150)


def test_with_the_nose_far_from_level_it_does_not_bank_but_does_correct_the_pitch():
    """A bank in a dive or a zoom is how it departs (ADR 114). Pressing nothing,
    as it first did, leaves this aircraft where it is: the nose does not level
    by itself."""
    dive = _plan().step(0.0, 5000, 300.0, None, -74.0, -200.0)
    assert dive.state == "not-level" and dive.roll is None
    assert dive.pitch == "up" and dive.pitch_s == pytest.approx(0.35)
    zoom = _plan().step(0.0, 5000, 300.0, None, 60.0, 180.0)
    assert zoom.state == "not-level" and zoom.pitch == "down"


def test_a_turn_back_at_the_edge_is_flown_even_with_the_nose_off_level():
    plan = _plan()
    plan.step(0.0, 5000, 0.0)
    cmd = _meet(plan, 30.0, 5000, 0.0, -40.0, -100.0)
    assert cmd.state == "reverse" and cmd.roll == "right"


def test_a_level_nose_or_an_unknown_one_steers_as_usual():
    for pitch_deg in (5.0, None):
        plan = _plan()
        plan.step(0.0, 5000, 300.0, None, pitch_deg)
        assert plan.step(0.3, 5000, 300.0, None, pitch_deg).state == "steer"


def test_a_turn_pulls_only_while_it_is_not_already_climbing():
    """Fourth flight, 2026-10-04 10:23: the tree's boundary turn held its pull for
    12 s, took the nose to 84 degrees up and the aircraft over the top."""
    plan = _plan(turn_climb_max_ms=20)
    plan.step(0.0, 5000, 0.0)
    assert _meet(plan, 30.0, 5000, 0.0, 0.0, 0.0).pull is True
    climbing = plan.step(31.0, 5000, 45.0, None, 15.0, 60.0)
    assert climbing.state == "reverse" and climbing.roll == "right" and climbing.pull is False
    assert plan.step(32.0, 5000, 60.0, None, 2.0, 5.0).pull is True


@pytest.mark.parametrize("heading, roll", [(340.0, "right"), (20.0, "left")])
def test_off_the_pass_heading_it_banks_and_pulls_towards_it(heading, roll):
    plan = _plan()
    plan.step(0.0, 5000, heading)
    cmd = plan.step(0.3, 5000, heading)
    assert cmd.state == "steer" and cmd.roll == roll and cmd.pull is True
    assert cmd.pitch is None, "the pull is the pitch input while it turns"


def test_one_read_off_heading_does_not_start_a_correction():
    """Second flight, 2026-10-04: single reads jumped 15 to 20 degrees and back
    while the aircraft was on heading, and a correction started on each."""
    plan = _plan()
    assert plan.step(0.0, 5000, 358.0).state == "cruise"
    assert plan.step(0.3, 5000, 15.0).state == "cruise"       # one read: not yet
    assert plan.step(0.6, 5000, 358.0).state == "cruise"
    assert plan.step(0.9, 5000, 338.0).state == "cruise"
    assert plan.step(1.2, 5000, 340.0).state == "steer"        # two in a row


def test_a_correction_is_flown_until_the_heading_is_inside_the_release_band():
    plan = _plan(heading_deadband_deg=6, heading_release_deg=3)
    plan.step(0.0, 5000, 340.0)
    assert plan.step(0.3, 5000, 340.0).state == "steer"
    assert plan.step(0.6, 5000, 355.0).state == "steer", "inside the deadband, not yet the release"
    assert plan.step(0.9, 5000, 358.0).state == "cruise"
    assert plan.step(1.2, 5000, 355.0).state == "cruise", "inside the deadband: left alone"


def test_a_correction_above_the_band_banks_without_a_pull():
    """A bank with a pull climbs: 3717 m to 4426 m through the second flight's
    turns, into the height where the aircraft stalls. A bank alone sinks."""
    plan = _plan()
    plan.step(0.0, 5400, 340.0)
    high = plan.step(0.3, 5400, 340.0)
    assert high.state == "steer" and high.roll == "right" and high.pull is False
    plan = _plan()
    plan.step(0.0, 5000, 340.0)
    assert plan.step(0.3, 5000, 340.0).pull is True


def test_the_edge_is_left_alone_for_a_while_after_turning_back():
    """Second flight: the pass was turned round again 17 s after the last time."""
    plan = _plan(edge_holdoff_s=30)
    plan.step(0.0, 5000, 0.0)
    assert _meet(plan, 60.0, 5000, 0.0).new_leg is True
    plan.step(70.0, 5000, 180.0)                               # the turn is over
    assert _meet(plan, 77.0, 5000, 180.0).new_leg is False
    assert _meet(plan, 95.0, 5000, 180.0).new_leg is True


def test_an_unread_heading_presses_no_roll():
    cmd = _plan().step(0.0, 5000, None)
    assert cmd.state == "no-heading" and cmd.roll is None


def test_at_the_edge_the_new_pass_points_away_from_the_way_it_is_flying():
    """Third flight, 10:01:23: a climb had taken the aircraft over the top, and
    it met the edge heading 180 with the pass still set to 000. Turning the pass
    round blindly set it to 180, the way it was already flying towards the edge."""
    plan = _plan(heading_deg=0, sweep="right")
    plan.step(0.0, 5000, 0.0)
    cmd = _meet(plan, 60.0, 5000, 180.0)        # flying south, pass still says north
    assert cmd.new_leg is True and cmd.target == 0.0
    assert cmd.state == "reverse" and cmd.roll == "left", "from south, east is a turn to the left"


def test_on_heading_and_on_altitude_it_presses_nothing():
    cmd = _plan().step(0.0, 5050, 3.0, None, 0.0, 2.0)
    assert cmd.state == "cruise"
    assert cmd.roll is None and cmd.pull is False and cmd.pitch is None


def test_the_edge_ahead_starts_a_pass_the_other_way():
    plan = _plan()
    plan.step(0.0, 5000, 0.0)
    cmd = _meet(plan, 30.0, 5000, 0.0)
    assert cmd.new_leg is True and cmd.leg == 1 and cmd.target == 180.0
    assert cmd.state == "reverse" and cmd.pull is True


def test_the_edge_beside_the_aircraft_is_not_a_reason_to_turn():
    """Fifth flight, 2026-10-04: five of six turns back were made with the rim's
    nearest point 64 to 85 degrees off the nose, flying along the rim."""
    for dist, forward in ((0.52, 0.05), (0.69, 0.30), (0.56, 0.21), (0.73, 0.38)):
        plan = _plan(edge_turn_frac=0.7, edge_cone_cos=0.7, edge_close_frac=0.35)
        plan.step(0.0, 5000, 0.0)
        assert _meet(plan, 30.0, 5000, 0.0, dist=dist, forward=forward).new_leg is False, (dist, forward)
    plan = _plan(edge_turn_frac=0.7, edge_cone_cos=0.7, edge_close_frac=0.35)
    plan.step(0.0, 5000, 0.0)
    assert _meet(plan, 30.0, 5000, 0.0, dist=0.60, forward=0.50).new_leg is True       # within the cone


def test_the_edge_close_beside_the_aircraft_does_turn_it_away_from_the_rim():
    """Far from the arena's middle a pass meets the rim at a slant and the cone
    never fills; the rim being close, and closing, is then the reason. The turn
    goes away from the rim whichever side the passes are moving to."""
    plan = _plan(edge_turn_frac=0.7, edge_cone_cos=0.7, edge_close_frac=0.35, sweep="right")
    plan.step(0.0, 5000, 0.0)
    assert _meet(plan, 30.0, 5000, 0.0, dist=0.45, forward=0.10).new_leg is False
    cmd = _meet(plan, 40.0, 5000, 0.0, dist=0.30, forward=0.08, lateral=0.29)       # rim on the right
    assert cmd.new_leg is True and cmd.roll == "left"
    plan = _plan(sweep="right")
    plan.step(0.0, 5000, 0.0)
    assert _meet(plan, 30.0, 5000, 0.0, dist=0.30, forward=0.08, lateral=-0.29).roll == "right"
    plan = _plan()
    plan.step(0.0, 5000, 0.0)
    assert _meet(plan, 30.0, 5000, 0.0, dist=0.30, forward=-0.08).new_leg is False, "close but behind"


def test_the_edge_is_only_a_reason_to_turn_while_it_is_getting_nearer():
    """Sixth flight, 2026-10-04 10:58: at the spawn the aircraft was flying AWAY
    from the rim, 0.30 radii growing to 0.38, while the forward part of the
    reading flickered between -0.2 and +0.27. One positive read turned it back
    at the rim it had just left."""
    plan = _plan(edge_turn_frac=0.7, edge_cone_cos=0.7)
    plan.step(0.0, 5000, 0.0)
    readings = [(0.303, -0.076), (0.330, -0.151), (0.356, -0.202), (0.372, 0.221),
                (0.374, -0.215), (0.377, 0.271)]
    for i, (dist, forward) in enumerate(readings):
        t = 10.0 + 1.5 * i
        assert plan.step(t, 5000, 0.0, (dist, forward, t, None)).new_leg is False, (dist, forward)
    # One reading, however close and dead ahead, is not a trend.
    plan = _plan()
    plan.step(0.0, 5000, 0.0)
    assert plan.step(30.0, 5000, 0.0, (0.4, 0.38, 30.0, None)).new_leg is False


def test_the_edge_behind_or_far_or_stale_is_not_a_reason_to_turn():
    plan = _plan()
    plan.step(0.0, 5000, 0.0)
    assert _meet(plan, 30.0, 5000, 0.0, forward=-0.3).new_leg is False
    assert _meet(plan, 40.0, 5000, 0.0, dist=0.8, forward=0.78).new_leg is False
    _meet(plan, 50.0, 5000, 0.0, dist=0.75, forward=0.72)
    assert plan.step(60.0, 5000, 0.0, (0.4, 0.38, 50.0, None)).new_leg is False      # read 10 s ago
    assert plan.step(61.0, 5000, 0.0, (None, None, 61.0, None)).new_leg is False
    assert plan.step(62.0, 5000, 0.0, None).new_leg is False


def test_the_turns_go_towards_the_same_compass_side_so_the_passes_lie_side_by_side():
    """Right at one end and left at the other. Turning the same hand at both
    ends would fly a racetrack over the same two lanes."""
    plan = _plan(heading_deg=0, sweep="right")          # passes north and south, moving east
    plan.step(0.0, 5000, 0.0)
    north_end = _meet(plan, 60.0, 5000, 0.0)
    assert north_end.roll == "right", "from north, east is a turn to the right"
    plan.step(70.0, 5000, 180.0)                        # the turn is over
    south_end = _meet(plan, 130.0, 5000, 180.0)
    assert south_end.roll == "left", "from south, east is a turn to the left"
    assert south_end.target == 0.0


def test_sweep_left_mirrors_it():
    plan = _plan(heading_deg=90, sweep="left")          # passes east and west, moving north
    plan.step(0.0, 5000, 90.0)
    assert _meet(plan, 60.0, 5000, 90.0).roll == "left"


def test_a_turn_back_is_flown_one_way_until_the_heading_is_close():
    plan = _plan()
    plan.step(0.0, 5000, 0.0)
    _meet(plan, 60.0, 5000, 0.0)             # turning right towards 180
    # It keeps turning right until the heading is within turn_release_deg, then the
    # ordinary steering takes over.
    assert plan.step(63.0, 5000, 90.0).roll == "right"
    assert plan.step(66.0, 5000, 150.0).roll == "right"
    plan.step(68.0, 5000, 160.0)                        # within turn_release_deg: the turn is over
    close = plan.step(68.3, 5000, 160.0)                # and the ordinary steering finishes it
    assert close.state == "steer" and close.roll == "right"
    assert plan.step(70.0, 5000, 178.0).state == "cruise"


def test_a_turn_back_keeps_going_while_the_heading_is_not_read():
    """A turn that stops half way leaves the aircraft heading for the edge."""
    plan = _plan()
    plan.step(0.0, 5000, 0.0)
    _meet(plan, 60.0, 5000, 0.0)
    cmd = plan.step(62.0, 5000, None)
    assert cmd.state == "reverse" and cmd.roll == "right" and cmd.pull is True


def test_a_turn_back_gives_up_after_its_cap():
    plan = _plan(turn_max_s=40)
    plan.step(0.0, 5000, 0.0)
    _meet(plan, 60.0, 5000, 0.0)
    assert plan.step(101.0, 5000, None).state == "no-heading"


def test_the_edge_does_not_start_another_pass_in_the_middle_of_a_turn():
    plan = _plan()
    plan.step(0.0, 5000, 0.0)
    _meet(plan, 60.0, 5000, 0.0)
    again = _meet(plan, 62.0, 5000, 45.0)
    assert again.new_leg is False and again.leg == 1


def test_two_short_passes_send_the_sweep_back_the_other_way():
    """The arena is a circle: at its side the passes shrink to nothing."""
    plan = _plan(min_leg_s=20)
    plan.step(0.0, 5000, 0.0)
    assert _meet(plan, 60.0, 5000, 0.0).roll == "right"      # long pass, sweep right
    plan.step(65.0, 5000, 180.0)
    assert _meet(plan, 70.0, 5000, 180.0).roll == "left"     # short pass 1, still sweeping right
    plan.step(75.0, 5000, 0.0)
    flipped = _meet(plan, 80.0, 5000, 0.0)                    # short pass 2: flip
    assert flipped.roll == "left", "from north, back towards the west is a turn to the left"


def test_the_edge_turns_the_pass_round_below_the_band_and_without_an_altitude():
    """First flight, 2026-10-04 09:22: the edge was looked at only in level
    flight, the aircraft never left its climb, and it flew out of the arena."""
    plan = _plan()
    plan.step(0.0, 3000, 0.0)
    low = _meet(plan, 30.0, 3000, 0.0)
    assert low.new_leg is True and low.state == "reverse" and low.roll == "right"

    plan = _plan()
    plan.step(0.0, None, 0.0)
    blind = _meet(plan, 30.0, None, 0.0)
    assert blind.new_leg is True and blind.state == "reverse" and blind.pull is True


def test_a_turn_back_outranks_the_climb_until_it_is_done():
    plan = _plan()
    plan.step(0.0, 5000, 0.0)
    _meet(plan, 60.0, 5000, 0.0)
    assert plan.step(62.0, 3000, 90.0).state == "reverse"
    assert plan.step(66.0, 3000, 175.0).state == "climb"                 # round: now it climbs


def test_the_defaults_come_from_the_schema_and_the_shipped_config_declares_them():
    plan = SurveyPlan()
    assert plan.target_alt == schema_default("survey_mission.target_alt")
    assert plan.edge_turn_frac == schema_default("survey_mission.edge_turn_frac")
    with open(CONFIG_PATH, encoding="utf-8") as f:
        shipped = yaml.safe_load(f)["survey_mission"]
    assert SurveyPlan(shipped).target_alt == shipped["target_alt"]


# --- the controller loop --------------------------------------------------------

def _Snap(alt, fresh=True, speed=900):
    """A real TelemetrySnapshot (see tests/test_mission_loiter.py for why not a fake)."""
    now = time.time()
    ts = None if not fresh else now
    return TelemetrySnapshot(
        speed=TelemetrySignal(value=speed, stable_value=speed, ts=ts, rate=0.0),
        altitude=TelemetrySignal(value=alt, stable_value=alt, ts=ts, rate=0.0),
        taken_at_s=now,
        stale_after_s=6.0,
    )


def _survey_ctrl(alt, heading, boundary=None, padlock=False):
    """Controller stub running only the survey loop."""
    from wingman.controller import Controller
    c = Controller.__new__(Controller)
    c._mission_lock = threading.Lock()
    c._mission_complete = threading.Event()
    c._mission_cancel = threading.Event()
    c._mission_generation = 0
    c._mission_generation_lock = threading.Lock()
    c._climb_stop = threading.Event()
    c._climbing = threading.Event()
    c._climb_emergency_requested = False
    c._boundary_turning = threading.Event()
    c._surveying = threading.Event()
    c._exit_event = threading.Event()
    c._operator_stop_event = threading.Event()
    c._turn_guard_until = 0.0
    c._analyzer = mock.MagicMock()
    c._analyzer.get_telemetry.return_value = None if alt is None else _Snap(alt)
    c._analyzer.read_compass_heading.return_value = heading
    c._capture = mock.MagicMock()
    c.padlock_state = mock.MagicMock(return_value=padlock)
    c._loiter_boundary = boundary
    c._survey_boundary = None if boundary is None else (*boundary, None)
    c._survey_turning = False
    # turn_climb_max_ms 20: the loop tests below were written for a turn that
    # pulls while it is not climbing, and keep that path under test. The
    # shipped value pulls only for a sinking turn.
    c._survey_cfg = {"heading_deg": 0, "sweep": "right", "target_alt": 5000, "hysteresis_m": 500,
                     "turn_climb_max_ms": 20}
    c._survey_heading_step_deg = 45.0
    c._survey_battles = 0
    c._survey_map_look_every_s = 0.0     # no look at the full map unless a test asks for it
    c._survey_map_wait_s = 0.2
    c._survey_map_first_look_s = 5.0
    c._survey_arena_radius_m = 9400.0
    c._survey_position_max_age_s = 60.0
    c._survey_edge_backstop_radius = 0.92
    c._survey_radial = None
    c._survey_map_poll_s = 0.005
    c._survey_map_key_hold_s = 0.01
    c._analyzer.full_map_shown.return_value = False
    c._analyzer.full_map_covering.return_value = False
    c._survey_tick_s = 0.01
    c._survey_pulse_s = 0.01
    c._survey_lock_timeout_s = 1.0
    c._survey_status_every_s = 5.0
    c._survey_takeover_alt_m = 1200.0
    c._survey_backstop_frac = 0.3
    c._survey_boundary_max_age_s = 4.0
    c.climb_mode = mock.MagicMock()
    c.nose_up = mock.MagicMock()
    c.nose_down = mock.MagicMock()
    c._execute_key_press = mock.MagicMock()
    return c


def _run_briefly(c, seconds=0.15):
    t = threading.Thread(target=c.mission_survey, daemon=True)
    t.start()
    time.sleep(seconds)
    c.cancel_mission = lambda: None
    c._mission_cancel.set()
    t.join(timeout=2.0)
    assert not t.is_alive()


def _rolls(c):
    return [call.kwargs.get("action_name") for call in c._execute_key_press.call_args_list]


def test_below_the_band_the_loop_climbs_with_its_own_presses_and_does_not_bank():
    c = _survey_ctrl(3000, 0.0)
    _run_briefly(c)
    assert c.nose_up.called
    assert not c.climb_mode.called, "the shared climb takes this aircraft over the top"
    assert not c._execute_key_press.called


def test_on_heading_at_altitude_the_loop_presses_nothing():
    c = _survey_ctrl(5000, 2.0)
    _run_briefly(c)
    assert not c._execute_key_press.called and not c.nose_up.called and not c.nose_down.called
    assert not c.climb_mode.called


def test_off_heading_the_loop_banks_and_pulls():
    from wingman.controller import ROLL_RIGHT_KEY
    c = _survey_ctrl(5000, 300.0)                 # 60 degrees left of north: turn right
    _run_briefly(c)
    assert c._execute_key_press.called
    assert c._execute_key_press.call_args_list[0].args[0] == ROLL_RIGHT_KEY
    assert set(_rolls(c)) == {"survey_roll"}
    assert c.nose_up.called, "a bank without a pull does not turn the flight path (ADR 107)"


def test_stale_telemetry_commands_nothing():
    c = _survey_ctrl(None, 300.0)
    _run_briefly(c)
    assert not c._execute_key_press.called and not c.climb_mode.called


def test_with_the_padlock_on_the_compass_is_not_used():
    """The minimap then follows the camera, so it is not the aircraft's heading."""
    c = _survey_ctrl(5000, 300.0, padlock=True)
    _run_briefly(c)
    assert not c._execute_key_press.called
    assert not c._analyzer.read_compass_heading.called


def test_the_loop_does_not_bank_under_a_climb():
    c = _survey_ctrl(4800, 300.0)                 # inside the band, a climb thread still alive
    c._climbing.set()
    _run_briefly(c)
    assert not c._execute_key_press.called


def test_the_loop_stands_off_while_the_trees_emergency_climb_runs():
    c = _survey_ctrl(4800, 300.0)
    c._climbing.set()
    c._climb_emergency_requested = True
    _run_briefly(c)
    assert not c._execute_key_press.called
    assert c.climb_mode.call_count == 0


def test_a_climb_nobody_needs_any_more_is_stopped():
    """Second flight, 2026-10-04: an emergency cleared after four seconds and its
    climb carried on, nose up, towards 5000 m for another seventy."""
    c = _survey_ctrl(4800, 0.0)                   # in the band, on heading
    c._climbing.set()
    stops = []
    real_set = c._climb_stop.set
    c._climb_stop.set = lambda: (stops.append(time.time()), real_set())[1]
    t = threading.Thread(target=c.mission_survey, daemon=True)
    t.start()
    time.sleep(0.1)
    assert stops, "the leftover climb was not told to stop"
    c.cancel_mission = lambda: None
    c._mission_cancel.set()
    t.join(timeout=2.0)


def test_the_low_climb_that_lifts_a_fresh_spawn_is_left_to_finish():
    c = _survey_ctrl(800, 0.0)                    # below the takeover altitude
    c._climbing.set()
    stops = []
    real_set = c._climb_stop.set
    c._climb_stop.set = lambda: (stops.append(1), real_set())[1]
    t = threading.Thread(target=c.mission_survey, daemon=True)
    t.start()
    time.sleep(0.1)
    assert not stops and not c.nose_up.called and not c._execute_key_press.called
    c.cancel_mission = lambda: None
    c._mission_cancel.set()
    t.join(timeout=2.0)


def test_the_survey_flag_is_set_while_running_and_cleared_on_every_exit():
    c = _survey_ctrl(5000, 0.0)
    t = threading.Thread(target=c.mission_survey, daemon=True)
    t.start()
    time.sleep(0.05)
    assert c.is_surveying() is True
    c.cancel_mission = lambda: None
    c._mission_cancel.set()
    t.join(timeout=2.0)
    assert c.is_surveying() is False and not c._mission_lock.locked()

    c = _survey_ctrl(5000, 0.0)
    c._analyzer.get_telemetry.side_effect = RuntimeError("boom")
    _run_briefly(c)
    assert c.is_surveying() is False and not c._mission_lock.locked()


def test_a_stale_launch_token_does_not_start():
    c = _survey_ctrl(5000, 0.0)
    c._mission_generation = 3
    c.mission_survey(token=2)
    assert not c._surveying.is_set() and not c._mission_lock.locked()


def test_the_navigators_and_the_no_enemy_roll_yield_while_a_survey_flies():
    """Each of them bends a pass, and the no-enemy roll also cancels the mission."""
    c = _survey_ctrl(5000, 0.0)
    c._surveying.set()
    c.roll_left(hold_seconds=0.1, block=False)
    c.roll_right(hold_seconds=0.1, block=False)
    c.cancel_mission = mock.MagicMock()
    c.disengage_roll_right(duration=0.1)
    assert not c._execute_key_press.called
    assert not c.cancel_mission.called, "the no-enemy roll must not cancel the survey"

    c._surveying.clear()
    c.roll_left(hold_seconds=0.1, block=False)
    assert c._execute_key_press.called


def test_the_survey_is_a_mission_battle_entry_and_respawn_can_start():
    from wingman.controller import Controller
    c = _survey_ctrl(5000, 0.0)
    c._default_mission = "survey"
    c._last_mission = None
    c._last_mission_lock = threading.Lock()
    started = threading.Event()
    c.mission_survey = lambda token=None: started.set()
    Controller._start_default_mission(c)
    assert started.wait(1.0)
    assert c._last_mission == "survey"

    started.clear()
    c.is_mission_running = lambda: False
    c._airframe_handed_back = lambda: None
    assert Controller.restart_last_mission(c) is True
    assert started.wait(1.0)


def test_the_trees_boundary_turn_is_a_last_resort_while_a_survey_flies():
    """Refused outright (first flight) the aircraft left the arena; allowed
    freely (fourth) its 12 s pull took the nose to 84 degrees up and over the
    top. It is kept for the edge ahead and close, when the survey's own turn
    has not worked."""
    c = _survey_ctrl(5000, 0.0)
    now = time.time()
    c._loiter_boundary = (0.2, 0.15, now)
    assert c._survey_refuses_boundary_turn() is False, "not surveying: the tree turns as it likes"
    c._surveying.set()
    for reading, refused in [
        (None, True),                       # no reading
        ((0.5, 0.4, now), True),            # ahead, but the survey's own turn has it
        ((0.2, -0.15, now), True),          # close, but behind: flying away from it
        ((0.2, 0.15, now - 30), True),      # read half a minute ago
        ((None, None, now), True),
        ((0.2, 0.15, now), False),          # ahead and inside the last-resort range
    ]:
        c._loiter_boundary = reading
        assert c._survey_refuses_boundary_turn() is refused, reading
    # One thing turns the aircraft at a time. Sixth flight, 10:58: the tree's turn
    # fired five times in eighty seconds in the middle of the survey's own.
    c._survey_turning = True
    assert c._survey_refuses_boundary_turn() is True


def test_the_loop_says_when_it_is_flying_its_own_turn_back():
    c = _survey_ctrl(2000, 358.0)
    seen = []
    c._execute_key_press.side_effect = lambda *a, **k: seen.append(c._survey_turning)
    with mock.patch.object(SurveyPlan, "_edge_ahead", return_value="ahead"):
        _run_briefly(c)
    assert seen and all(seen)
    assert c._survey_turning is False, "cleared when the mission ends"


def test_a_refused_boundary_turn_starts_nothing():
    from wingman.controller import Controller
    c = _survey_ctrl(5000, 0.0, boundary=(0.5, 0.4, time.time()))
    c._surveying.set()
    Controller.boundary_turn_mode(c)                      # returns before touching anything else
    assert not c._boundary_turning.is_set()


def test_the_loop_stands_off_while_the_boundary_turn_runs():
    """The boundary turn picks its own side; a bank from the survey could oppose it."""
    c = _survey_ctrl(5000, 300.0)                 # would otherwise bank right
    c._boundary_turning.set()
    _run_briefly(c)
    assert not c._execute_key_press.called and not c.nose_up.called


def test_the_edge_stops_a_climb_and_turns():
    c = _survey_ctrl(3000, 0.0)
    c._climbing.set()
    with mock.patch.object(SurveyPlan, "_edge_ahead", return_value="ahead"):
        _run_briefly(c)
    assert c._climb_stop.is_set(), "the edge outranks the climb"
    assert c._execute_key_press.called and c.nose_up.called


@pytest.mark.parametrize("alt, target_alt, emergency, refused", [
    (3600, 5000.0, False, True),      # the tree's sustain climb at height: stalled the first flight
    (3600, 5000.0, True, False),      # a hard emergency still climbs
    (3600, None, False, False),       # the low band's climb
    (550, 5000.0, False, False),      # low down it is what lifts a fresh spawn (fifth flight)
    (None, 5000.0, False, False),     # altitude not known: do not take the climb away
])
def test_the_trees_sustain_climb_is_refused_at_height_while_a_survey_flies(alt, target_alt,
                                                                           emergency, refused):
    c = _survey_ctrl(alt, 0.0)
    assert c._survey_refuses_climb(5000.0, False) is False, "not surveying: nothing refused"
    c._surveying.set()
    assert c._survey_refuses_climb(target_alt, emergency) is refused


def test_a_refused_climb_starts_nothing():
    from wingman.controller import Controller
    c = _survey_ctrl(3600, 0.0)
    c._surveying.set()
    Controller.climb_mode(c, target_alt=5000.0)           # returns before touching anything else
    assert not c._climbing.is_set()


def test_the_shipped_altitude_is_below_where_the_climb_stalled():
    """2026-10-04: the nose-up climb stalled near 4000 m and peaked at 4321 m."""
    assert schema_default("survey_mission.target_alt") <= 3600
    with open(CONFIG_PATH, encoding="utf-8") as f:
        assert yaml.safe_load(f)["survey_mission"]["target_alt"] <= 3600


def test_the_loop_trims_once_and_waits_instead_of_pressing_every_tick():
    c = _survey_ctrl(5400, 0.0)                    # 400 m above a 5000 m target
    _run_briefly(c, seconds=0.25)                  # about twenty ticks of 10 ms
    assert c.nose_down.call_count == 1


def test_a_turn_back_is_not_flown_while_the_aircraft_is_low():
    """Fifth flight, 2026-10-04 10:41: a turn back flown at 550 m, fifteen seconds
    after the spawn, went into the terrain. Height first."""
    c = _survey_ctrl(550, 358.0)
    with mock.patch.object(SurveyPlan, "_edge_ahead", return_value="ahead"):
        _run_briefly(c)
    assert not c._execute_key_press.called, "no bank at 550 m"
    assert c.nose_up.called, "it climbs instead"

    c = _survey_ctrl(550, 358.0)
    c._climbing.set()                             # the tree's climb is already lifting it
    with mock.patch.object(SurveyPlan, "_edge_ahead", return_value="ahead"):
        _run_briefly(c)
    assert not c._execute_key_press.called and not c.nose_up.called


def test_above_the_takeover_altitude_the_turn_back_is_flown():
    c = _survey_ctrl(2000, 358.0)
    with mock.patch.object(SurveyPlan, "_edge_ahead", return_value="ahead"):
        _run_briefly(c)
    assert c._execute_key_press.called


# ── each battle flies the next pass heading ───────────────────────────────

def test_each_battle_starts_its_passes_further_round():
    from wingman.survey import battle_heading
    assert [battle_heading(0.0, 45.0, n) for n in range(9)] == [
        0.0, 45.0, 90.0, 135.0, 180.0, 225.0, 270.0, 315.0, 0.0]
    assert battle_heading(350.0, 45.0, 1) == 35.0            # round the compass
    assert battle_heading(0.0, 0.0, 5) == 0.0                # a step of 0 keeps one heading
    assert battle_heading(0.0, 45.0, -1) == 0.0              # started with no battle counted


def test_a_session_starts_part_way_round_the_headings():
    """Stopped and started every few battles, a session that always began on
    the first heading would never fly the later ones."""
    from wingman.survey import first_battle_index
    starts = {first_battle_index(60.0 * minute, 45.0) for minute in range(8)}
    assert starts == set(range(8)), "every one of the eight headings can come first"
    assert first_battle_index(125.0, 45.0) == first_battle_index(179.0, 45.0) == 2
    assert first_battle_index(1e9, 0.0) == 0, "a step of 0 has one heading"


def _first_pass_headings(c, caplog, starts):
    """The pass headings the loop announces, one per start in `starts`
    ("battle" for a battle entry, "respawn" for a restart inside one)."""
    c._default_mission = "survey"
    c._last_mission = None
    c._last_mission_lock = threading.Lock()
    c._manual_takeover_active = lambda: False
    c.cancel_mission = lambda: None
    seen = []
    with caplog.at_level("INFO", logger="wingman.controller"):
        for start in starts:
            c._mission_cancel.clear()
            before = len(caplog.records)
            if start == "battle":
                c._start_default_mission()
            else:
                assert c.restart_last_mission() is True
            deadline = time.time() + 2.0
            line = None
            while line is None and time.time() < deadline:
                line = next((r.getMessage() for r in caplog.records[before:]
                             if "mission_survey - passes on" in r.getMessage()), None)
                time.sleep(0.01)
            assert line is not None, f"the survey did not start for the {start}"
            seen.append(line.split("passes on ")[1][:11])
            c._mission_cancel.set()
            deadline = time.time() + 2.0
            while c._mission_lock.locked() and time.time() < deadline:
                time.sleep(0.01)
            assert not c._mission_lock.locked()
    return seen


def test_a_new_battle_takes_the_next_heading_and_a_respawn_keeps_the_battles(caplog):
    """27 mission starts on 2026-10-04 all flew 000 and 180: one set of lanes."""
    c = _survey_ctrl(alt=5000, heading=0.0)
    assert _first_pass_headings(c, caplog, ["battle", "respawn", "battle", "respawn", "battle"]) == [
        "000 and 180", "000 and 180", "045 and 225", "045 and 225", "090 and 270"]


def test_a_heading_step_of_zero_flies_every_battle_on_the_same_heading(caplog):
    c = _survey_ctrl(alt=5000, heading=0.0)
    c._survey_heading_step_deg = 0.0
    assert _first_pass_headings(c, caplog, ["battle", "battle"]) == ["000 and 180", "000 and 180"]



# ── the altitude hold on readings that are seconds old ────────────────────

def test_a_press_corrects_part_of_the_gap_not_all_of_it():
    """Eighth flight, 2026-10-04: the hold corrected the whole gap on a rate 1.5
    to 4.5 s old, and from 35 s into a pass on the altitude was still swinging
    by a median 499 m (98 passes)."""
    cmd = _plan(rate_gain=0.5).step(0.0, 5000, 0.0, None, -8.0, -40.0)   # sinking at 40, wants 0
    assert cmd.pitch == "up" and cmd.pitch_s == pytest.approx(0.5 * 40 / 150)


def test_one_altitude_reading_gets_one_press():
    """The readings land every 3 s and so does the press timer: without this the
    same reading could be acted on twice, which is the whole gap again."""
    plan = _plan(pitch_interval_s=3.0)
    assert plan.step(0.0, 5000, 0.0, None, -8.0, -40.0, alt_ts=100.0).pitch == "up"
    assert plan.step(3.5, 5000, 0.0, None, -8.0, -40.0, alt_ts=100.0).pitch is None, "same reading"
    assert plan.step(4.0, 5000, 0.0, None, -8.0, -40.0, alt_ts=103.0).pitch == "up", "a new one"


def test_the_shipped_gain_corrects_less_than_the_whole_gap():
    assert 0 < schema_default("survey_mission.rate_gain") < 1


def test_the_loop_flies_on_the_last_reading_carried_to_now_not_on_the_mean(caplog):
    """The mean of the last three readings trails a descent by hundreds of
    metres. Here it says 3600 m, inside the band, while the aircraft read
    3000 m two seconds ago and is sinking at 60 m/s: 2880 m, below it."""
    c = _survey_ctrl(alt=3600, heading=0.0)
    c._survey_cfg = dict(c._survey_cfg, target_alt=3500, hysteresis_m=500)
    now = time.time()
    c._analyzer.get_telemetry.return_value = TelemetrySnapshot(
        speed=TelemetrySignal(value=900, stable_value=900, ts=now, rate=0.0),
        altitude=TelemetrySignal(value=3000, stable_value=3600, ts=now - 2.0, rate=-60.0),
        taken_at_s=now, stale_after_s=6.0)
    with caplog.at_level("INFO", logger="wingman.controller"):
        _run_briefly(c)
    line = next(r.getMessage() for r in caplog.records if "SURVEY: state=" in r.getMessage())
    assert "state=climb" in line and "alt=2880" in line


# ── the look at the full map ──────────────────────────────────────────────

class _MapGame:
    """The game's side of the m key: each press toggles the map, but only
    `lag` looks at the picture later. A press in `deaf` (counted from 1) is
    not heard at all."""

    def __init__(self, c, lag=1, deaf=(), opened=False):
        self.open, self.lag, self.deaf = opened, lag, set(deaf)
        self.presses, self._due = 0, []
        c._execute_key_press.side_effect = self._press
        c._analyzer.full_map_shown.side_effect = self._shown
        c._analyzer.full_map_covering.side_effect = lambda frame: self.open
        c._analyzer.read_full_map.side_effect = lambda frame: "a fix" if self.open else None

    def _press(self, key, **kwargs):
        if key != "m":
            return
        self.presses += 1
        if self.presses not in self.deaf:
            self._due.append(self.lag)

    def _shown(self, frame):
        self._due = [n - 1 for n in self._due]
        while self._due and self._due[0] <= 0:
            self._due.pop(0)
            self.open = not self.open
        return self.open


def test_a_look_opens_the_map_reads_it_and_closes_it():
    c = _survey_ctrl(alt=3500, heading=0.0)
    game = _MapGame(c)
    assert c._survey_look_at_map() == "a fix"
    assert game.presses == 2 and not game.open, "one press to open, one to close"
    close = [call for call in c._execute_key_press.call_args_list if call.args[0] == "m"][1]
    assert close.kwargs.get("ignore_cancel") is True, "closed even if the mission was just cancelled"


def test_a_map_that_comes_up_late_is_waited_for_and_then_closed():
    """Eleventh flight, 2026-10-04 17:04:36: a check 0.72 s after the key found
    no map, nothing more was pressed, the map came up after it and stayed open
    for 28 s with the aircraft flying blind."""
    c = _survey_ctrl(alt=3500, heading=0.0)
    game = _MapGame(c, lag=12)
    assert c._survey_look_at_map() == "a fix"
    assert game.presses == 2 and not game.open


def test_a_map_that_is_slow_to_go_is_not_pressed_again():
    """Same flight, 17:05:09: the map was still up 0.35 s after the closing
    press, so the key was pressed again, which opened it again."""
    c = _survey_ctrl(alt=3500, heading=0.0)
    game = _MapGame(c, lag=10)
    c._survey_look_at_map()
    assert game.presses == 2 and not game.open, "one closing press, and the wait for it to work"


def test_a_closing_press_the_game_did_not_hear_is_made_again():
    c = _survey_ctrl(alt=3500, heading=0.0)
    game = _MapGame(c, deaf={2})
    assert c._survey_look_at_map() == "a fix"
    assert game.presses == 3 and not game.open


def test_an_opening_press_the_game_did_not_hear_gets_no_closing_press():
    """m toggles: a closing press for a map that never opened would open it."""
    c = _survey_ctrl(alt=3500, heading=0.0)
    game = _MapGame(c, deaf={1})
    assert c._survey_look_at_map() is None
    assert game.presses == 1 and not game.open
    c._analyzer.read_full_map.assert_not_called()


def test_a_map_already_open_is_read_and_closed_without_an_opening_press():
    c = _survey_ctrl(alt=3500, heading=0.0)
    game = _MapGame(c, opened=True)
    assert c._survey_look_at_map() == "a fix"
    assert game.presses == 1 and not game.open


def test_a_map_that_is_up_but_cannot_be_read_is_still_closed():
    """The ring is over the view and the N never shows: nothing is read, and
    the view is not left covered."""
    c = _survey_ctrl(alt=3500, heading=0.0)
    c._analyzer.full_map_covering.return_value = True
    assert c._survey_look_at_map() is None
    assert len([call for call in c._execute_key_press.call_args_list if call.args[0] == "m"]) == 2
    c._analyzer.read_full_map.assert_not_called()


def test_the_loop_closes_a_map_left_open_when_no_look_is_in_progress():
    """With the map open the compass is not read, the altitude goes stale and
    the survey commands nothing: it flew 28 s like that on the eleventh flight."""
    c = _survey_ctrl(alt=5000, heading=None)
    c._survey_map_look_every_s = 60.0
    game = _MapGame(c, opened=True)
    _run_briefly(c, 0.4)
    assert not game.open and game.presses == 1


def test_one_sighting_of_the_map_is_not_enough_to_close_it():
    c = _survey_ctrl(alt=5000, heading=None)
    assert c._survey_close_stray_map(0) == 0, "no map: nothing counted"
    c._analyzer.full_map_shown.return_value = True
    assert c._survey_close_stray_map(0) == 1, "seen once: counted, not pressed"
    assert not [call for call in c._execute_key_press.call_args_list if call.args[0] == "m"]


def _looks(c, seconds=0.2):
    c._survey_look_at_map = mock.MagicMock(return_value=None)
    _run_briefly(c, seconds)
    return c._survey_look_at_map.call_count


def test_in_cruise_the_loop_looks_at_the_map_and_no_more_often_than_asked():
    c = _survey_ctrl(alt=5000, heading=0.0)
    c._survey_map_look_every_s = 0.08
    looks = _looks(c, 0.3)
    assert 1 <= looks <= 5, "about one look per 0.08 s of a 0.3 s run, not one per tick"


def test_the_loop_does_not_look_at_the_map_when_looks_are_switched_off():
    assert _looks(_survey_ctrl(alt=5000, heading=0.0)) == 0      # the stub's map_look_every_s is 0


def test_the_loop_does_not_look_at_the_map_while_it_is_turning_or_standing_off():
    """The map covers the altitude readout and the forward view: not off
    heading with a bank on, and not while something else has the aircraft.
    (With no position yet, one first look is taken early: see below.)"""
    from wingman.full_map import FullMapFix
    steering = _survey_ctrl(alt=5000, heading=40.0)
    steering._survey_map_look_every_s = 0.01
    steering._survey_look_at_map = mock.MagicMock(return_value=FullMapFix(east=0.1, north=0.1))
    _run_briefly(steering, 0.3)
    assert steering._survey_look_at_map.call_count == 1, "the first look, and none while steering"
    yielding = _survey_ctrl(alt=5000, heading=0.0)
    yielding._survey_map_look_every_s = 0.01
    yielding._boundary_turning.set()
    assert _looks(yielding) == 0


def test_the_shipped_look_waits_longer_than_the_game_has_been_seen_to_take():
    assert schema_default("survey_mission.map_look_every_s") >= 10
    assert schema_default("survey_mission.map_wait_s") >= 1.5, "0.72 s was not enough on 2026-10-04"
    assert schema_default("survey_mission.map_poll_s") <= 0.2


# ── turning back on the position, not on the minimap's rim ────────────────

def test_a_position_is_carried_along_the_heading_at_the_hud_speed():
    from wingman.survey import carry_position
    one_radius_in_ten_seconds = 9400.0 / 10.0 * 3.6          # KPH
    east, north = carry_position(0.0, 0.0, 90.0, one_radius_in_ten_seconds, 10.0, 9400.0)
    assert (east, north) == (pytest.approx(1.0), pytest.approx(0.0, abs=1e-9))
    east, north = carry_position(0.2, 0.1, 225.0, one_radius_in_ten_seconds, 5.0, 9400.0)
    assert east == pytest.approx(0.2 - 0.5 * 0.7071, abs=1e-3)
    assert north == pytest.approx(0.1 - 0.5 * 0.7071, abs=1e-3)


def test_radial_says_how_far_out_and_whether_the_heading_leads_out():
    from wingman.survey import radial
    assert radial(0.5, 0.0, 90.0) == (pytest.approx(0.5), pytest.approx(1.0))
    assert radial(0.5, 0.0, 270.0)[1] == pytest.approx(-1.0)
    assert radial(0.5, 0.0, 0.0)[1] == pytest.approx(0.0, abs=1e-9)
    assert radial(0.0, 0.0, 123.0) == (0.0, 1.0), "from the centre every heading leads out"


def test_with_a_position_the_pass_turns_back_near_the_real_edge():
    plan = _plan(edge_radius_frac=0.8)
    assert not plan.step(0.0, 5000, 0.0, None, 0.0, 0.0, where=(0.79, 1.0)).new_leg
    cmd = plan.step(1.0, 5000, 0.0, None, 0.0, 0.0, where=(0.81, 1.0))
    assert cmd.new_leg and cmd.state == "reverse" and cmd.target == 180.0


def test_with_a_position_a_pass_heading_inward_is_left_alone_even_at_the_edge():
    plan = _plan(edge_radius_frac=0.8)
    assert not plan.step(0.0, 5000, 0.0, None, 0.0, 0.0, where=(0.95, -0.4)).new_leg


def test_with_a_position_the_minimaps_rim_does_not_turn_the_pass():
    """Eleventh flight, 2026-10-04 17:03: every pass of a battle was turned
    back within 0.35 radii of the arena's centre, on a rim reading made 6 to
    9 km from the edge. Sixteen fixes; 18 percent of the arena flown."""
    plan = _plan()
    cmd = None
    for back in (3.0, 1.5, 0.0):
        d = 0.4 + back * 0.05
        cmd = plan.step(100.0 - back, 5000, 0.0, (d, 0.38 * d / 0.4, 100.0 - back, None),
                        0.0, 0.0, where=(0.30, 1.0))
    assert not cmd.new_leg and plan.leg == 0
    assert _meet(_plan(), 100.0, 5000, 0.0).new_leg, "the same readings, with no position, do turn it"


def test_a_pass_just_turned_back_is_not_turned_again_by_the_position():
    plan = _plan(edge_radius_frac=0.8, edge_holdoff_s=30, turn_max_s=5)
    assert plan.step(0.0, 5000, 0.0, None, 0.0, 0.0, where=(0.85, 1.0)).new_leg
    plan.step(6.0, 5000, 170.0, None, 0.0, 0.0, where=(0.86, -0.9))     # the turn is over
    assert not plan.step(8.0, 5000, 178.0, None, 0.0, 0.0, where=(0.85, 0.2)).new_leg
    assert plan.step(40.0, 5000, 180.0, None, 0.0, 0.0, where=(0.85, 1.0)).new_leg


def test_the_trees_boundary_turn_defers_to_the_surveys_position():
    """The tree reads the same rim as the survey did, and its boundary turn was
    the selected tactic on every tick of the eleventh flight's battle."""
    c = _survey_ctrl(alt=5000, heading=0.0, boundary=(0.1, 0.09, time.time()))
    c._surveying.set()
    assert c._survey_refuses_boundary_turn() is False, "no position: the rim reading decides"
    c._survey_radial = (0.30, 1.0, time.time())
    assert c._survey_refuses_boundary_turn() is True, "0.30 radii from the centre is not the edge"
    c._survey_radial = (0.95, 1.0, time.time())
    assert c._survey_refuses_boundary_turn() is False, "really at the edge: the last resort stands"
    c._survey_radial = (0.30, 1.0, time.time() - 10.0)
    assert c._survey_refuses_boundary_turn() is False, "an old position is not one"


def test_the_loop_carries_its_position_from_the_fix_and_turns_back_at_the_edge(caplog):
    from wingman.full_map import FullMapFix
    c = _survey_ctrl(alt=5000, heading=90.0)
    c._survey_cfg = dict(c._survey_cfg, heading_deg=90, edge_radius_frac=0.8, edge_holdoff_s=0)
    c._survey_map_look_every_s = 60.0
    # One fix, 0.79 radii east of the centre, heading east at the stub's 900 KPH:
    # 0.027 radii a second on a 9.4 km arena, so the edge at 0.8 is 0.4 s away.
    c._survey_look_at_map = mock.MagicMock(return_value=FullMapFix(east=0.79, north=0.0))
    with caplog.at_level("INFO", logger="wingman.controller"):
        _run_briefly(c, 1.2)
    turned = [r.getMessage() for r in caplog.records if "edge ahead" in r.getMessage()]
    assert turned and "by position, r=0.8" in turned[0]
    assert c._survey_look_at_map.call_count == 1


# ── a lane's width between passes ─────────────────────────────────────────

def _at_the_north_edge(plan, east=0.0):
    """Flying 000 and reaching 0.8 radii north of the centre, with a position."""
    return plan.step(0.0, 5000, 0.0, None, 0.0, 0.0, where=(0.8, 1.0), position=(east, 0.8))


def test_with_a_position_a_turn_back_first_steps_across_towards_the_sweep_side():
    """Thirteenth flight, 2026-10-04 17:38: three passes each 1.5 radii long lay
    0.05 to 0.2 radii apart, and six minutes of them covered 21 percent."""
    plan = _plan(lane_spacing_frac=0.3)
    cmd = _at_the_north_edge(plan)
    assert cmd.new_leg and cmd.state == "reverse" and cmd.roll == "right"
    assert cmd.target == 90.0, "passes on 000 and 180 moving right: the step across is flown east"
    assert cmd.then == 180.0, "and the pass after it is the one coming back"


def test_the_step_across_is_flown_until_the_next_lane_and_then_the_turn_is_finished():
    plan = _plan(lane_spacing_frac=0.3, turn_release_deg=25)
    _at_the_north_edge(plan)
    # Round to east: the first half of the turn is over, and the step is flown.
    assert plan.step(5.0, 5000, 88.0, None, 0.0, 0.0, where=(0.81, 0.1), position=(0.05, 0.81)).state == "cross"
    assert plan.step(9.0, 5000, 90.0, None, 0.0, 0.0, where=(0.84, 0.3), position=(0.25, 0.80)).state == "cross"
    over = plan.step(11.0, 5000, 90.0, None, 0.0, 0.0, where=(0.86, 0.4), position=(0.31, 0.80))
    assert over.state == "reverse" and over.roll == "right" and over.target == 180.0
    assert not over.new_leg, "the same turn back, not another pass"
    done = plan.step(16.0, 5000, 178.0, None, 0.0, 0.0, where=(0.80, -0.9), position=(0.33, 0.73))
    assert done.state == "cruise" and done.target == 180.0


def test_a_step_across_that_runs_into_the_edge_is_cut_short_and_the_lanes_go_back():
    plan = _plan(lane_spacing_frac=0.3, cross_edge_radius_frac=0.9, lane_limit_frac=1.0)
    _at_the_north_edge(plan, east=0.35)
    plan.step(5.0, 5000, 90.0, None, 0.0, 0.0, where=(0.88, 0.5), position=(0.40, 0.79))
    assert plan.sweep_side == 1
    cut = plan.step(7.0, 5000, 90.0, None, 0.0, 0.0, where=(0.91, 0.6), position=(0.47, 0.78))
    assert cut.state == "reverse" and cut.target == 180.0
    assert plan.sweep_side == -1, "the arena ran out to the east: the next step across goes west"


def test_a_step_across_is_given_up_after_its_time():
    plan = _plan(lane_spacing_frac=0.3, cross_max_s=20)
    _at_the_north_edge(plan)
    assert plan.step(19.0, 5000, 90.0, None, 0.0, 0.0, where=(0.8, 0.0), position=(0.02, 0.8)).state == "cross"
    assert plan.step(21.0, 5000, 90.0, None, 0.0, 0.0, where=(0.8, 0.0), position=(0.02, 0.8)).target == 180.0


def test_with_no_lane_spacing_the_turn_goes_straight_round():
    plan = _plan(lane_spacing_frac=0.0)
    cmd = _at_the_north_edge(plan)
    assert cmd.new_leg and cmd.target == 180.0 and cmd.then is None


def test_without_a_position_the_turn_goes_straight_round():
    cmd = _meet(_plan(lane_spacing_frac=0.3), 100.0, 5000, 0.0)
    assert cmd.new_leg and cmd.target == 180.0 and cmd.then is None


def test_the_loop_looks_at_the_map_on_the_step_across_too(caplog):
    """The step is measured on the position, so the position has to stay fresh."""
    from wingman.full_map import FullMapFix
    c = _survey_ctrl(alt=5000, heading=90.0)
    c._survey_cfg = dict(c._survey_cfg, heading_deg=0, edge_radius_frac=0.8, edge_holdoff_s=0,
                         lane_spacing_frac=0.3, turn_release_deg=25)
    c._survey_map_look_every_s = 0.3
    # Already at the north edge heading east, which is the step across for
    # passes on 000 and 180: the first fix starts the turn back, and the
    # aircraft is on the cross heading at once.
    c._survey_look_at_map = mock.MagicMock(return_value=FullMapFix(east=0.0, north=0.81))
    c._analyzer.read_compass_heading.return_value = 0.0
    threading.Timer(0.25, lambda: setattr(c._analyzer.read_compass_heading, "return_value", 90.0)).start()
    with caplog.at_level("INFO", logger="wingman.controller"):
        _run_briefly(c, 1.2)
    lines = [r.getMessage() for r in caplog.records]
    assert any("after a step across on 090" in line for line in lines)
    assert any("SURVEY: state=cross" in line for line in lines)
    assert c._survey_look_at_map.call_count >= 2


def test_with_no_position_yet_the_first_look_is_taken_in_the_climb():
    """Thirteenth flight, 2026-10-04 17:46: a battle's first two passes were
    turned back on the minimap's rim, 10 and 40 s in, before its first look."""
    c = _survey_ctrl(alt=2000, heading=0.0)          # below the band: a climb
    c._survey_map_look_every_s = 60.0
    c._survey_map_first_look_s = 0.05
    assert _looks(c, 0.3) >= 1


def test_once_there_is_a_position_looks_keep_to_their_own_time_and_to_settled_flight():
    from wingman.full_map import FullMapFix
    c = _survey_ctrl(alt=2000, heading=0.0)          # still climbing
    c._survey_map_look_every_s = 60.0
    c._survey_map_first_look_s = 0.05
    c._survey_look_at_map = mock.MagicMock(return_value=FullMapFix(east=0.1, north=0.1))
    _run_briefly(c, 0.5)
    assert c._survey_look_at_map.call_count == 1, "one fix, and no more looks in the climb"


# ── the step across goes to the side that has room ────────────────────────

def test_the_step_across_goes_the_other_way_when_the_sweep_side_has_no_room():
    """Fourteenth flight, 2026-10-04 18:16: at the north-west rim the step was
    made further out, and the pass after it lay along the rim."""
    plan = _plan(lane_spacing_frac=0.3, lane_limit_frac=0.6)
    cmd = plan.step(0.0, 5000, 0.0, None, 0.0, 0.0, where=(0.8, 1.0), position=(0.5, 0.62))
    assert cmd.new_leg and cmd.target == 270.0 and cmd.roll == "left", "0.5 east already: step west"
    assert cmd.then == 180.0 and plan.sweep_side == -1


@pytest.mark.parametrize("east", [0.0, 0.29, -0.5])
def test_the_step_across_keeps_to_the_sweep_side_while_it_has_room(east):
    plan = _plan(lane_spacing_frac=0.3, lane_limit_frac=0.6)
    north = math.sqrt(0.8 ** 2 - east ** 2)
    cmd = plan.step(0.0, 5000, 0.0, None, 0.0, 0.0, where=(0.8, 1.0), position=(east, north))
    assert cmd.target == 90.0 and plan.sweep_side == 1


def test_with_the_compass_unread_the_loop_never_presses_the_map_key():
    """Fifteenth flight, 2026-10-04 18:50:24: the game's scoreboard menu was on
    screen, so the compass was unread; a look was allowed anyway, `m` was
    pressed into the menu, and the match was exited two seconds later. The
    compass read on the same tick is the proof that the flight HUD is up."""
    for alt in (5000, 2000):                              # in cruise, and in the climb
        c = _survey_ctrl(alt=alt, heading=None)
        c._survey_map_look_every_s = 0.01
        c._survey_map_first_look_s = 0.01
        assert _looks(c, 0.3) == 0
        assert not [call for call in c._execute_key_press.call_args_list if call.args[0] == "m"]


def test_the_shipped_turn_pulls_only_to_hold_up_a_sinking_turn():
    """Fourteenth flight, 2026-10-04 18:30: the pull at the end of a lane change
    took the aircraft up 970 m in 8 s to a stall, and it did not recover from
    the dive that followed. Pulling does not make the turn faster."""
    plan = SurveyPlan({})
    assert plan.turn_climb_max_ms < 0
    assert not plan._may_pull(0.0) and not plan._may_pull(15.0), "level or climbing: bank only"
    assert plan._may_pull(-60.0), "sinking fast: a pull holds it up"


def test_the_shipped_edge_margins_keep_the_trees_boundary_turn_as_a_last_resort():
    assert schema_default("survey_mission.edge_radius_frac") < schema_default(
        "survey_mission.cross_edge_radius_frac") < schema_default(
        "survey_mission.edge_backstop_radius_frac") < 1.0

