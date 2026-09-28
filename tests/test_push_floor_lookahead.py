"""Cycle 12 (2026-09-27): the icon push floor reads the last altitude reading
carried ahead at the measured descent rate, not the smoothed altitude.

At 20:19:25-28 the smoothed altitude (the mean of the last readings) said 2248 m
and then 1873 m while the HUD read 1929 m and then 1403 m, so push_floor_m
(1500) never refused the push and the jet flew into the ground at 20:19:36.
"""

from wingman.config_schema import schema_default
from wingman.icon_steering import IconSteeringConfig
from wingman.telemetry import TelemetrySignal, TelemetrySnapshot

FLOOR = 1500.0


def _snap(value, rate, stable, *, ts=25.616, taken=27.091, stale_after=6.0):
    return TelemetrySnapshot(
        speed=TelemetrySignal(value=1274, ts=ts, stable_value=1274.0, rate=0.0),
        altitude=TelemetrySignal(value=value, ts=ts, stable_value=stable, rate=rate),
        taken_at_s=taken, stale_after_s=stale_after)


def test_the_2019_dive_is_below_the_floor_before_the_next_reading():
    """20:19:27.09, a push tick: last reading 1929 m at 20:19:25.62, 2287 m
    2.99 s before it, smoothed 2248 m. The next reading was 1403 m."""
    snap = _snap(1929, (1929 - 2287) / 2.988, 2248.3)
    assert snap.altitude.stable_value > FLOOR, "the old input allowed the push"
    assert snap.altitude_ahead(3.0) < FLOOR


def test_level_flight_above_the_floor_is_left_alone():
    assert _snap(1929, 0.0, 1930.0).altitude_ahead(3.0) == 1929.0


def test_a_climb_is_never_projected_upward():
    """Below the floor and climbing is still below the floor."""
    assert _snap(1450, +50.0, 1400.0).altitude_ahead(3.0) == 1450.0


def test_no_rate_yet_means_the_reading_itself():
    assert _snap(1600, None, 1600.0).altitude_ahead(3.0) == 1600.0


def test_a_stale_reading_gives_none():
    assert _snap(1929, -120.0, 2248.3, taken=25.616 + 6.5).altitude_ahead(3.0) is None


def test_the_lookahead_default_lives_in_the_schema():
    assert IconSteeringConfig().push_floor_lookahead_s == schema_default(
        "pursuit_mode.icon_steering.push_floor_lookahead_s") == 3.0
    assert IconSteeringConfig.from_dict({"push_floor_lookahead_s": 1.5}).push_floor_lookahead_s == 1.5
