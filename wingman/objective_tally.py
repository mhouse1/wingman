"""How many objectives wingman flew through in a round.

Operator, 2026-10-09: "at round end it prints how many air superiority
targets, resupply, and priority targets are captured ... it should not read
the score bar, only track when wingman flies through the targets."

Nothing here looks at the game's own account of who holds what. A capture is
counted from what the pursuit already sees: the marker of an objective, which
the game draws larger the nearer the aircraft is. A marker that was followed
until it was last seen at close range and ahead of the nose, and then was
gone, was flown through. A marker lost while still small was turned away from,
or hidden. A marker lost at close range out at the side of the screen was
passed beside.

No I/O beyond the log line, and no clock of its own: the pursuit passes the time.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from .config_schema import schema_default

logger = logging.getLogger(__name__)

AIR_SUPERIORITY = "air_superiority"
RESUPPLY = "resupply"
PRIORITY_TARGET = "priority_target"

# What each is called in the round line, in its order.
_NAMES = {
    AIR_SUPERIORITY: "air superiority points",
    RESUPPLY: "resupply",
    PRIORITY_TARGET: "priority targets",
}

# The disc's size, in px at 1200 px of frame height, from which the aircraft
# counts as at the objective: `pursuit_mode.objective_tally.near_px`. Measured
# on 2,240 archived frames, the discs are 19 to 25 px (control point), 24 to
# 40 px (resupply) and 14 to 20 px (crown) across at the distances a pursuit
# usually sees them, and 56, 68 and 39 px at the nearest caught. The defaults
# sit between the two. Named guesses: no frame of the moment of passing
# through was available to measure.
NEAR_PX = {kind: float(schema_default("pursuit_mode.objective_tally.near_px." + kind))
           for kind in _NAMES}
# How far from the screen centre, in the same px, the marker may be when it is
# last seen: `pursuit_mode.objective_tally.centre_px`. An objective flown
# through stays ahead of the nose to the end; one passed beside slides to the
# edge. First live session, 2026-10-09: the resupply point that was followed
# by a rearm was last seen 61 px from the centre (61 to 110 px over its last
# second), and the crown the aircraft passed at 51 px across was last seen
# 562 px out (440 to 562 px). Named guess between the two.
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
    """Counts fly-throughs per kind for one round. Not thread-safe by itself:
    the pursuit loop is its one writer, and the round line is read under the
    controller's own lock."""

    def __init__(self, near_px: "dict | None" = None,
                 centre_px: "float | None" = None) -> None:
        self._near_px = {kind: float((near_px or {}).get(kind, NEAR_PX[kind]))
                         for kind in _NAMES}
        self._centre_px = CENTRE_PX if centre_px is None else float(centre_px)
        self._start_round()

    def _start_round(self) -> None:
        self._counts = dict.fromkeys(_NAMES, 0)
        self._rearms = 0
        self._approach: "dict[str, _Approach]" = {}
        self._counted_ts: "dict[str, float]" = {}
        self._seen_anything = False

    # -- what the pursuit reports -------------------------------------------

    def see(self, kind: str, size_px: float, now: float, off_centre_px: float = 0.0) -> None:
        """This scan found `kind`'s marker in view, `size_px` across and
        `off_centre_px` from the screen centre, both at 1200 px of frame height."""
        self._seen_anything = True
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
            near = approach.last_px >= self._near_px[kind]
            ahead = approach.last_off_px <= self._centre_px
            followed = (approach.sightings >= MIN_SIGHTINGS
                        and approach.last_ts - approach.first_ts >= MIN_FOLLOWED_S)
            recent = now - self._counted_ts.get(kind, float("-inf")) < REFRACTORY_S
            if near and ahead and followed and not recent:
                self._counts[kind] += 1
                self._counted_ts[kind] = now
                flown.append(kind)
                logger.info(
                    "OBJECTIVE: flew through %s, %.0f px across and %.0f px off the "
                    "centre at last sight (%d this round)", _singular(kind),
                    approach.last_px, approach.last_off_px, self._counts[kind])
            else:
                logger.debug(
                    "OBJECTIVE: %s lost at %.0f px, %.0f px off the centre, after %d "
                    "sightings in %.1fs — %s",
                    _singular(kind), approach.last_px, approach.last_off_px,
                    approach.sightings, approach.last_ts - approach.first_ts,
                    "not at close range (%.0f px)" % self._near_px[kind] if not near
                    else "passed beside it (ahead is within %.0f px)" % self._centre_px
                    if not ahead
                    else "not followed long enough" if not followed
                    else "counted a moment ago")
        return flown

    def drop_approaches(self) -> None:
        """The pursuit ended (a death, a takeover, the round): a marker in view
        at that moment was not flown through."""
        self._approach.clear()

    def note_rearm(self) -> None:
        """The pursuit confirmed a rearm from the ammo count. Kept beside the
        resupply fly-throughs, not added to them: it is the check on them."""
        self._seen_anything = True
        self._rearms += 1

    def note_round_activity(self) -> None:
        """A pursuit ran, so the round has a line to print even with no
        objective seen."""
        self._seen_anything = True

    # -- the round line -----------------------------------------------------

    def counts(self) -> "dict[str, int]":
        return dict(self._counts)

    def rearms(self) -> int:
        return self._rearms

    def round_line(self) -> str:
        parts = []
        for kind, name in _NAMES.items():
            text = "%s %d" % (name, self._counts[kind])
            if kind == RESUPPLY:
                text += " (%d rearm%s confirmed)" % (
                    self._rearms, "" if self._rearms == 1 else "s")
            parts.append(text)
        return "ROUND OBJECTIVES — flown through: " + ", ".join(parts)

    def end_round(self) -> "str | None":
        """The round's line, or None when no pursuit ran in it. Starts the
        next round's count either way."""
        line = self.round_line() if self._seen_anything else None
        self._start_round()
        return line


def _singular(kind: str) -> str:
    return {AIR_SUPERIORITY: "an air superiority point", RESUPPLY: "the resupply point",
            PRIORITY_TARGET: "the priority target"}[kind]
