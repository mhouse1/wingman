"""Pure telemetry signal processing for ADR 038.

Filter, smoothing, rate derivation, and pitch-band estimation for the
altitude/speed signals read from the combined ALTITUDE_SPEED crop. Follows the
crop_region.py precedent: no internal imports, no threads, no OCR — every rule
is unit-testable with plain numbers. GameStateAnalyzer owns an instance and
serializes access with its telemetry lock; this module itself is not
thread-safe.

The plausibility filter adopts the ADR 030 pattern (cheap reader plus a
rejection gate in front of decision logic) with bounds derived from physics
rather than a rolling ceiling:

- speed is filtered first against an acceleration envelope, so one bogus
  speed read cannot inflate the altitude gate on the same tick
- the altitude gate bounds climb/descent by the last accepted speed (vertical
  speed can never exceed total speed), falling back to max_speed_mph when the
  speed signal is stale
- rejected readings keep the last accepted value; after
  reseed_after_rejections consecutive rejections the signal re-seeds, so one
  bogus seed cannot lock out all subsequent real values
"""

from __future__ import annotations

from dataclasses import dataclass, field

MPH_TO_FPS = 5280.0 / 3600.0

# Pitch bands returned by pitch_band(). Sine is symmetric about vertical, so a
# shallow band cannot distinguish under-rotation from over-rotation past
# vertical — callers must correct with measure-correct-measure, not blind
# re-issue (ADR 038).
BAND_STEEP_DIVE = "steep_dive"
BAND_DIVE = "dive"
BAND_LEVEL = "level"
BAND_CLIMB = "climb"
BAND_STEEP_CLIMB = "steep_climb"

TREND_RISING = "rising"
TREND_FALLING = "falling"
TREND_FLAT = "flat"
TREND_UNKNOWN = "unknown"


@dataclass(frozen=True)
class TelemetrySignal:
    """Immutable per-signal state: last accepted reading plus derived values."""

    value: int | None = None          # last accepted raw reading
    ts: float | None = None           # when it was accepted (caller clock)
    stable_value: float | None = None # mean of the last smoothing_window accepted readings
    rate: float | None = None         # units/s between the last two accepted readings
    trend: str = TREND_UNKNOWN
    rejected_streak: int = 0          # consecutive rejections since last accept

    def age_s(self, now_s: float) -> float | None:
        if self.ts is None:
            return None
        return max(0.0, now_s - self.ts)

    def is_fresh(self, now_s: float, stale_after_s: float) -> bool:
        age = self.age_s(now_s)
        return age is not None and age <= stale_after_s


@dataclass(frozen=True)
class TelemetrySnapshot:
    """Atomic pairing of both signals from the same processor state.

    Consumers must take speed and altitude from one snapshot — nose-direction
    estimation divides altitude rate by speed, so a torn pair from different
    cycles produces a wrong pitch estimate.
    """

    speed: TelemetrySignal = field(default_factory=TelemetrySignal)
    altitude: TelemetrySignal = field(default_factory=TelemetrySignal)
    taken_at_s: float = 0.0
    stale_after_s: float = 6.0

    def speed_fresh(self) -> bool:
        return self.speed.is_fresh(self.taken_at_s, self.stale_after_s)

    def altitude_fresh(self) -> bool:
        return self.altitude.is_fresh(self.taken_at_s, self.stale_after_s)

    def pitch_band(
        self,
        *,
        steep_min_sin: float = 0.8,
        level_max_sin: float = 0.15,
    ) -> str | None:
        """Classify nose attitude, or None when either signal is missing/stale."""
        if not (self.speed_fresh() and self.altitude_fresh()):
            return None
        return pitch_band(
            self.speed.stable_value,
            self.altitude.rate,
            steep_min_sin=steep_min_sin,
            level_max_sin=level_max_sin,
        )


