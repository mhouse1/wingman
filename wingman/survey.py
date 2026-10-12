"""The survey flight's decisions (Design 017, phase 4b).

A survey flies an arena in straight passes so the pictures can be turned into a
map. It needs no position: it holds an altitude, holds a compass heading, and
turns back when the arena edge comes up ahead.

    leg 0 on `heading_deg`, leg 1 on the opposite heading, leg 2 on the first...

The next battle starts `heading_step_deg` further round (`battle_heading`), so a
session's passes cross each other and are not all the same lanes.

Each reversal turns towards the same compass side (`sweep`, measured from the
first leg's heading), which means right at one end and left at the other. That
moves every pass over by the width of the turn, so the passes lie side by side
across the arena. With a position from the full map the next pass is put a
whole lane over instead: half the turn, a step across the passes, the other
half (`lane_spacing_frac`). Turning the same HAND at both ends would fly a racetrack over
the same two lanes instead. When two legs in a row are short the arena has run
out on that side, and the sweep goes back the other way.

This module only decides. `Controller.mission_survey` reads the instruments,
calls `SurveyPlan.step` and presses the keys, so everything here can be tested
without an aircraft. Standard library only.
"""

import math
from dataclasses import dataclass

from .config_schema import schema_default


def _d(key: str):
    return schema_default(f"survey_mission.{key}")


def heading_error(target: float, heading: float) -> float:
    """Degrees to turn from `heading` to `target`, -180 to 180. Positive is clockwise (right)."""
    return (target - heading + 180.0) % 360.0 - 180.0


def battle_heading(first: float, step: float, battle: int) -> float:
    """The first pass's heading for a session's `battle`-th battle, counted from 0.

    Every battle on one heading flies one set of parallel lanes, and terrain
    seen only from one direction does not come out right (Design 017, the
    survey footage check of 2026-10-04: 27 mission starts, all on 000 and 180).
    Turning the heading by `step` each battle gives the crossing passes.
    """
    return (first + step * max(battle, 0)) % 360.0


def carry_position(east: float, north: float, heading: float, kph: float, seconds: float,
                   arena_radius_m: float) -> "tuple[float, float]":
    """A position in arena radii, carried along `heading` at `kph` for `seconds`.

    Between two looks at the full map the survey knows its heading and speed,
    and that is enough for the fifteen seconds between them: the fixes of the
    eleventh flight (2026-10-04) moved 0.15 to 0.31 radii per look, and HUD
    speed over that put the arena's radius at 8.8 to 10.3 km on eight pairs.
    """
    step = kph / 3.6 * seconds / arena_radius_m
    return (east + step * math.sin(math.radians(heading)),
            north + step * math.cos(math.radians(heading)))


def radial(east: float, north: float, heading: float) -> "tuple[float, float]":
    """(distance from the arena's centre in radii, how far outward the heading
    points: 1 straight at the edge, 0 along it, -1 straight at the centre)."""
    r = math.hypot(east, north)
    if r < 1e-6:
        return 0.0, 1.0             # at the centre every heading leads outward
    out = (east * math.sin(math.radians(heading)) + north * math.cos(math.radians(heading))) / r
    return r, out


