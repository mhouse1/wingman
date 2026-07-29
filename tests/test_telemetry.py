"""Unit tests for wingman/telemetry.py — ADR 038 signal model.

Pure-module tests: no OCR, no threads, plain numbers only.
"""

import pytest

from wingman.telemetry import (
    BAND_CLIMB,
    BAND_DIVE,
    BAND_LEVEL,
    BAND_STEEP_CLIMB,
    BAND_STEEP_DIVE,
    MPH_TO_FPS,
    TREND_FALLING,
    TREND_FLAT,
    TREND_RISING,
    TREND_UNKNOWN,
    TelemetryProcessor,
    TelemetrySnapshot,
    pitch_band,
)


def _proc(**overrides):
    cfg = {
        "max_speed_mph": 2000.0,
        "max_altitude_ft": 60000.0,
        "max_speed_change_mph_s": 300.0,
        "plausibility_margin": 1.5,
        "reseed_after_rejections": 3,
        "smoothing_window": 3,
        "stale_after_s": 6.0,
    }
    cfg.update(overrides)
    return TelemetryProcessor(cfg)


# ---------------------------------------------------------------------------
# Seeding and acceptance
# ---------------------------------------------------------------------------

class TestSeedingAndAcceptance:
    def test_first_reading_seeds_unconditionally_within_bounds(self):
        p = _proc()
        p.update(530, 27681, now_s=0.0)
        snap = p.snapshot(0.0)
        assert snap.speed.value == 530
        assert snap.altitude.value == 27681

    def test_gradual_change_accepted_and_rate_derived(self):
        p = _proc()
        p.update(600, 20000, now_s=0.0)
        p.update(610, 19400, now_s=1.5)
        snap = p.snapshot(1.5)
        assert snap.altitude.value == 19400
        assert snap.altitude.rate == pytest.approx(-400.0)  # 600 ft in 1.5 s
        assert snap.speed.rate == pytest.approx(10 / 1.5)

    def test_stable_value_is_windowed_mean(self):
        p = _proc(smoothing_window=3)
        for i, alt in enumerate([10000, 10100, 10200, 10300]):
            p.update(None, alt, now_s=float(i))
        snap = p.snapshot(3.0)
        assert snap.altitude.stable_value == pytest.approx((10100 + 10200 + 10300) / 3)


# ---------------------------------------------------------------------------
# Plausibility filter — the ADR 030 pattern with physics bounds
# ---------------------------------------------------------------------------

class TestPlausibilityFilter:
    def test_stray_digit_altitude_spike_rejected(self):
        # The ADR 038 example: 2768 read as 27681 in one tick.
        p = _proc()
        p.update(600, 2768, now_s=0.0)
        p.update(600, 27681, now_s=1.5)
        snap = p.snapshot(1.5)
        assert snap.altitude.value == 2768          # last accepted kept
        assert snap.altitude.ts == 0.0              # age keeps growing
        assert snap.altitude.rejected_streak == 1

    def test_max_rate_dive_is_accepted(self):
        # Vertical dive at 600 MPH ≈ 880 ft/s — the very signal being measured
        # must pass (analogue of ADR 030 accepting health restores).
        p = _proc()
        p.update(600, 20000, now_s=0.0)
        dive_delta = 880 * 1.5  # 1320 ft in one tick, within speed bound
        p.update(600, int(20000 - dive_delta), now_s=1.5)
        snap = p.snapshot(1.5)
        assert snap.altitude.value == int(20000 - dive_delta)
        assert snap.altitude.rejected_streak == 0

    def test_bogus_speed_does_not_inflate_altitude_gate_same_tick(self):
        # Filter ordering: altitude gate uses the last *accepted* speed, never
        # the raw speed from the same tick. 13550 MPH is rejected, so the
        # 25000 ft jump must also be rejected.
        p = _proc()
        p.update(1355, 2768, now_s=0.0)
        p.update(13550, 27681, now_s=1.5)
        snap = p.snapshot(1.5)
        assert snap.speed.value == 1355
        assert snap.altitude.value == 2768

    def test_stale_speed_falls_back_to_envelope_bound(self):
        # With no speed signal at all, the altitude gate uses max_speed_mph —
        # conservative but still tight enough to reject huge jumps.
        p = _proc(max_speed_mph=2000.0, plausibility_margin=1.0)
        p.update(None, 5000, now_s=0.0)
        # 2000 MPH ≈ 2933 ft/s → 4400 ft allowed per 1.5 s tick.
        p.update(None, 5000 + 4000, now_s=1.5)   # within envelope bound
        assert p.snapshot(1.5).altitude.value == 9000
        p.update(None, 40000, now_s=3.0)          # far beyond envelope bound
        assert p.snapshot(3.0).altitude.value == 9000

    def test_speed_acceleration_envelope(self):
        p = _proc(max_speed_change_mph_s=300.0, plausibility_margin=1.0)
        p.update(500, None, now_s=0.0)
        p.update(940, None, now_s=1.5)  # +440 in 1.5s < 450 allowed
        assert p.snapshot(1.5).speed.value == 940
        p.update(1500, None, now_s=3.0)  # +560 in 1.5s > 450 allowed
        assert p.snapshot(3.0).speed.value == 940


# ---------------------------------------------------------------------------
# Reseed behavior
# ---------------------------------------------------------------------------

