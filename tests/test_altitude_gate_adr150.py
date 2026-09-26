"""Digit-drop altitude rejection (ADR 150).

On 2026-09-26 from 01:54 to 02:41, 16 hard-emergency climbs inside a pursuit were
started by an altitude read with a digit lost: the true value, then about a
tenth of it, then the true value again 3 s later. ADR 097's 1000 m/s ceiling
admits a 2766 m drop across a 3 s gap, so each one was accepted and gave "2s to
ground". The sequences below are from wingman.log.
"""

from wingman.telemetry import TelemetryProcessor

# (altitude_raw, speed_raw) at the real ~3 s cadence.
ALT_0225 = [(3078, 159), (312, 260), (2993, 347)]        # 02:25:39-45
ALT_0239 = [(2817, 231), (2, 259), (2830, 240)]           # 02:39:40-46
ALT_0226 = [(3100, 250), (334, 241), (3396, 82)]         # 02:26:12-15, true value was rejected


def _proc(**over):
    cfg = {"max_speed_mph": 2000, "max_altitude_ft": 60000, "plausibility_margin": 1.5,
           "stale_after_s": 6.0, "smoothing_window": 3, "reseed_after_rejections": 3,
           "max_alt_rate_mps": 1000.0, "reseed_agreement_m": 150.0,
           "digit_drop_ratio": 0.2}
    cfg.update(over)
    return TelemetryProcessor(cfg)


def _feed(proc, rows, t0=1000.0, step=3.0):
    seen = []
    for i, (alt, spd) in enumerate(rows):
        proc.update(spd, alt, t0 + i * step)
        seen.append(proc.snapshot(t0 + i * step).altitude)
    return seen


def test_a_dropped_digit_is_rejected():
    seen = _feed(_proc(), ALT_0225)
    assert [s.value for s in seen] == [3078, 3078, 2993]


def test_a_single_digit_read_is_rejected():
    seen = _feed(_proc(), ALT_0239)
    assert [s.value for s in seen] == [2817, 2817, 2830]


def test_the_true_value_after_a_drop_is_not_rejected():
    """02:26:15: the filter accepted 334 and then rejected the true 3396."""
    seen = _feed(_proc(), ALT_0226)
    assert seen[-1].value == 3396


def test_no_impossible_rate_is_published():
    for rows in (ALT_0225, ALT_0239, ALT_0226):
        for s in _feed(_proc(), rows):
            assert s.rate is None or abs(s.rate) < 300, rows


def test_the_old_filter_accepted_them():
    """What the change is measured against: ratio 0 restores ADR 097 alone."""
    seen = _feed(_proc(digit_drop_ratio=0.0), ALT_0225)
    assert seen[1].value == 312


def test_two_dropped_digits_that_agree_do_not_reseed():
    """ADR 097 D3 reseeds on two agreeing rejects; these must not count."""
    seen = _feed(_proc(), [(3078, 250), (312, 250), (311, 250)])
    assert seen[-1].value == 3078


def test_a_real_descent_is_kept():
    # 3300 m to 2700 m at about 100 m/s, the steepest real dives seen tonight.
    rows = [(3300, 700), (3000, 720), (2700, 740), (2400, 760)]
    assert [s.value for s in _feed(_proc(), rows)] == [3300, 3000, 2700, 2400]


def test_a_low_anchor_is_not_gated():
    """Below 1000 m a real descent can cross the ratio within one gap."""
    rows = [(900, 500), (150, 500)]
    assert _feed(_proc(), rows)[-1].value == 150


def test_a_stale_anchor_is_not_gated():
    proc = _proc()
    proc.update(250, 3000, 1000.0)
    proc.update(250, 400, 1000.0 + 10.0)     # past stale_after_s: a fresh seed
    assert proc.snapshot(1010.0).altitude.value == 400


def test_a_persistent_low_read_is_not_held_off_forever():
    """Rejects leave the anchor's timestamp alone, so once it is older than
    stale_after_s (6 s, about two readings) the gate stands aside and a low
    reading seeds fresh, with no rate, so no false "2s to ground"."""
    proc = _proc()
    rows = [(3000, 250), (300, 250), (300, 250), (300, 250)]
    seen = _feed(proc, rows)
    assert [s.value for s in seen[:3]] == [3000, 3000, 3000]
    assert seen[3].value == 300
    assert seen[3].rate is None


def test_shipped_config():
    import yaml
    with open("wingman/config.yaml") as fh:
        assert yaml.safe_load(fh)["telemetry"]["digit_drop_ratio"] == 0.2
