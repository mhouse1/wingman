"""ADR 181: whether this round's chase stays on its target through missile and
terrain warnings.

Operator, 2026-11-15: "when pursuing a target it stays on pursuit, regardless of
terrain signal or incoming missile". Leaving a target to evade costs the lock and
the time to find it again: on 2026-11-15 the target was out of view after 296 of
297 flare rolls that began with it in view, and back within 20 s after only 151 of
them, a median 8.0 s later.

The arm is drawn once a round, as ADR 172, 174 and 179 draw theirs, so that the
rule can be judged against rounds without it in the same session.
"""

from __future__ import annotations

import random

HOLD, EVADE = "hold", "evade"


class PursuitHoldTrial:
    """This round's arm. `hold` (with probability `share`): while the chase has a
    target in view nothing takes the airframe from it. `evade`: the flare roll,
    the crash recovery's overrides and the terrain turn act as before. Drawn at
    the round start (main loop); read on the pursuit and tactic threads."""

    def __init__(self, share: float, rng: "random.Random | None" = None) -> None:
        if not 0.0 <= share <= 1.0:
            raise ValueError(f"hold_target share {share!r} is outside 0 to 1")
        self.share = float(share)
        self._rng = rng if rng is not None else random.Random()
        self.arm: "str | None" = None
        self.rounds = 0

    @property
    def enabled(self) -> bool:
        return self.share > 0.0

    def new_round(self) -> "str | None":
        """Draw this round's arm, or None while the rule is off."""
        if not self.enabled:
            self.arm = None
            return None
        self.arm = HOLD if self._rng.random() < self.share else EVADE
        self.rounds += 1
        return self.arm
