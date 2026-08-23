"""Tests for the ResourceSampler (Performance 008 leak diagnosis).

The sampler is a diagnostic: correctness matters, but *never breaking the main
loop* matters more. These tests cover the throttle, the emitted contract, the
read-only window accessor, and the never-raises guarantee.
"""

import pytest

from wingman.performance import PerformanceTracker
from wingman.resource_monitor import ResourceSampler


class _Clock:
    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def _tracker(tmp_path):
    return PerformanceTracker(
        {"performance": {"round_histogram": {"enabled": True,
                                             "output_dir": str(tmp_path)}}},
        version="test",
    )


def test_first_sample_is_immediate_then_throttled():
    """Every session needs a t=0 baseline to measure growth against; after
    that the sampler must stay quiet until the interval elapses."""
    clock = _Clock()
    s = ResourceSampler({"interval_s": 300.0}, clock=clock)

    assert s.maybe_sample() is not None, "no baseline sample at session start"
    assert s.maybe_sample() is None, "sampled twice inside one interval"

    clock.advance(299.0)
    assert s.maybe_sample() is None
    clock.advance(2.0)
    assert s.maybe_sample() is not None


def test_line_carries_every_diagnostic_field():
    """The line is the whole product — grep RESOURCE must yield a parseable
    row with each field the 2026-08-20 analysis had to reconstruct by hand."""
    line = ResourceSampler({"interval_s": 1.0}, clock=_Clock()).maybe_sample()
    assert line is not None
    for field in ("elapsed=", "rss_mb=", "swap_mb=", "threads=", "fds=",
                  "gc=(", "ocr_med=", "ocr_p95=", "n_ocr=",
                  "game_rss_mb=", "game_swap_mb=", "sys_swap_mb="):
        assert field in line, f"missing diagnostic field {field!r}: {line}"


def test_disabled_sampler_emits_nothing():
    s = ResourceSampler({"enabled": False, "interval_s": 1.0}, clock=_Clock())
    assert s.maybe_sample() is None


def test_interval_has_a_floor():
    """A misconfigured tiny interval would sample every tick and drown the log."""
    s = ResourceSampler({"interval_s": 0.001}, clock=_Clock())
    assert s._interval_s >= 10.0


def test_ocr_window_is_per_interval_not_cumulative(tmp_path):
    """The window must show the CURRENT interval's OCR health. Cumulative
    stats would mask exactly the degradation this exists to detect."""
    clock = _Clock()
    tracker = _tracker(tmp_path)
    s = ResourceSampler({"interval_s": 100.0}, perf_tracker=tracker, clock=clock)

    # Baseline sample marks the starting position; nothing recorded yet.
    assert "n_ocr=0" in s.maybe_sample()

    for _ in range(10):
        tracker.record_ocr_crop("respawn", 0.25)
    clock.advance(101.0)
    line = s.maybe_sample()
    assert "n_ocr=10" in line
    assert "ocr_med=0.25" in line

    # A degraded interval must report ITS OWN numbers, not blended history.
    for _ in range(10):
        tracker.record_ocr_crop("respawn", 4.00)
    clock.advance(101.0)
    line = s.maybe_sample()
    assert "n_ocr=10" in line, "window leaked samples from the previous interval"
    assert "ocr_med=4.00" in line, f"cumulative blending masked degradation: {line}"


def test_snapshot_since_does_not_drain_the_tracker(tmp_path):
    """Read-only contract: the sampler must not disturb the session buffers
    the regression gate and session summary depend on."""
    tracker = _tracker(tmp_path)
    for _ in range(5):
        tracker.record_ocr_crop("respawn", 0.3)

    _, marks = tracker.snapshot_since(None)
    window, _ = tracker.snapshot_since(marks)
    assert window["respawn"] == []          # nothing new since the mark

    for _ in range(3):
        tracker.record_ocr_crop("respawn", 0.4)
    window, _ = tracker.snapshot_since(marks)
    assert window["respawn"] == [0.4, 0.4, 0.4]

    # All 8 samples must still be in the session buffer.
    with tracker._lock:
        assert len(tracker._session_crops["respawn"]) == 8


def test_sampler_never_raises_when_probes_fail(monkeypatch):
    """A diagnostic must not be able to kill the main loop. Break every probe
    and the sampler must still return a line (or None), never raise."""
    import wingman.resource_monitor as rm

    monkeypatch.setattr(rm, "_PROC", "/nonexistent-proc-path")

    def _boom(*_a, **_k):
        raise OSError("probe exploded")

    monkeypatch.setattr(rm.os, "listdir", _boom)
    monkeypatch.setattr(rm.gc, "get_count", _boom)

    s = ResourceSampler({"interval_s": 1.0}, clock=_Clock())
    s.maybe_sample()  # must not raise


