"""HLDD 001 part 2 (Code Review 019 cycle 32): the lowest point a pull-out
started now would reach.

Time to ground (ADR 086) divides the altitude by the descent rate: it assumes
the descent goes on to sea level and ignores how long the pull-out takes. The
nose comes up slower the faster the aircraft goes (measured during the
emergency climbs of 2026-10-17, cycle 21), so a fast dive needs far more height
to level out than a slow one, and the trigger fires too late for it.

With the nose coming up at a constant rate q, the flight path goes from g to 0
in T = |g| / q seconds and the height lost on the way is

    v T (1 - cos g) / g        (g in radians)

On top of that the aircraft keeps descending at v sin|g| for the reaction time:
the reading's age plus the time the climb takes to start.

Standard library only, like `state.py`: the behavior tree and the offline
scorer both import it.
"""

from __future__ import annotations

import math

# Nose-up rate (deg/s) during an emergency climb, by speed: the medians
# measured in cycle 21 (21 deg/s at 400-599 KPH, 8 at 600-799, 7 at 800-999,
# 5 at 1,000-1,199, 3 at 1,200-1,399). Upper bounds in KPH, ascending.
PITCH_RATE_DEG_S = ((600.0, 21.0), (800.0, 8.0), (1000.0, 7.0), (1200.0, 5.0))
PITCH_RATE_FASTEST_DEG_S = 3.0


def pitch_rate_deg_s(speed_kph: float) -> float:
    """The measured nose-up rate for this speed."""
    for upper, rate in PITCH_RATE_DEG_S:
        if speed_kph < upper:
            return rate
    return PITCH_RATE_FASTEST_DEG_S


def pullout_loss_m(speed_kph: float, path_deg: float) -> float:
    """Height lost levelling out from `path_deg` (negative is a dive) at the
    measured nose-up rate; 0 when not descending."""
    if path_deg >= 0.0 or speed_kph <= 0.0:
        return 0.0
    g = math.radians(-path_deg)
    t_level = -path_deg / pitch_rate_deg_s(speed_kph)
    return (speed_kph / 3.6) * t_level * (1.0 - math.cos(g)) / g


def lowest_point_m(altitude_m: float, speed_kph: float, path_deg: float,
                   reaction_s: float) -> float:
    """The altitude at the bottom of a pull-out started `reaction_s` from now."""
    if path_deg >= 0.0:
        return float(altitude_m)
    drop = (speed_kph / 3.6) * math.sin(math.radians(-path_deg)) * max(0.0, reaction_s)
    return float(altitude_m) - drop - pullout_loss_m(speed_kph, path_deg)
