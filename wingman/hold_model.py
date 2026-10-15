"""How long a steering key has to stay down (HLDD 005, Sustained-Hold Actuation).

The picture answers a flight key late. Measured on four sessions of 2026-10-11
(the travel of the steered point between two pictures, against the seconds of
key held between them): the two agree best when the key time is taken 0.20 to
0.30 s earlier than the pictures, on both axes, and the point then moves 0.6 to
1.15 half-frames a second in pitch and 0.2 to 0.4 in roll (r 0.91 and 0.84 on a
control point, which does not move by itself).

A hold that ends when the picture shows the point at the centre therefore ends
that much too late: the nose keeps going for the lag, the opposite key is
pressed, and it comes back the same way. In those sessions 70 to 80 percent of
pitch presses and 62 to 78 percent of roll presses were the opposite of the one
before.

`HoldModel` keeps one axis's recent key history and says how far the keys have
already sent the nose beyond what a picture shows, and so how much longer the
key is needed. It also keeps the target's own motion across the screen, read
from what the pictures show beyond the keys' doing, and sizes a hold for it:
without it the hold trails a crossing target and never reaches it (simulated at
0.4 half-frames a second: 0 percent of ticks inside the deadband, against 62
percent with it).

No key is held while the picture shows the target inside the deadband. The
first live session (2026-10-11 16:34 to 17:58, 1,366 s of lock) let the
prediction press there, to lead a crossing target and to counter travel still
to come: 58 percent of pitch presses and 65 percent of roll presses were made
that way, 79 percent of them the opposite of the press before, with the
prediction changing sign from one tick to the next over an error that had not
moved (16:48:20.401 `err_y=+0.005 pred=-0.191`, 16:48:20.568 `err_y=+0.003
pred=+0.268`). The prediction is not good enough to act on alone: an isolated
0.08 s pitch pulse shows nothing for 0.2 s and is still arriving at 0.5 s, not
all at once a lag later as the model has it.
`PictureClock` says when the picture in hand was first grabbed:
with a steering key held, so the nose certainly moving, 23 to 26 percent of
consecutive steering grabs in those sessions showed the picture of the grab
before, unchanged for 0.17 s at the median and 0.34 to 0.44 s at the 90th
percentile.

No I/O and no clock of its own: every time is passed in, from one monotonic
clock.
"""

import math
from typing import NamedTuple

import numpy as np


class HoldPlan(NamedTuple):
    """`sign` +1 or -1 is the key to hold, for `seconds`; 0 is no key.
    `predicted` is the error expected when a key pressed now starts to act."""

    sign: int
    seconds: float
    predicted: float


def _sign(value: float) -> int:
    return (value > 0) - (value < 0)