def test_broken_perf_tracker_does_not_break_sampling():
    """A failing OCR window must degrade to n/a, not lose the whole line."""
    class _Exploding:
        def snapshot_since(self, _offsets):
            raise RuntimeError("tracker exploded")

    line = ResourceSampler({"interval_s": 1.0}, perf_tracker=_Exploding(),
                           clock=_Clock()).maybe_sample()
    assert line is not None
    assert "ocr_med=n/a" in line
    assert "rss_mb=" in line, "a tracker failure cost us the memory fields"


@pytest.mark.skipif(not __import__("os").path.isdir("/proc/self"),
                    reason="/proc not available (non-Linux)")
def test_self_memory_fields_are_real_numbers():
    line = ResourceSampler({"interval_s": 1.0}, clock=_Clock()).maybe_sample()
    rss = [tok for tok in line.split() if tok.startswith("rss_mb=")][0]
    assert rss != "rss_mb=n/a"
    assert int(rss.split("=")[1]) > 0


def test_game_process_absent_reports_na():
    """No game running is a normal state (lobby-less test runs, CI) and must
    read as n/a rather than 0, which would look like a game with no memory."""
    line = ResourceSampler({"interval_s": 1.0,
                            "game_process_name": "definitely-not-a-real-process"},
                           clock=_Clock()).maybe_sample()
    assert "game_rss_mb=n/a" in line


# ---------------------------------------------------------------------------
# Session-end summary and leak attribution
# ---------------------------------------------------------------------------

def _sampler_with_memory(clock, series, game_series=None, step_s=3600.0, cfg=None):
    """Drive a sampler through a scripted RSS series (MB), one sample per step."""
    s = ResourceSampler({"interval_s": 1.0, "warmup_s": 0.0, **(cfg or {})}, clock=clock)
    for i, rss in enumerate(series):
        game = None if game_series is None else game_series[i]
        obs = {"t": clock(), "rss": rss, "swap": 0, "peak": rss, "fds": 100,
               "threads": 20, "game_rss": game, "game_swap": 0,
               "ocr_med": None, "sys_swap": 1000}
        if s._first is None:
            s._first = dict(obs)
        if s._anchor is None and (clock() - s._session_start) >= s._warmup_s:
            s._anchor = dict(obs)
        s._last = dict(obs)
        s._samples += 1
        clock.advance(step_s)
    return s


def test_verdict_attributes_growth_to_wingman():
    """The whole point of the feature: name the leaking process."""
    clock = _Clock()
    s = _sampler_with_memory(clock, [400, 1400, 2400], game_series=[4000, 4010, 4020])
    out = s.summarize()
    assert "WINGMAN-SIDE" in out, out
    assert "MB/h" in out


def test_verdict_attributes_growth_to_game():
    """The opposite finding must be stated just as plainly — wingman would be
    a victim, and the Performance 008 remedy would be entirely different."""
    clock = _Clock()
    s = _sampler_with_memory(clock, [400, 405, 410], game_series=[4000, 6000, 8000])
    out = s.summarize()
    assert "GAME-SIDE" in out, out


def test_verdict_reports_both_when_both_grow():
    clock = _Clock()
    s = _sampler_with_memory(clock, [400, 1400, 2400], game_series=[4000, 5500, 7000])
    out = s.summarize()
    assert "BOTH" in out, out


def test_verdict_declines_to_call_a_leak_on_flat_session():
    """A clean session must read as clean; warm-up drift is not a leak."""
    clock = _Clock()
    s = _sampler_with_memory(clock, [400, 410, 420], game_series=[4000, 4005, 4010])
    out = s.summarize()
    assert "no leak observed" in out, out


def test_verdict_refuses_attribution_on_short_session():
    """A 10-minute run cannot support a MB/h claim — say so instead of
    extrapolating a rate from noise."""
    clock = _Clock()
    s = ResourceSampler({"interval_s": 1.0}, clock=clock)
    for rss in (400, 900):
        obs = {"t": clock(), "rss": rss, "swap": 0, "peak": rss, "fds": 100,
               "threads": 20, "game_rss": 4000, "game_swap": 0,
               "ocr_med": None, "sys_swap": 1000}
        if s._first is None:
            s._first = dict(obs)
        s._last = dict(obs)
        s._samples += 1
        clock.advance(300.0)
    out = s.summarize()
    # Either refusal is correct — no anchor yet, or too short a measured
    # window. What matters is that no attribution is claimed.
    assert ("too short" in out) or ("warming up" in out), out
    assert "WINGMAN-SIDE" not in out