def pitch_band(
    speed_mph: float | None,
    alt_rate_fps: float | None,
    *,
    steep_min_sin: float = 0.8,
    level_max_sin: float = 0.15,
    min_speed_fps: float = 30.0,
) -> str | None:
    """Estimate the flight-path-angle band from altitude rate and speed.

    Altitude rate is approximately speed times the sine of the flight-path
    angle, so the ratio alt_rate / speed bounds the attitude without extra
    HUD parsing. Returns None when inputs are missing or speed is too small
    for the ratio to be meaningful.

    Two physical caveats (flight-tested, session 2026-07-28): this measures
    the velocity-vector angle, not nose attitude — they differ by angle of
    attack, most at low speed; and sine compresses near vertical (sin 75 deg
    is 0.97), so bands distinguish 30 from 60 degrees but not 75 from 90.
    The default steep_min_sin of 0.8 (approx 53 degrees) is the steepest
    band that confirms reliably through OCR noise.
    """
    if speed_mph is None or alt_rate_fps is None:
        return None
    speed_fps = speed_mph * MPH_TO_FPS
    if speed_fps < min_speed_fps:
        return None
    ratio = alt_rate_fps / speed_fps
    ratio = max(-1.5, min(1.5, ratio))  # OCR noise can push past ±1
    if ratio <= -steep_min_sin:
        return BAND_STEEP_DIVE
    if ratio < -level_max_sin:
        return BAND_DIVE
    if ratio <= level_max_sin:
        return BAND_LEVEL
    if ratio < steep_min_sin:
        return BAND_CLIMB
    return BAND_STEEP_CLIMB


