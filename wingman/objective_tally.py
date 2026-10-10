"""How many objectives wingman flew through, by round and over the session.

Operator, 2026-10-09: "at round end it prints how many air superiority
targets, resupply, and priority targets are captured ... it should not read
the score bar, only track when wingman flies through the targets." And later
that day: "i only want it to print total counts during wingman session
summary." The session's totals are what is printed, in the Wingman Session
Summary; the round's line is kept at DEBUG. The next day: "when one of the
objectives are flown I want it to print a block of green text", so each count
also prints the summary's block with the totals so far, in green.

Nothing here looks at the game's own account of who holds what. A capture is
counted from what the pursuit already sees.

For an air superiority point and the priority target that is the marker, which
the game draws larger the nearer the aircraft is.

An air superiority point (operator, 2026-10-09: "the evidence for
airsuperiority marker fly through should be based on if the marker reached
large size then disappeared"): its marker was followed, reached close-range
size, and was then gone. Where on the screen it went does not matter. A point
that is taken turns blue and stops being found wherever it is, and one flown
through leaves by the edge.

The priority target: its marker was followed until it was last seen at close
range and ahead of the nose, and then was gone. Lost at close range out at the
side of the screen, it was passed beside. Either kind's marker lost while
still small was turned away from, or hidden.

Gone means looked for and not found. A kind the pursuit stops scanning for
(the control points, with every rack empty) has its approach dropped, not
counted.

For the resupply point it is the ammo count going up, which only flying
through the point gives. Its marker is not used: in the first live sessions,
2026-10-09, the marker's size at last sight was wrong both ways. A rearm
followed a marker lost dead ahead at 25 px across, and no rearm followed
markers lost dead ahead at 56 px and at 51 px.

No I/O beyond the log line, and no clock of its own: the pursuit passes the time.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from .config_schema import schema_default
from .mission_stats import objectives_flown_lines

logger = logging.getLogger(__name__)

_GREEN, _RESET = "\033[92m", "\033[0m"

AIR_SUPERIORITY = "air_superiority"
RESUPPLY = "resupply"
PRIORITY_TARGET = "priority_target"

# What each is called in the round line, in its order.
_NAMES = {
    AIR_SUPERIORITY: "air superiority points",
    RESUPPLY: "resupply",
    PRIORITY_TARGET: "priority targets",
}
# The kinds counted from their marker. The resupply point is counted from its rearm.
_BY_MARKER = (AIR_SUPERIORITY, PRIORITY_TARGET)

# The disc's size, in px at 1200 px of frame height, from which the aircraft
# counts as at the objective: `pursuit_mode.objective_tally.near_px`. Measured
# on 2,240 archived frames, the discs are 19 to 25 px (control point) and 14
# to 20 px (crown) across at the distances a pursuit usually sees them, and 56
# and 39 px at the nearest caught. The defaults sit between the two. Named
# guesses: no frame of the moment of passing through was available to measure.
NEAR_PX = {kind: float(schema_default("pursuit_mode.objective_tally.near_px." + kind))
           for kind in _BY_MARKER}
# How far from the screen centre, in the same px, the crown's marker may be
# when it is last seen: `pursuit_mode.objective_tally.centre_px`. A crown
# flown at stays ahead of the nose to the end; one passed beside slides to the
# edge. First live session, 2026-10-09: the crown the aircraft passed at 51 px
# across was last seen 562 px out (440 to 562 px over its last second), and
# the two crowns it flew at were last seen 55 and 220 px out. Named guess
# between the two.
CENTRE_PX = float(schema_default("pursuit_mode.objective_tally.centre_px"))
# A marker not seen for this long is gone (the pursuit's own marker memory
# bridges 0.5 s of missed scans).
LOST_S = 1.0
# One scan is not an approach: this many sightings, over this long.
MIN_SIGHTINGS = 3
MIN_FOLLOWED_S = 0.5
# After a fly-through the same kind does not count again this soon: the disc
# can show once more as the aircraft passes.
REFRACTORY_S = 5.0


@dataclass
class _Approach:
    first_ts: float
    last_ts: float
    sightings: int
    last_px: float
    max_px: float
    last_off_px: float


class ObjectiveTally:
    """Counts fly-throughs per kind, for the round and for the session. Not
    thread-safe by itself: the pursuit loop is its one writer, and the round
    line and the session's totals are read under the controller's own lock."""

    def __init__(self, near_px: "dict | None" = None,
                 centre_px: "float | None" = None) -> None:
        self._near_px = {kind: float((near_px or {}).get(kind, NEAR_PX[kind]))
                         for kind in _BY_MARKER}
        self._centre_px = CENTRE_PX if centre_px is None else float(centre_px)
        self._session = dict.fromkeys(_NAMES, 0)
        self._start_round()

    def _start_round(self) -> None:
        self._counts = dict.fromkeys(_NAMES, 0)
        self._resupply_sight: "tuple[float, float, float] | None" = None
        self._approach: "dict[str, _Approach]" = {}
        self._counted_ts: "dict[str, float]" = {}
        self._seen_anything = False

    # -- what the pursuit reports -------------------------------------------

    def see(self, kind: str, size_px: float, now: float, off_centre_px: float = 0.0) -> None:
        """This scan found `kind`'s marker in view, `size_px` across and
        `off_centre_px` from the screen centre, both at 1200 px of frame height."""
        self._seen_anything = True
        if kind == RESUPPLY:
            # Not an approach to judge: only what the rearm's line says of it.
            self._resupply_sight = (now, float(size_px), float(off_centre_px))
            return
        approach = self._approach.get(kind)
        if approach is None:
            self._approach[kind] = _Approach(
                now, now, 1, float(size_px), float(size_px), float(off_centre_px))
            return
        approach.last_ts = now
        approach.sightings += 1
        approach.last_px = float(size_px)
        approach.max_px = max(approach.max_px, float(size_px))
        approach.last_off_px = float(off_centre_px)

    def tick(self, now: float) -> "list[str]":
        """Close every approach whose marker has been gone for LOST_S. Returns
        the kinds that were flown through, each already counted and logged."""
        flown = []
        for kind in list(self._approach):
            approach = self._approach[kind]
            if now - approach.last_ts < LOST_S:
                continue
            del self._approach[kind]
            if kind == AIR_SUPERIORITY:
                # Reached the size, at any point of the approach, and then gone.
                near = approach.max_px >= self._near_px[kind]
                ahead = True
                small = "never reached close range (%.0f px)" % self._near_px[kind]
            else:
                near = approach.last_px >= self._near_px[kind]
                ahead = approach.last_off_px <= self._centre_px
                small = "not at close range (%.0f px)" % self._near_px[kind]
            followed = (approach.sightings >= MIN_SIGHTINGS
                        and approach.last_ts - approach.first_ts >= MIN_FOLLOWED_S)
            recent = now - self._counted_ts.get(kind, float("-inf")) < REFRACTORY_S
            if near and ahead and followed and not recent:
                self._counts[kind] += 1
                self._session[kind] += 1
                self._counted_ts[kind] = now
                flown.append(kind)
                if kind == AIR_SUPERIORITY:
                    logger.info(
                        "OBJECTIVE: flew through an air superiority point: its marker "
                        "reached %.0f px across and was gone, last seen at %.0f px and "
                        "%.0f px off the centre (%d this round)", approach.max_px,
                        approach.last_px, approach.last_off_px, self._counts[kind])
                else:
                    logger.info(
                        "OBJECTIVE: flew through %s, %.0f px across and %.0f px off the "
                        "centre at last sight (%d this round)", _singular(kind),
                        approach.last_px, approach.last_off_px, self._counts[kind])
                self._log_flown_block()
            else:
                logger.debug(
                    "OBJECTIVE: %s lost at %.0f px, %.0f px off the centre, largest "
                    "%.0f px, after %d sightings in %.1fs — %s",
                    _singular(kind), approach.last_px, approach.last_off_px,
                    approach.max_px, approach.sightings,
                    approach.last_ts - approach.first_ts,
                    small if not near
                    else "passed beside it (ahead is within %.0f px)" % self._centre_px
                    if not ahead
                    else "not followed long enough" if not followed
                    else "counted a moment ago")
        return flown

    def drop_approaches(self, kinds: "tuple[str, ...] | None" = None) -> None:
        """The pursuit ended (a death, a takeover, the round): a marker in view
        at that moment was not flown through.

        With `kinds`, only theirs: the pursuit has stopped looking for them
        (every rack empty), and a marker nobody looks for is not gone. On
        2026-10-10 05:44:07 the crown was counted a second after its scan was
        switched off, with its marker 55 px across and dead ahead."""
        if kinds is None:
            self._approach.clear()
            return
        for kind in kinds:
            self._approach.pop(kind, None)

    def note_rearm(self, now: float) -> None:
        """The pursuit confirmed a rearm from the ammo count: the resupply
        point was flown through. This is the resupply point's only count."""
        self._seen_anything = True
        self._counts[RESUPPLY] += 1
        self._session[RESUPPLY] += 1
        sight = self._resupply_sight
        if sight is None:
            marker = "its marker not seen this round"
        else:
            marker = ("its marker last seen %.1f s before, %.0f px across and %.0f px "
                      "off the centre" % (now - sight[0], sight[1], sight[2]))
        logger.info("OBJECTIVE: flew through the resupply point, rearm confirmed, %s "
                    "(%d this round)", marker, self._counts[RESUPPLY])
        self._log_flown_block()

    def _log_flown_block(self) -> None:
        """Operator, 2026-10-10: "when one of the objectives are flown I want it
        to print a block of green text, this will allow me to visually see if
        wingman registered it or wrongly registered it while it scrolls." The
        session summary's own block, with the totals so far. Every line carries
        its own color codes, so one shown by itself (grep) is still green."""
        logger.info("\n".join(
            _GREEN + line + _RESET for line in objectives_flown_lines(self._session)))

    def note_round_activity(self) -> None:
        """A pursuit ran, so the round has a line to print even with no
        objective seen."""
        self._seen_anything = True

    # -- the round line -----------------------------------------------------

    def counts(self) -> "dict[str, int]":
        return dict(self._counts)

    def session_counts(self) -> "dict[str, int]":
        """Every fly-through since wingman started, the round in progress
        included. The Wingman Session Summary prints these."""
        return dict(self._session)

    def round_line(self) -> str:
        return "ROUND OBJECTIVES — flown through: " + ", ".join(
            "%s %d" % (name, self._counts[kind]) for kind, name in _NAMES.items())

    def end_round(self) -> "str | None":
        """The round's line, or None when no pursuit ran in it. Starts the
        next round's count either way."""
        line = self.round_line() if self._seen_anything else None
        self._start_round()
        return line


def _singular(kind: str) -> str:
    return {AIR_SUPERIORITY: "an air superiority point",
            PRIORITY_TARGET: "the priority target"}[kind]
