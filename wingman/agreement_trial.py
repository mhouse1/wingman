"""ADR 172: in a share of rounds, two agreeing terrain warnings do not break off
the approach to the resupply marker.

Code Review 019 cycle 25 (operator, 2026-10-18) let two agreeing terrain warnings
start the crash recovery with an objective in view. On a resupply run a crash
followed such an override within 12 s 13.5% of the time (46 of 340, 2026-10-26 to
11-04), and 48% of them rearmed within 25 s anyway; an approach to a marker on the
ground looks to the warnings like terrain closing. Whether the climb saves more
aircraft than it costs rearms is not in the logs: only rounds drawn into two arms
inside the same sessions can say (Research 020).

The arm is drawn once a round, where the session summary opens a mission:
`holdoff` with probability `share`, `override` otherwise. In `holdoff` the
agreement does not set aside the hold-off for the resupply marker. Every other
objective, and the pull-out alarm's own break of the hold-off (cycle 36), are as
before in both arms.

Standard library only; no I/O.
"""

from __future__ import annotations

import random

# The pursuit's name for the marker (Controller._note_pursuit_objective).
RESUPPLY = "resupply marker"


class AgreementTrial:
    """The current round's arm. One caller draws (the round start, main loop);
    the controller reads the arm from its climb and pursuit threads."""

    def __init__(self, share: float, rng: "random.Random | None" = None) -> None:
        if not 0.0 <= share <= 1.0:
            raise ValueError(f"resupply_holdoff_share {share!r} is outside 0 to 1")
        self.share = float(share)
        self._rng = rng if rng is not None else random.Random()
        self.arm: "str | None" = None
        self.rounds = 0             # rounds whose arm was drawn

    @property
    def enabled(self) -> bool:
        return self.share > 0.0

    def new_round(self) -> "str | None":
        """Draw this round's arm, or None while the trial is off."""
        if not self.enabled:
            self.arm = None
            return None
        self.arm = "holdoff" if self._rng.random() < self.share else "override"
        self.rounds += 1
        return self.arm

    def agreement_breaks_holdoff(self, objective: "str | None") -> bool:
        """Whether two agreeing warnings may set aside the hold-off for `objective`."""
        return not (objective == RESUPPLY and self.arm == "holdoff")
