"""ADR 179: in a share of rounds, the chase brakes when it closes fast on a
target that is near.

Operator, 2026-11-13: "there are engagements where if we see a target is moving
rapidly to the left or right we can slow down while turning and then speed up
quickly to continue following target."

Measured on the six Phase 3 logs of 2026-11-13 and 14 (Design 021): of 89
encounters under 1.2 km, the median lasted 2.0 s; wingman arrived closing at a
median 252 m/s (73% over 150 m/s) and most ended at their nearest point with
the target out of the picture. Two seconds is not enough to bring the lead ring
to the crosshair, whatever the aim rule.

This module says when to brake, from the tracked target's range readings alone,
and keeps the trial's arm and its measure (how long each encounter under
1.2 km lasted, by arm). The controller holds the airbrake. No I/O and no clock:
every time is passed in.
"""

from __future__ import annotations

import random
from collections import deque

# The readings a closing speed is worked out over, and what it needs: the range
# is printed to 0.1 km past 1 km, so two readings alone say little.
_WINDOW_S = 1.5
_MIN_READINGS = 3
_MIN_SPAN_S = 0.5
# A reading older than this says nothing about now.
_FRESH_S = 1.0
# The trial's measure: an encounter is a run of readings under this range with
# no gap longer than _ENCOUNTER_GAP_S, and counts with three readings or more.
ENCOUNTER_KM = 1.2
_ENCOUNTER_GAP_S = 2.0
_ENCOUNTER_MIN_READINGS = 3


class ClosingBrake:
    """The round's arm, the brake decision and the encounter tally.

    `brake` rounds (with probability `share`) brake; `control` rounds fly as
    before. Both arms tally their encounters. The round start draws the arm
    (main loop); the pursuit thread does the rest."""

    def __init__(self, share: float, on_km: float, on_mps: float, off_mps: float,
                 max_hold_s: float, cooldown_s: float,
                 rng: "random.Random | None" = None) -> None:
        if not 0.0 <= share <= 1.0:
            raise ValueError(f"closing_brake share {share!r} is outside 0 to 1")
        self.share = float(share)
        self.on_km = float(on_km)
        self.on_mps = float(on_mps)
        self.off_mps = float(off_mps)
        self.max_hold_s = float(max_hold_s)
        self.cooldown_s = float(cooldown_s)
        self._rng = rng if rng is not None else random.Random()
        self.arm: "str | None" = None
        self.rounds = 0
        self._readings: "deque[tuple[float, float]]" = deque()      # ts, km
        self._hold_since: "float | None" = None
        self._no_hold_before = 0.0
        self.why_off = ""
        # The encounter in hand: its first and last reading's time, and how many.
        self._enc: "list[float] | None" = None
        self.encounters: "dict[str, list[float]]" = {}                # arm -> seconds

    @property
    def enabled(self) -> bool:
        return self.share > 0.0

    def new_round(self) -> "str | None":
        """Draw this round's arm, or None while the trial is off."""
        self._close_encounter()
        self._readings.clear()
        self._hold_since = None
        if not self.enabled:
            self.arm = None
            return None
        self.arm = "brake" if self._rng.random() < self.share else "control"
        self.rounds += 1
        return self.arm

    # -- readings ---------------------------------------------------------------

    def note(self, km: float, ts: float) -> None:
        """A new range reading of the tracked target."""
        if self._readings and ts <= self._readings[-1][0]:
            return
        self._readings.append((ts, float(km)))
        while self._readings and ts - self._readings[0][0] > _WINDOW_S:
            self._readings.popleft()
        if km < ENCOUNTER_KM:
            if self._enc is not None and ts - self._enc[1] > _ENCOUNTER_GAP_S:
                self._close_encounter()
            if self._enc is None:
                self._enc = [ts, ts, 0]
            self._enc[1] = ts
            self._enc[2] += 1
        elif self._enc is not None and ts - self._enc[1] > _ENCOUNTER_GAP_S:
            self._close_encounter()

    def _close_encounter(self) -> None:
        enc, self._enc = self._enc, None
        if enc is not None and enc[2] >= _ENCOUNTER_MIN_READINGS:
            self.encounters.setdefault(self.arm or "off", []).append(enc[1] - enc[0])

    def closing_mps(self) -> "float | None":
        """How fast the range is falling, m/s, by least squares over the last
        _WINDOW_S of readings; None without enough of them. Negative: opening."""
        pts = list(self._readings)
        if len(pts) < _MIN_READINGS or pts[-1][0] - pts[0][0] < _MIN_SPAN_S:
            return None
        n = len(pts)
        mt = sum(t for t, _ in pts) / n
        mk = sum(k for _, k in pts) / n
        den = sum((t - mt) ** 2 for t, _ in pts)
        if den <= 0.0:
            return None
        slope = sum((t - mt) * (k - mk) for t, k in pts) / den      # km a second
        return -slope * 1000.0

    # -- the decision -------------------------------------------------------------

    def wants(self, now: float) -> bool:
        """Whether the chase should hold the airbrake now. Only in a `brake`
        round, on a fresh reading under `on_km`: it starts when the range closes
        at `on_mps` or more, and ends when it closes at less than `off_mps`, the
        target is past `on_km` or lost, or after `max_hold_s` (then not again
        for `cooldown_s`)."""
        if self.arm != "brake":
            return self._end("not a brake round")
        if not self._readings or now - self._readings[-1][0] > _FRESH_S:
            return self._end("no fresh range")
        km = self._readings[-1][1]
        if km >= self.on_km:
            return self._end("the target is %.1f km out" % km)
        closing = self.closing_mps()
        if closing is None:
            return self._end("no closing speed yet")
        if self._hold_since is None:
            if closing < self.on_mps or now < self._no_hold_before:
                return False
            self._hold_since = now
            self.why_off = ""
            return True
        if closing < self.off_mps:
            return self._end("closing at %.0f m/s" % closing)
        if now - self._hold_since >= self.max_hold_s:
            self._no_hold_before = now + self.cooldown_s
            return self._end("held %.1f s" % (now - self._hold_since))
        return True

    def stop(self, why: str) -> None:
        """The chase cannot brake now (the target does not steer, the speed is
        too low): a hold in hand is over."""
        self._end(why)

    def _end(self, why: str) -> bool:
        if self._hold_since is not None:
            self._hold_since = None
            self.why_off = why
        return False

    # -- the measure ----------------------------------------------------------------

    def summary(self) -> str:
        self._close_encounter()
        parts = []
        for arm, secs in sorted(self.encounters.items()):
            s = sorted(secs)
            parts.append("%s arm %d encounters under %.1f km, median %.1f s, %d of 3 s or more" % (
                arm, len(s), ENCOUNTER_KM, s[len(s) // 2], sum(1 for v in s if v >= 3.0)))
        return "; ".join(parts) or "no encounters"