def test_summary_needs_two_samples():
    s = ResourceSampler({"interval_s": 1.0}, clock=_Clock())
    assert s.summarize() is None          # nothing sampled
    s.maybe_sample()
    assert s.summarize() is None          # one point is not a trend


def test_summary_never_raises_on_partial_data():
    """Missing game data (no game running) must still yield a summary."""
    clock = _Clock()
    s = _sampler_with_memory(clock, [400, 1400, 2400], game_series=None)
    out = s.summarize()
    assert out is not None
    assert "game    rss n/a" in out


def test_line_carries_deltas_and_pool_depth():
    """Deltas make the curve readable without arithmetic; pool depth closes
    FUTURE 001 item 5."""
    s = ResourceSampler({"interval_s": 1.0}, clock=_Clock())
    s.set_pool_depth_source(lambda: 7)
    line = s.maybe_sample()
    for field in ("d_rss=", "d_threads=", "d_fds=", "peak_rss_mb=",
                  "pool_depth=7", "d_game_rss="):
        assert field in line, f"missing {field!r}: {line}"


def test_pool_depth_source_failure_degrades_to_na():
    def _boom():
        raise RuntimeError("pool gone")
    s = ResourceSampler({"interval_s": 1.0}, clock=_Clock())
    s.set_pool_depth_source(_boom)
    line = s.maybe_sample()
    assert "pool_depth=n/a" in line


def test_warmup_allocation_is_not_reported_as_a_leak():
    """Regression, live-observed 2026-08-20 23:09: wingman allocates ~3.9 GB in
    its first 5 minutes loading 13 thread-local EasyOCR readers (rss 681 ->
    4598 MB, threads 2 -> 22). Amortising that one-off across an 8-hour session
    yields ~490 MB/h and would report a WINGMAN-SIDE leak on a clean session.
    Rates must be measured from a post-warm-up anchor."""
    clock = _Clock()
    s = ResourceSampler({"interval_s": 1.0, "warmup_s": 600.0}, clock=clock)

    # t=0 baseline, pre-model-load.
    for rss in (681, 4598):
        obs = {"t": clock(), "rss": rss, "swap": 0, "peak": rss, "fds": 70,
               "threads": 22, "game_rss": 1200, "game_swap": 0,
               "ocr_med": 0.22, "sys_swap": 3255}
        if s._first is None:
            s._first = dict(obs)
        if s._anchor is None and (clock() - s._session_start) >= s._warmup_s:
            s._anchor = dict(obs)
        s._last = dict(obs)
        s._samples += 1
        clock.advance(300.0)

    # Post-warm-up: eight hours of a genuinely FLAT process.
    for _ in range(8):
        obs = {"t": clock(), "rss": 4610, "swap": 0, "peak": 4620, "fds": 70,
               "threads": 22, "game_rss": 1210, "game_swap": 0,
               "ocr_med": 0.23, "sys_swap": 3255}
        if s._anchor is None and (clock() - s._session_start) >= s._warmup_s:
            s._anchor = dict(obs)
        s._last = dict(obs)
        s._samples += 1
        clock.advance(3600.0)

    out = s.summarize()
    assert "WINGMAN-SIDE" not in out, f"warm-up misreported as a leak:\n{out}"
    assert "no leak observed" in out, out
    # The absolute span is still shown — the warm-up is not hidden, just not
    # amortised into the rate.
    assert "681->4610MB" in out, out


def test_real_leak_after_warmup_is_still_caught():
    """The anchor must not blind the detector to an actual leak."""
    clock = _Clock()
    s = _sampler_with_memory(
        clock, [4600, 5600, 6600, 7600], game_series=[1200, 1205, 1210, 1215],
        cfg={"warmup_s": 0.0})
    out = s.summarize()
    assert "WINGMAN-SIDE" in out, out


def test_verdict_withheld_before_warmup_completes():
    """A session that ends inside warm-up has no anchor and must say so."""
    clock = _Clock()
    s = ResourceSampler({"interval_s": 1.0, "warmup_s": 600.0}, clock=clock)
    for rss in (681, 4598):
        obs = {"t": clock(), "rss": rss, "swap": 0, "peak": rss, "fds": 70,
               "threads": 22, "game_rss": 1200, "game_swap": 0,
               "ocr_med": 0.22, "sys_swap": 3255}
        if s._first is None:
            s._first = dict(obs)
        s._last = dict(obs)
        s._samples += 1
        clock.advance(120.0)
    out = s.summarize()
    assert "warming up" in out, out
    assert "WINGMAN-SIDE" not in out