def first_battle_index(now: float, step: float) -> int:
    """Where in the round of headings a session starts, taken from the clock.

    A session that always began on the first heading would only ever fly the
    first one or two when it is stopped and started again every few battles,
    which is what working on the flight does.
    """
    if step <= 0:
        return 0
    return int(now // 60) % max(1, round(360.0 / step))


@dataclass(frozen=True)
class SurveyCommand:
    """What to do for one tick. `state` names the branch, for the log."""

    state: str                      # "no-altitude", "climb", "reverse", "not-level", "steer", "cruise", "cross", "no-heading"
    target: float                   # the leg's compass heading
    leg: int
    roll: "str | None" = None       # "left" or "right": bank that way
    pull: bool = False              # nose up with the bank, which is what turns the flight path
    pitch: "str | None" = None      # "up" or "down": one press of `pitch_s` seconds
    pitch_s: float = 0.0
    new_leg: bool = False           # this tick turned back at the edge
    then: "float | None" = None     # with new_leg: the pass heading that follows a step across
    error: "float | None" = None    # heading error, degrees, when the heading was read


class SurveyPlan:
    """Keeps the leg, its heading and the turn in progress."""

    def __init__(self, cfg: "dict | None" = None) -> None:
        cfg = cfg or {}

        def get(key):
            return cfg.get(key, _d(key))

        self.target_alt = float(get("target_alt"))
        self.hysteresis_m = float(get("hysteresis_m"))
        self.altitude_deadband_m = float(get("altitude_deadband_m"))
        self.level_band_deg = float(get("level_band_deg"))
        self.climb_rate_ms = float(get("climb_rate_ms"))
        self.descent_rate_ms = float(get("descent_rate_ms"))
        self.alt_tau_s = float(get("alt_tau_s"))
        self.rate_deadband_ms = float(get("rate_deadband_ms"))
        self.rate_per_press_s = float(get("rate_per_press_s"))
        self.rate_gain = float(get("rate_gain"))
        self.pitch_interval_s = float(get("pitch_interval_s"))
        self.press_min_s = float(get("press_min_s"))
        self.press_max_s = float(get("press_max_s"))
        self.turn_climb_max_ms = float(get("turn_climb_max_ms"))
        self.takeover_alt_m = float(get("takeover_alt_m"))
        self.first_heading = float(get("heading_deg")) % 360.0
        self.sweep_side = 1 if str(get("sweep")) == "right" else -1
        self.heading_deadband_deg = float(get("heading_deadband_deg"))
        self.heading_release_deg = float(get("heading_release_deg"))
        self.edge_holdoff_s = float(get("edge_holdoff_s"))
        self.turn_release_deg = float(get("turn_release_deg"))
        self.turn_max_s = float(get("turn_max_s"))
        self.edge_turn_frac = float(get("edge_turn_frac"))
        self.edge_cone_cos = float(get("edge_cone_cos"))
        self.edge_close_frac = float(get("edge_close_frac"))
        self.edge_closing_frac = float(get("edge_closing_frac"))
        self.edge_radius_frac = float(get("edge_radius_frac"))
        self.lane_spacing_frac = float(get("lane_spacing_frac"))
        self.cross_max_s = float(get("cross_max_s"))
        self.cross_edge_radius_frac = float(get("cross_edge_radius_frac"))
        self.lane_limit_frac = float(get("lane_limit_frac"))
        self.boundary_max_age_s = float(get("boundary_max_age_s"))
        self.min_leg_s = float(get("min_leg_s"))
        self.target = self.first_heading
        self.leg = 0
        self._leg_started: "float | None" = None
        self._short_legs = 0
        self._turn_dir: "str | None" = None     # set while a reversal is being flown
        self._turn_started = 0.0
        self._last_pitch_press = float("-inf")
        self._pressed_reading_ts: "float | None" = None    # the altitude reading the last press acted on
        # While a step across to the next lane is flown: (east and north where
        # it began, its heading, the pass heading that follows, when to give up).
        self._cross: "tuple | None" = None
        self._last_reverse = float("-inf")
        self._edge_seen: list = []      # the last few (ts, dist) readings of the rim
        self._steering = False          # a heading correction is being flown
        self._off_heading_reads = 0     # consecutive reads outside the deadband

    # ------------------------------------------------------------------
    def _edge_ahead(self, now: float, boundary) -> "str | None":
        """Why to turn back now: "ahead", "close", or None.

        `boundary` is (dist, forward, ts) or (dist, forward, ts, lateral), in
        minimap radii, for the NEAREST point of the rim.
        """
        if not boundary:
            return None
        dist, forward, ts = boundary[:3]
        if dist is None or forward is None or now - ts > self.boundary_max_age_s:
            return None
        if not self._edge_seen or ts - self._edge_seen[-1][0] >= 1.0:
            self._edge_seen = [r for r in self._edge_seen if ts - r[0] <= 8.0][-2:] + [(ts, dist)]
        if now - self._last_reverse < self.edge_holdoff_s:
            # Just turned round: the edge is still close, and for part of the
            # turn still ahead. Second flight, 2026-10-04: the pass was turned
            # round again 17 s after the last time, in the middle of the turn.
            return None
        # The rim has to be getting NEARER, over three readings. Which side of
        # abeam its nearest point lies on is not steady enough to act on: at
        # the sixth flight's spawn (10:57:54 to 10:58:03) the aircraft was
        # flying away from the rim, 0.30 radii growing to 0.38, while the
        # forward part of the reading went -0.08, -0.15, -0.20, +0.22, -0.22,
        # +0.27, and the last of those turned it back at the rim it had left.
        if len(self._edge_seen) < 3 or (
                self._edge_seen[0][1] - self._edge_seen[-1][1] < self.edge_closing_frac):
            return None
        if forward <= 0.0 or dist <= 0.0:
            return None
        # In front means within a cone about the nose, not merely forward of
        # abeam: on the fifth flight five of six turns back were made with the
        # rim's nearest point 64 to 85 degrees off the nose, flying along it.
        if forward >= self.edge_cone_cos * dist and dist <= self.edge_turn_frac:
            return "ahead"
        # Far from the arena's middle a pass meets the rim at a slant and the
        # cone never fills. There the rim being close, and closing, turns it.
        if dist <= self.edge_close_frac:
            return "close"
        return None

    def _edge_by_position(self, now: float, where) -> "str | None":
        """Why to turn back now, from a position: "edge", or None.

        `where` is (distance from the arena's centre in radii, outwardness of
        the heading) from `radial`. The pass is turned back when it is near
        the edge and still heading out.

        This replaces the minimap's rim whenever a position is known. On the
        eleventh flight (2026-10-04 17:03) sixteen fixes showed every pass
        turned back within 0.35 radii of the centre, on a rim reading of 0.3
        to 0.7 minimap radii made 6 to 9 km from the arena's edge: whatever
        the reader takes for the rim there, it is not the edge.
        """
        if now - self._last_reverse < self.edge_holdoff_s:
            return None
        r, outward = where
        return "edge" if r >= self.edge_radius_frac and outward > 0.0 else None

    def _cross_done(self, now: float, where, position) -> bool:
        """The step across has gone far enough, or cannot go further."""
        east0, north0, cross, _new_pass, deadline = self._cross
        if now >= deadline:
            return True
        if position is not None:
            along = ((position[0] - east0) * math.sin(math.radians(cross))
                     + (position[1] - north0) * math.cos(math.radians(cross)))
            if along >= self.lane_spacing_frac:
                return True
        if (where is not None and where[0] >= self.cross_edge_radius_frac
                and where[1] > 0.0):
            # The arena has run out on this side: the lanes go back the other way.
            self.sweep_side = -self.sweep_side
            return True
        return False

    def _reverse(self, now: float, heading: "float | None", why: str = "ahead",
                 lateral: "float | None" = None, position=None) -> None:
        if self._leg_started is not None and now - self._leg_started < self.min_leg_s:
            self._short_legs += 1
        else:
            self._short_legs = 0
        if self._short_legs >= 2:
            # Two short passes running: the arena has run out on this side.
            self.sweep_side = -self.sweep_side
            self._short_legs = 0
        # Which of the two pass headings the aircraft is flying towards the
        # edge on. Usually the pass it was given, but not always: on the third
        # flight (2026-10-04 10:01) a climb had taken it over the top and it
        # met the edge heading 180 with the pass still set to 000. Turning the
        # pass round blindly set it to 180, the way it was already going.
        flown = self.target
        if heading is not None:
            opposite = (self.target + 180.0) % 360.0
            if abs(heading_error(opposite, heading)) < abs(heading_error(self.target, heading)):
                flown = opposite
        if why == "edge" and position is not None and self.lane_spacing_frac > 0.0:
            # Step to the side that has room. Where this lane lies along the
            # sweep's axis is known from the position; if a lane's width more
            # would put the next pass beyond `lane_limit_frac`, there is only
            # rim out there, and the lanes go back the other way first. On the
            # fourteenth flight (2026-10-04 18:16) a step made outward at the
            # north-west rim led onto a pass that lay along the rim, 0.87
            # radii out at its nearest, and was turned back again 30 s later.
            side = math.radians((self.first_heading + 90.0 * self.sweep_side) % 360.0)
            lane = position[0] * math.sin(side) + position[1] * math.cos(side)
            if lane + self.lane_spacing_frac > self.lane_limit_frac:
                self.sweep_side = -self.sweep_side
        # Turn through the sweep's compass side: clockwise when that side is
        # 90 degrees clockwise of the heading just flown, otherwise the other way.
        sweep_heading = (self.first_heading + 90.0 * self.sweep_side) % 360.0
        clockwise = abs(heading_error(sweep_heading, (flown + 90.0) % 360.0)) < 1.0
        self._turn_dir = "right" if clockwise else "left"
        if why == "close" and lateral:
            # Met at a slant, with the rim alongside: turn away from it,
            # whichever side the passes are moving to. A turn towards it goes
            # outside the arena.
            self._turn_dir = "left" if lateral > 0 else "right"
        self._turn_started = now
        self._last_reverse = now
        self._steering = False
        self._off_heading_reads = 0
        new_pass = (flown + 180.0) % 360.0
        self._cross = None
        if why == "edge" and position is not None and self.lane_spacing_frac > 0.0:
            # With a position the next pass is put a lane's width over, not
            # just a turn's width: half a turn, a step across the passes
            # towards the sweep side, then the other half. On the thirteenth
            # flight (2026-10-04 17:38) three passes each 1.5 radii long lay
            # 0.05 to 0.2 radii apart, almost on top of each other.
            cross = sweep_heading
            self._cross = (position[0], position[1], cross, new_pass, now + self.cross_max_s)
            self.target = cross
        else:
            self.target = new_pass
        self.leg += 1
        self._leg_started = now

    # ------------------------------------------------------------------
    def step(self, now: float, alt: "float | None", heading: "float | None",
             boundary=None, pitch_deg: "float | None" = None,
             alt_rate: "float | None" = None, alt_ts: "float | None" = None,
             where=None, position=None) -> SurveyCommand:
        """One tick's command from the altitude, the compass heading, the boundary
        reading and, when they are known, the nose angle and the climb rate.
        `alt_ts` is when the altitude was read, so one reading gets one press.
        `where` is the position as `radial` gives it; with one, the edge is
        judged from it and the minimap's rim reading is not consulted.
        `position` is the same position as (east, north), which lets a turn
        back put the next pass a lane's width over."""
        if self._leg_started is None:
            self._leg_started = now

        # The edge first, whatever else is going on. On the first flight
        # (2026-10-04 09:22) the edge was only looked at in level flight, the
        # aircraft never left its climb, and it flew out of the arena.
        new_leg = False
        why = None
        if self._cross is None:
            # A step across runs along the edge and has its own limit there
            # (`_cross_done`); the edge rule would take it for a new pass.
            why = (self._edge_by_position(now, where) if where is not None
                   else self._edge_ahead(now, boundary))
        then = None
        if self._turn_dir is None and why is not None:
            lateral = boundary[3] if boundary and len(boundary) > 3 else None
            self._reverse(now, heading, why, lateral, position)
            new_leg = True
            then = None if self._cross is None else self._cross[3]
        elif (self._cross is not None and self._turn_dir is None
              and self._cross_done(now, where, position)):
            # Far enough across: the second half of the turn, the same way
            # round, onto the new pass.
            cross, new_pass = self._cross[2], self._cross[3]
            self._cross = None
            self.target = new_pass
            self._turn_dir = "right" if heading_error(new_pass, cross) > 0 else "left"
            self._turn_started = now
            self._steering = False
            self._off_heading_reads = 0

        error = None if heading is None else heading_error(self.target, heading)
        if self._turn_dir is not None:
            done = error is not None and abs(error) <= self.turn_release_deg
            if done or now - self._turn_started > self.turn_max_s:
                self._turn_dir = None
            else:
                # Flown without an altitude, below the band, and when the
                # heading is not read: a turn that stops half way leaves the
                # aircraft heading for the edge.
                return SurveyCommand("reverse", self.target, self.leg, roll=self._turn_dir,
                                     pull=self._may_pull(alt_rate), new_leg=new_leg,
                                     error=error, then=then)

        if alt is None:
            # No fresh altitude: command nothing. A climb or a heading
            # correction ordered on a stale reading is ordered blind.
            return SurveyCommand("no-altitude", self.target, self.leg, new_leg=new_leg)
        # Pitch, in every state that is not a turn: one press sized to how far
        # the climb rate is from the one wanted, then a wait to see what it did.
        pitch, pitch_s = self._pitch_press(now, alt, alt_rate, alt_ts)

        if pitch_deg is not None and abs(pitch_deg) > self.level_band_deg:
            # Nose well up or well down: no bank until it is back near level.
            # A bank now is how a dive or a zoom becomes a departure (ADR 114),
            # and the compass swings with the nose, so the heading error it
            # shows is not one to steer on. The pitch press above is what
            # brings it back.
            return SurveyCommand("not-level", self.target, self.leg, pitch=pitch,
                                 pitch_s=pitch_s, new_leg=new_leg, error=error)
        if alt < self.target_alt - self.hysteresis_m:
            return SurveyCommand("climb", self.target, self.leg, pitch=pitch, pitch_s=pitch_s,
                                 new_leg=new_leg, error=error)
        if error is None:
            return SurveyCommand("no-heading", self.target, self.leg, pitch=pitch,
                                 pitch_s=pitch_s, new_leg=new_leg)
        # Start a correction on two reads in a row outside the deadband, and
        # fly it until the heading is inside the tighter release band. One
        # read is not enough: on the second flight single reads jumped 15 to
        # 20 degrees and back while the aircraft was on heading, and a
        # correction started and stopped on every one of them.
        if abs(error) > self.heading_deadband_deg:
            self._off_heading_reads += 1
        else:
            self._off_heading_reads = 0
        if self._steering and abs(error) <= self.heading_release_deg:
            self._steering = False
        elif not self._steering and self._off_heading_reads >= 2:
            self._steering = True
        if self._steering:
            # Bank always. Pull only while the aircraft is neither above its
            # band nor already climbing: a bank with a pull climbs, and a bank
            # without one sinks, which brings it back down.
            pull = (alt <= self.target_alt + self.altitude_deadband_m
                    and self._may_pull(alt_rate))
            return SurveyCommand("steer", self.target, self.leg,
                                 roll="right" if error > 0 else "left", pull=pull,
                                 new_leg=new_leg, error=error)
        return SurveyCommand("cruise" if self._cross is None else "cross", self.target, self.leg,
                             pitch=pitch, pitch_s=pitch_s, new_leg=new_leg, error=error)

    # ------------------------------------------------------------------
    def _may_pull(self, alt_rate: "float | None") -> bool:
        """A turn pulls only while it is not already climbing.

        The tree's boundary turn holds the pull for its whole 12 s: on the
        fourth flight (2026-10-04 10:23) that took the nose to 84 degrees up
        and the aircraft over the top. A turn that banks and pulls only while
        the climb rate is small stays near level.
        """
        return alt_rate is None or alt_rate < self.turn_climb_max_ms

    def _pitch_press(self, now: float, alt: float, alt_rate: "float | None",
                     alt_ts: "float | None" = None):
        """("up" | "down" | None, seconds): the press that moves the climb rate towards the one wanted.

        This aircraft keeps the nose where a press leaves it. Presses on a
        timer therefore add up: nine nose-down presses made a 74 degree dive
        (second flight) and nose-up presses a vertical zoom (third). So the
        wanted climb rate comes from how far the altitude is from the target,
        the press is sized to the gap between that and the measured rate, and
        nothing is pressed again until the reading has had time to show what
        the last press did. With no rate reading, nothing is pressed, except
        low down just after a spawn.

        A press corrects `rate_gain` of the gap, not all of it, and one
        altitude reading gets one press. The readings land every 3 s, so a rate
        is 1.5 to 4.5 s old when it is acted on. Correcting the whole gap on
        that, sometimes twice on the same reading, over-corrects: on the
        eighth flight (2026-10-04, 98 passes) the altitude was still swinging
        by a median 499 m from 35 s into a pass on, long after the turn.
        """
        if alt_rate is None and alt < self.takeover_alt_m:
            # Just spawned, low, and the telemetry has no rate yet: climb
            # anyway. On the fifth flight (2026-10-04 10:41) the aircraft sat
            # at 550 m for ten seconds and then turned into the terrain.
            alt_rate = 0.0
        if alt_rate is None or now - self._last_pitch_press < self.pitch_interval_s:
            return None, 0.0
        if alt_ts is not None and alt_ts == self._pressed_reading_ts:
            return None, 0.0        # this reading has had its press; the next shows what it did
        wanted = (self.target_alt - alt) / self.alt_tau_s
        wanted = max(-self.descent_rate_ms, min(self.climb_rate_ms, wanted))
        gap = wanted - alt_rate
        if abs(gap) <= self.rate_deadband_ms:
            return None, 0.0
        seconds = max(self.press_min_s, min(
            self.press_max_s, self.rate_gain * abs(gap) / self.rate_per_press_s))
        self._last_pitch_press = now
        self._pressed_reading_ts = alt_ts
        return ("up" if gap > 0 else "down"), seconds