class HoldModel:
    """One steering axis: the error falls `rate_per_s` for each second of key,
    `lag_s` after the key.

    +1 is the key that lowers a positive error (ROLL_RIGHT, NOSE_DOWN), -1 the
    other, 0 neither. `key` is called from whichever thread presses or lets go;
    it only appends, or swaps in a new list, so readers need no lock.
    """

    # The target's own motion is averaged over about this long.
    DRIFT_TAU_S = 0.4
    # Two pictures further apart than this are not one motion (the target was
    # lost in between), nor are two closer than _MIN_GAP_S (pixel noise).
    DRIFT_MAX_GAP_S = 0.6
    _MIN_GAP_S = 0.02
    # Faster than this across the screen is a different target, not a moving one.
    DRIFT_MAX_PER_S = 1.5
    # The most the target's motion may add to the error, in half-frames.
    LEAD_MAX = 0.2
    # A learned rate (learn_rate) weighs about this many seconds of steering,
    # and starts from the configured rate as if it had seen this much key time.
    RATE_TAU_S = 5.0
    _RATE_PRIOR_KEY_S = 0.3
    # A picture pair with less key time than this says nothing about the rate.
    _RATE_MIN_KEY_S = 0.02

    def __init__(self, rate_per_s: float, lag_s: float, memory_s: float = 3.0, *,
                 learn_rate: bool = False, rate_min: "float | None" = None,
                 rate_max: "float | None" = None) -> None:
        self.base_rate_per_s = float(rate_per_s)
        self.rate_per_s = float(rate_per_s)
        # learn_rate: the rate follows the pictures (`_observe`), between
        # rate_min and rate_max. The jets differ fivefold: a second of pitch key
        # moved the target 0.6 to 0.9 half-frames in the sessions of 2026-10-11
        # and 0.15 to 0.23 on 2026-10-14 at 1,200 to 1,450 KPH.
        self.learn_rate = bool(learn_rate)
        self.rate_min = self.rate_per_s * 0.1 if rate_min is None else float(rate_min)
        self.rate_max = self.rate_per_s * 1.3 if rate_max is None else float(rate_max)
        prior = self._RATE_PRIOR_KEY_S ** 2
        self._rate_num = self.rate_per_s * prior
        self._rate_den = prior
        self.lag_s = max(0.0, float(lag_s))
        # How far back a picture may date and still be believed: an unchanged
        # picture older than this is taken as that old, no older.
        self.memory_s = float(memory_s)
        self._events: "list[tuple[float, int]]" = []
        # The target's own motion: error per second with no key held. A rate
        # set too high or too low for the airframe shows up here too, as motion
        # with the key or against it, and is led the same way.
        self.drift_per_s = 0.0
        self._sample: "tuple[float, float] | None" = None   # last new picture

    def key(self, sign: int, now: float) -> None:
        """The axis's key state from `now`."""
        events = self._events
        horizon = now - self.memory_s - self.lag_s
        if len(events) > 1 and events[1][0] < horizon:
            # The newest entry before the horizon is the state at the horizon.
            keep = max(i for i, e in enumerate(events) if e[0] < horizon)
            events = events[keep:]
        self._events = events + [(now, _sign(sign))]

    def key_time(self, start: float, end: float) -> float:
        """Signed seconds of key held between `start` and `end`."""
        total = 0.0
        state = 0
        at = start
        for when, sign in list(self._events):
            if when >= end:
                break
            if when > at:
                total += state * (when - at)
                at = when
            state = sign
        return total + state * max(0.0, end - at)

    def pending(self, seen_at: float, now: float) -> float:
        """How far the keys have sent the error down that a picture first
        grabbed at `seen_at` does not show yet."""
        return self.rate_per_s * self.key_time(seen_at - self.lag_s, now)

    def _observe(self, error: float, seen_at: float) -> None:
        """Update the target's own motion from a picture not seen before."""
        prev = self._sample
        if prev is not None and seen_at <= prev[0]:
            return                              # the same picture again
        self._sample = (seen_at, error)
        if prev is None:
            return
        gap = seen_at - prev[0]
        if not self._MIN_GAP_S <= gap <= self.DRIFT_MAX_GAP_S:
            self.drift_per_s = 0.0
            return
        key_s = self.key_time(prev[0] - self.lag_s, seen_at - self.lag_s)
        changed = error - prev[1]
        # What the error did beyond what the keys account for.
        moved = changed + self.rate_per_s * key_s
        if abs(moved) / gap > self.DRIFT_MAX_PER_S:
            self.drift_per_s = 0.0
            return
        weight = 1.0 - math.exp(-gap / self.DRIFT_TAU_S)
        if not self.learn_rate:
            self.drift_per_s += weight * (moved / gap - self.drift_per_s)
            return
        # Learning the rate, the two are kept apart: a pair with no key in it
        # shows only the target's own motion, and a pair with key time shows the
        # keys' travel once that motion is taken out. Estimated from every pair
        # at once, a rate set too high reads as the target moving away and the
        # two hold each other up (simulated: a jet answering at 0.2 was learned
        # as 0.54 to 0.74; kept apart, 0.22 to 0.40).
        if abs(key_s) < self._RATE_MIN_KEY_S:
            self.drift_per_s += weight * (changed / gap - self.drift_per_s)
            return
        # Least squares through the origin, travel against key time, older
        # pairs fading over RATE_TAU_S.
        travel = self.drift_per_s * gap - changed
        fade = math.exp(-gap / self.RATE_TAU_S)
        self._rate_num = self._rate_num * fade + travel * key_s
        self._rate_den = self._rate_den * fade + key_s * key_s
        self.rate_per_s = max(self.rate_min, min(self.rate_max,
                                                 self._rate_num / self._rate_den))

    def plan(self, error: float, seen_at: float, now: float, *,
             deadband: float, min_pulse_s: float = 0.0) -> HoldPlan:
        """What to hold for `error`, read from a picture first grabbed at `seen_at`.

        `predicted` is the error when a key pressed now starts to act: the
        error shown, plus the target's own motion until then, less the travel
        already commanded. No key when that is inside the deadband, and none
        while the error shown is: the prediction sizes a hold, it does not
        start one the picture has not asked for (see the module docstring). A
        key is only ever held toward the error shown.
        """
        seen_at = min(now, max(seen_at, now - self.memory_s))
        self._observe(error, seen_at)
        lead = self.drift_per_s * (now - seen_at + self.lag_s)
        lead = max(-self.LEAD_MAX, min(self.LEAD_MAX, lead))
        predicted = error + lead - self.pending(seen_at, now)
        if (abs(error) <= deadband or abs(predicted) <= deadband
                or _sign(predicted) != _sign(error) or self.rate_per_s <= 0):
            return HoldPlan(0, 0.0, predicted)
        # A target moving away is closed on more slowly than one standing still.
        closing = max(self.rate_per_s - _sign(predicted) * self.drift_per_s,
                      0.25 * self.rate_per_s)
        return HoldPlan(_sign(predicted),
                        max(abs(predicted) / closing, min_pulse_s), predicted)


class PictureClock:
    """When the picture in hand was first grabbed.

    Two grabs are the same picture when every `step`-th pixel of every
    `step`-th row matches: 2,280 pixels of a 1920 x 1200 frame at the default.
    """

    def __init__(self, step: int = 32) -> None:
        self._step = max(1, int(step))
        self._probe = None
        self._seen_at = 0.0

    def seen(self, frame, now: float) -> float:
        if not isinstance(frame, np.ndarray):
            return now      # no picture to compare: taken as new
        probe = np.ascontiguousarray(frame[::self._step, ::self._step])
        if (self._probe is not None and probe.shape == self._probe.shape
                and np.array_equal(probe, self._probe)):
            return self._seen_at
        self._probe = probe
        self._seen_at = now
        return now