def test_severe_leak_is_named_even_in_a_short_window():
    """Regression, live 2026-08-20 23:04: wingman leaked 15.2 GB in 25 minutes
    (+36,300 MB/h) and the short-window guard withheld a verdict because the
    window was 0.4h. The guard protects against extrapolating noise, not
    against reporting a catastrophe."""
    clock = _Clock()
    s = ResourceSampler({"interval_s": 1.0, "warmup_s": 0.0}, clock=clock)
    for rss in (681, 15879):
        obs = {"t": clock(), "rss": rss, "swap": 0, "peak": rss, "fds": 69,
               "threads": 23, "game_rss": 1139, "game_swap": 0,
               "ocr_med": None, "sys_swap": 3267}
        if s._first is None:
            s._first = dict(obs)
        if s._anchor is None:
            s._anchor = dict(obs)
        s._last = dict(obs)
        s._samples += 1
        clock.advance(1500.0)          # 25 minutes
    out = s.summarize()
    assert "SEVERE" in out, f"catastrophic leak went unreported:\n{out}"
    assert "WINGMAN" in out, out
    assert "too short" not in out


def test_moderate_growth_in_short_window_still_withheld():
    """The escape hatch must not swallow the ordinary short-window guard."""
    clock = _Clock()
    s = ResourceSampler({"interval_s": 1.0, "warmup_s": 0.0}, clock=clock)
    for rss in (4000, 4100):           # +100 MB over 30 min = 200 MB/h
        obs = {"t": clock(), "rss": rss, "swap": 0, "peak": rss, "fds": 69,
               "threads": 23, "game_rss": 1139, "game_swap": 0,
               "ocr_med": None, "sys_swap": 3267}
        if s._first is None:
            s._first = dict(obs)
        if s._anchor is None:
            s._anchor = dict(obs)
        s._last = dict(obs)
        s._samples += 1
        clock.advance(1800.0)
    out = s.summarize()
    assert "too short" in out, out
    assert "SEVERE" not in out


# ---------------------------------------------------------------------------
# Performance 008: live-vs-retained split from the allocator
# ---------------------------------------------------------------------------

def test_malloc_stats_probe_returns_sane_fields():
    """glibc mallinfo2 distinguishes what RSS and anon cannot: memory in USE
    from memory freed and retained in the arena (fragmentation)."""
    from wingman.resource_monitor import _read_malloc_stats
    m = _read_malloc_stats()
    if not m:
        import pytest
        pytest.skip("mallinfo2 unavailable on this libc")
    for k in ("mi_use", "mi_free", "mi_mmap"):
        assert k in m, f"missing {k}"
        assert isinstance(m[k], int) and m[k] >= 0, f"{k}={m[k]!r}"


def test_malloc_probe_never_raises(monkeypatch):
    """A diagnostic must not be able to break the tick loop."""
    import wingman.resource_monitor as rm

    def _boom():
        raise OSError("libc exploded")

    monkeypatch.setattr(rm, "_mallinfo2", _boom)
    assert rm._read_malloc_stats() == {}


def test_malloc_probe_degrades_when_unavailable(monkeypatch):
    import wingman.resource_monitor as rm
    monkeypatch.setattr(rm, "_mallinfo2", None)
    assert rm._read_malloc_stats() == {}


# ---------------------------------------------------------------------------
# ADR 090: memory guard — bound the unfixed Performance 008 leak
# ---------------------------------------------------------------------------

def _guard(**over):
    from wingman.resource_monitor import ResourceSampler
    cfg = {"enabled": True, "interval_s": 10.0,
           "memory_guard": {"enabled": True, "soft_limit_mb": 6000,
                            "hard_limit_mb": 10000}}
    cfg["memory_guard"].update(over)
    return ResourceSampler(cfg)


def test_guard_quiet_below_the_soft_limit():
    s = _guard()
    s._guard_enabled = True
    assert s.should_stop(at_safe_point=True) is False


def test_soft_limit_waits_for_a_safe_point():
    """Stopping mid-mission abandons an aircraft in flight; the lobby is free."""
    s = _guard()
    s._guard_armed = True
    assert s.should_stop(at_safe_point=False) is False, "stopped mid-mission"
    assert s.should_stop(at_safe_point=True) is True


def test_hard_limit_stops_regardless_of_state():
    """Past the hard limit an OOM kill can take the desktop session with it —
    that outweighs one abandoned mission."""
    s = _guard()
    s._guard_hard = True
    assert s.should_stop(at_safe_point=False) is True


def test_guard_can_be_disabled():
    s = _guard(enabled=False)
    s._guard_hard = True
    s._guard_armed = True
    assert s.should_stop(at_safe_point=True) is False


def test_guard_reason_names_the_threshold_that_fired():
    s = _guard()
    s._guard_armed = True
    assert "soft" in s.guard_reason()
    s._guard_hard = True
    assert "hard" in s.guard_reason()