class TestReseed:
    def test_bogus_seed_cannot_lock_out_real_values(self):
        # Seed with a bogus 27681; real readings around 2768 are rejected until
        # the reseed threshold clears the bad seed.
        p = _proc(reseed_after_rejections=3)
        p.update(None, 27681, now_s=0.0)
        p.update(None, 2768, now_s=1.5)
        p.update(None, 2770, now_s=3.0)
        p.update(None, 2772, now_s=4.5)  # third consecutive rejection → reseed
        snap = p.snapshot(4.5)
        assert snap.altitude.value == 2772
        assert snap.altitude.rejected_streak == 0

    def test_out_of_envelope_stream_never_becomes_seed(self):
        p = _proc(reseed_after_rejections=2, max_altitude_ft=60000.0)
        p.update(None, 5000, now_s=0.0)
        p.update(None, 99999, now_s=1.5)
        p.update(None, 99999, now_s=3.0)  # reseed fires, but not seedable
        snap = p.snapshot(3.0)
        assert snap.altitude.value is None
        p.update(None, 5100, now_s=4.5)   # next in-bounds reading seeds
        assert p.snapshot(4.5).altitude.value == 5100

    def test_accept_resets_rejected_streak(self):
        p = _proc(reseed_after_rejections=3)
        p.update(None, 5000, now_s=0.0)
        p.update(None, 50000, now_s=1.5)   # rejected
        p.update(None, 5050, now_s=3.0)    # accepted
        snap = p.snapshot(3.0)
        assert snap.altitude.value == 5050
        assert snap.altitude.rejected_streak == 0


# ---------------------------------------------------------------------------
# Trend and staleness
# ---------------------------------------------------------------------------

class TestTrendAndStaleness:
    def test_trend_classification(self):
        p = _proc(trend_min_alt_rate_fps=20.0)
        p.update(None, 10000, now_s=0.0)
        assert p.snapshot(0.0).altitude.trend == TREND_UNKNOWN
        p.update(None, 10005, now_s=1.5)
        assert p.snapshot(1.5).altitude.trend == TREND_FLAT
        p.update(None, 10500, now_s=3.0)
        assert p.snapshot(3.0).altitude.trend == TREND_RISING
        p.update(None, 9800, now_s=4.5)
        assert p.snapshot(4.5).altitude.trend == TREND_FALLING

    def test_staleness_from_snapshot_time(self):
        p = _proc(stale_after_s=6.0)
        p.update(600, 10000, now_s=0.0)
        assert p.snapshot(3.0).altitude_fresh()
        assert not p.snapshot(10.0).altitude_fresh()

    def test_rejection_keeps_age_growing(self):
        p = _proc()
        p.update(None, 10000, now_s=0.0)
        p.update(None, 50000, now_s=5.0)  # rejected — ts stays 0.0
        snap = p.snapshot(8.0)
        assert snap.altitude.age_s(8.0) == pytest.approx(8.0)
        assert not snap.altitude_fresh()


# ---------------------------------------------------------------------------
# Pitch bands — nose-direction estimation
# ---------------------------------------------------------------------------

class TestPitchBand:
    def test_level_flight(self):
        assert pitch_band(600.0, 0.0) == BAND_LEVEL

    def test_shallow_dive_20_degrees_at_600mph(self):
        # sin(20°) ≈ 0.342 → ~300 ft/s descent at 600 MPH.
        assert pitch_band(600.0, -300.0) == BAND_DIVE

    def test_30_degree_dive_is_not_steep(self):
        # sin(30°) = 0.5 → ~440 ft/s at 600 MPH. The flight-tested failure:
        # with steep_min_sin 0.5 this counted as steep and the eject settled
        # at a 30-degree dive; the 0.8 default keeps it in the dive band.
        assert pitch_band(600.0, -440.0) == BAND_DIVE

    def test_vertical_dive_at_600mph(self):
        # sin(90°) = 1 → ~880 ft/s descent at 600 MPH.
        assert pitch_band(600.0, -880.0) == BAND_STEEP_DIVE

    def test_steep_climb(self):
        assert pitch_band(600.0, 820.0) == BAND_STEEP_CLIMB

    def test_shallow_climb(self):
        assert pitch_band(600.0, 300.0) == BAND_CLIMB

    def test_missing_inputs_return_none(self):
        assert pitch_band(None, -300.0) is None
        assert pitch_band(600.0, None) is None

    def test_too_slow_for_meaningful_ratio(self):
        assert pitch_band(10.0, -10.0) is None

    def test_snapshot_pitch_band_requires_both_signals_fresh(self):
        p = _proc(stale_after_s=6.0)
        p.update(600, 20000, now_s=0.0)
        p.update(600, 18800, now_s=1.5)  # -800 ft/s at 600 MPH → ratio 0.91, steep dive
        assert p.snapshot(2.0).pitch_band() == BAND_STEEP_DIVE
        # Stale snapshot must return None — corrections need contrary
        # evidence, never absence of data (ADR 038).
        assert p.snapshot(20.0).pitch_band() is None


# ---------------------------------------------------------------------------
# Snapshot atomicity
# ---------------------------------------------------------------------------

class TestSnapshot:
    def test_snapshot_is_immutable(self):
        snap = TelemetrySnapshot()
        with pytest.raises(Exception):
            snap.taken_at_s = 99.0

    def test_reset_clears_both_signals(self):
        p = _proc()
        p.update(600, 20000, now_s=0.0)
        p.reset()
        snap = p.snapshot(1.0)
        assert snap.speed.value is None
        assert snap.altitude.value is None
