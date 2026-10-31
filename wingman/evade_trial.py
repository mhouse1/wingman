"""ADR 163 / ADR 165: which missile engagements inside a pursuit the evade may fly.

The missile evade (ADR 070) is tried inside the pursuit as a live comparison,
evade against no evade. The arm is drawn once per ROUND (ADR 165), on the
round's first engagement inside a pursuit: `evade` with probability
`pursuit_share`, `control` otherwise. Every later engagement inside a pursuit in
that round takes the same arm, so each round carries one arm and its score can
be set against the other arm's. A round opens where the session summary opens a
mission, and the caller says so with `new_round()`.

Alerts no more than `VOLLEY_GROUP_S` apart are one engagement: the grouping
the session summary counts engagements by (`mission_stats._VOLLEY_GROUP_S`), so
each of its engagements carries the round's arm. Only an engagement that opens
inside a pursuit is in the trial. One that opened outside it keeps no arm, even
if the pursuit starts before its last alert: the behavior tree flies the evade
there (ADR 070).

Standard library only; no I/O.
"""

from __future__ import annotations

import random

# The session summary's own grouping; tests/test_evade_trial.py holds the two equal.
VOLLEY_GROUP_S = 3.0


class EvadeTrial:
    """The arm of the current round and engagement. Two callers, both on the main
    loop: the flare handler's alert edge, and its round start."""

    def __init__(self, share: float, rng: "random.Random | None" = None,
                 volley_s: float = VOLLEY_GROUP_S) -> None:
        if not 0.0 <= share <= 1.0:
            raise ValueError(f"pursuit_share {share!r} is outside 0 to 1")
        self.share = float(share)
        self._rng = rng if rng is not None else random.Random()
        self._volley_s = float(volley_s)
        self._last_alert_ts: "float | None" = None
        self._arm: "str | None" = None          # the current engagement's
        self._round_arm: "str | None" = None    # drawn on the round's first one in a pursuit
        self.engagements = 0        # engagements in the trial, either arm
        self.rounds = 0             # rounds whose arm was drawn

    @property
    def enabled(self) -> bool:
        return self.share > 0.0

    def new_round(self) -> None:
        """A round has started: its arm is drawn afresh, and no engagement
        carries over from the last round."""
        self._round_arm = None
        self._last_alert_ts = None
        self._arm = None

    def on_alert(self, ts: float, in_pursuit: bool) -> "tuple[str | None, bool]":
        """This alert's engagement arm (None when it is not in the trial), and
        whether the alert opened the engagement."""
        opened = self._last_alert_ts is None or ts - self._last_alert_ts > self._volley_s
        self._last_alert_ts = ts
        if opened:
            self._arm = None
            if in_pursuit and self.enabled:
                if self._round_arm is None:
                    self._round_arm = "evade" if self._rng.random() < self.share else "control"
                    self.rounds += 1
                self._arm = self._round_arm
                self.engagements += 1
        return self._arm, opened