class TelemetryProcessor:
    """Filter + smoothing + rate state for both telemetry signals.

    Feed accepted-or-not raw readings via update(); read one atomic
    TelemetrySnapshot via snapshot(). Not thread-safe — the caller serializes.
    """

    def __init__(self, config: dict | None = None):
        cfg = config or {}
        self.max_speed_mph = float(cfg.get("max_speed_mph", 2000.0))
        self.max_altitude_ft = float(cfg.get("max_altitude_ft", 60000.0))
        self.max_speed_change_mph_s = float(cfg.get("max_speed_change_mph_s", 300.0))
        self.plausibility_margin = float(cfg.get("plausibility_margin", 1.5))
        self.reseed_after_rejections = max(1, int(cfg.get("reseed_after_rejections", 3)))
        self.smoothing_window = max(1, int(cfg.get("smoothing_window", 3)))
        self.stale_after_s = float(cfg.get("stale_after_s", 6.0))
        self.trend_min_alt_rate_fps = float(cfg.get("trend_min_alt_rate_fps", 20.0))
        self.trend_min_speed_rate_mph_s = float(cfg.get("trend_min_speed_rate_mph_s", 15.0))
        self.steep_min_sin = float(cfg.get("steep_dive_min_sin", 0.5))
        self.level_max_sin = float(cfg.get("level_max_sin", 0.15))

        self._speed = TelemetrySignal()
        self._altitude = TelemetrySignal()
        # Accepted (ts, value) history, newest last, capped at smoothing_window.
        self._speed_hist: list[tuple[float, float]] = []
        self._alt_hist: list[tuple[float, float]] = []
        self.rejected_total = 0

    def reset(self) -> None:
        self._speed = TelemetrySignal()
        self._altitude = TelemetrySignal()
        self._speed_hist = []
        self._alt_hist = []

    def update(
        self,
        speed_raw: int | None,
        altitude_raw: int | None,
        now_s: float,
    ) -> None:
        """Apply the plausibility filter to one tick's raw OCR readings.

        Speed is filtered first: the altitude gate uses the last accepted
        speed, never the raw speed from the same tick (ADR 038 filter
        ordering).
        """
        if speed_raw is not None:
            self._speed = self._update_signal(
                signal=self._speed,
                hist=self._speed_hist,
                raw=float(speed_raw),
                now_s=now_s,
                absolute_max=self.max_speed_mph * self.plausibility_margin,
                max_delta_per_s=self.max_speed_change_mph_s * self.plausibility_margin,
                trend_min_rate=self.trend_min_speed_rate_mph_s,
            )
        if altitude_raw is not None:
            self._altitude = self._update_signal(
                signal=self._altitude,
                hist=self._alt_hist,
                raw=float(altitude_raw),
                now_s=now_s,
                absolute_max=self.max_altitude_ft,
                max_delta_per_s=self._altitude_bound_fps(now_s) * self.plausibility_margin,
                trend_min_rate=self.trend_min_alt_rate_fps,
            )

    def snapshot(self, now_s: float) -> TelemetrySnapshot:
        return TelemetrySnapshot(
            speed=self._speed,
            altitude=self._altitude,
            taken_at_s=now_s,
            stale_after_s=self.stale_after_s,
        )

    def _altitude_bound_fps(self, now_s: float) -> float:
        """Max plausible climb/descent rate: vertical speed cannot exceed total
        speed. Falls back to the aircraft envelope maximum when the speed
        signal is stale, so a dead speed read cannot freeze the altitude gate.
        """
        if self._speed.value is not None and self._speed.is_fresh(now_s, self.stale_after_s):
            bound_mph = max(float(self._speed.value), 60.0)  # floor: parked reads of 0 must not zero the gate
        else:
            bound_mph = self.max_speed_mph
        return bound_mph * MPH_TO_FPS

    def _update_signal(
        self,
        *,
        signal: TelemetrySignal,
        hist: list[tuple[float, float]],
        raw: float,
        now_s: float,
        absolute_max: float,
        max_delta_per_s: float,
        trend_min_rate: float,
    ) -> TelemetrySignal:
        if raw < 0.0 or raw > absolute_max:
            # Out-of-envelope readings are never seedable — a consistent
            # stream of absolute-bound garbage must not become the new seed.
            return self._reject(signal, hist, raw, now_s, seedable=False)

        if signal.value is not None and signal.ts is not None:
            dt = max(now_s - signal.ts, 0.1)  # guard duplicate timestamps
            if abs(raw - float(signal.value)) > max_delta_per_s * dt:
                return self._reject(signal, hist, raw, now_s, seedable=True)

        return self._accept(hist, raw, now_s, trend_min_rate)

    def _reject(
        self,
        signal: TelemetrySignal,
        hist: list[tuple[float, float]],
        raw: float,
        now_s: float,
        *,
        seedable: bool,
    ) -> TelemetrySignal:
        self.rejected_total += 1
        streak = signal.rejected_streak + 1
        if streak >= self.reseed_after_rejections:
            # The seed itself is suspect — recalibrate. A delta-rejected
            # stream is usually the consistent real value blocked by a bogus
            # seed, so seed from it; out-of-envelope streams clear to empty
            # and the next in-bounds reading seeds instead.
            hist.clear()
            if seedable:
                return TelemetrySignal(
                    value=int(raw),
                    ts=now_s,
                    stable_value=raw,
                )
            return TelemetrySignal()
        # Keep the last accepted value; age keeps growing (ts unchanged).
        return TelemetrySignal(
            value=signal.value,
            ts=signal.ts,
            stable_value=signal.stable_value,
            rate=signal.rate,
            trend=signal.trend,
            rejected_streak=streak,
        )

    def _accept(
        self,
        hist: list[tuple[float, float]],
        raw: float,
        now_s: float,
        trend_min_rate: float,
    ) -> TelemetrySignal:
        hist.append((now_s, raw))
        del hist[:-self.smoothing_window]

        stable = sum(v for _, v in hist) / len(hist)

        # Rate from post-filter accepted readings with real timestamp deltas —
        # deliberately not from stable_value, whose smoothing lag is too slow
        # for dive verification (ADR 038 data model).
        rate = None
        if len(hist) >= 2:
            (t_prev, v_prev), (t_new, v_new) = hist[-2], hist[-1]
            dt = t_new - t_prev
            if dt > 0.0:
                rate = (v_new - v_prev) / dt

        if rate is None:
            trend = TREND_UNKNOWN
        elif rate > trend_min_rate:
            trend = TREND_RISING
        elif rate < -trend_min_rate:
            trend = TREND_FALLING
        else:
            trend = TREND_FLAT

        return TelemetrySignal(
            value=int(raw),
            ts=now_s,
            stable_value=stable,
            rate=rate,
            trend=trend,
        )
