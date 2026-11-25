"""ADR 204: whether this round's acs lives may skip the start climb when the map
says the level path is clear.

Operator, 2026-11-25: "currently it nose up to avoid terrain but often times its
not necessary. ideally we avoid nose up if possible". Measured over the twelve
sessions of 2026-11-23 to 25: no acs life crashed in its first 20 s, with the
start climb (0 of 389 battle entries) or without it (0 of 57), and the climb took
the nose to a median +60 deg, +80 deg or more in 75 of 232.

ADR 191 D7 is the warning: spawning lives level raised terrain deaths, 8 in 34
against 5 in 106. So the skip is drawn once a round, as ADR 172, 174, 179 and 181
draw theirs, and a `climb` round flies the start exactly as before.
"""

from __future__ import annotations

import random

SKIP, CLIMB = "skip", "climb"


class StartClimbTrial:
    """This round's arm. `skip` (with probability `share`): a life with no target
    in view at the end of its watch skips the climb when the terrain service's
    level path is clear. `climb`: the climb starts as before, and the verdict is
    only logged. Drawn at the round start (main loop); read on the mission thread."""

    def __init__(self, share: float, rng: "random.Random | None" = None) -> None:
        if not 0.0 <= share <= 1.0:
            raise ValueError(f"start_skip_share {share!r} is outside 0 to 1")
        self.share = float(share)
        self._rng = rng if rng is not None else random.Random()
        self.arm: "str | None" = None
        self.rounds = 0

    @property
    def enabled(self) -> bool:
        return self.share > 0.0

    def new_round(self) -> "str | None":
        """Draw this round's arm, or None while the skip is off."""
        if not self.enabled:
            self.arm = None
            return None
        self.arm = SKIP if self._rng.random() < self.share else CLIMB
        self.rounds += 1
        return self.arm

    def skips(self) -> bool:
        return self.arm == SKIP
