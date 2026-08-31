"""ADR 096 — split the reaction metric into its segments.

`reaction` measures from the background OCR pass STARTING (`analyzer.py:2527`,
`current_time = t0`) to the tick handler acting on the result. It therefore
bundles two different things:

    reaction = detection duration + wait for tick pickup

A large value is consistent with a slow detector *or* a slow dispatch, and
HLDD 008's whole premise is that it is the second. These tests pin the split
that tells them apart, and that the historical `reaction` series is unchanged so
the 725-session baseline stays comparable.
"""

import json
import logging
import time
from pathlib import Path

from wingman.performance import PerformanceTracker, _load_run_file

logging.disable(logging.CRITICAL)

SEGMENTS = [(0.02, 0.31, 0.45), (0.03, 0.28, 0.51), (0.01, 0.33, 0.42)]


def _tracker(tmp):
    return PerformanceTracker(
        {"performance": {"round_histogram": {"enabled": True, "output_dir": str(tmp)}}},
        version="test")


def _write(tmp, segments=SEGMENTS, reaction=None):
    t = _tracker(tmp)
    for cp, de, di in segments:
        t.record_reaction_segments(cp, de, di)
    reaction = reaction if reaction is not None else [sum(s) for s in segments]
    t._write_run_file(crops={"incoming": [0.4, 0.5]}, reaction=reaction,
                      rounds=1, end_ts=time.time())
    return json.loads(next(Path(tmp, "current").glob("run_*.json")).read_text())


def test_segments_reach_the_run_file(tmp_path):
    j = _write(tmp_path)
    rs = j["reaction_segments"]
    assert rs["n"] == len(SEGMENTS)
    for name in ("capture_to_pass", "detect", "dispatch"):
        assert name in rs, f"{name} missing from the split"


def test_segments_sum_to_the_total(tmp_path):
    """ADR 096 V3 — the breakdown must account for the measured total, or it is
    describing a different path than the one being optimised."""
    j = _write(tmp_path)
    rs = j["reaction_segments"]
    total = sum(rs[k]["mean"] for k in ("capture_to_pass", "detect", "dispatch"))
    assert abs(total - j["reaction"]["mean"]) < 1e-6


def test_the_historical_reaction_series_is_unchanged(tmp_path):
    """The 725-session baseline must stay comparable — `reaction` keeps its
    meaning and the split is added beside it, not in place of it."""
    j = _write(tmp_path)
    assert j["reaction"]["n"] == len(SEGMENTS)
    # The writer rounds stored stats to 4dp, so compare at that precision.
    expected = sum(sum(s) for s in SEGMENTS) / len(SEGMENTS)
    assert abs(j["reaction"]["mean"] - expected) < 1e-4


def test_absent_segments_are_null_not_missing(tmp_path):
    """A session with no missile engagements still writes a well-formed file."""
    t = _tracker(tmp_path)
    t._write_run_file(crops={"incoming": [0.4]}, reaction=[], rounds=1, end_ts=time.time())
    j = json.loads(next(Path(tmp_path, "current").glob("run_*.json")).read_text())
    assert j["reaction_segments"] is None


def test_pre_adr096_files_still_load(tmp_path):
    """ADR 092's aggregator reads the whole archive; 725 sessions predate this."""
    j = _write(tmp_path)
    j.pop("reaction_segments")
    p = tmp_path / "run_old.json"
    p.write_text(json.dumps(j))
    assert _load_run_file(p) is not None


def test_recording_never_raises(tmp_path):
    """Diagnostic code on the flare path must not be able to break it."""
    t = _tracker(tmp_path)
    t.record_reaction_segments(float("nan"), 0.0, 0.0)     # must not raise
    t.record_reaction_segments(-1.0, -1.0, -1.0)


def test_disabled_tracker_records_nothing(tmp_path):
    t = PerformanceTracker({"performance": {"round_histogram": {"enabled": False}}},
                           version="test")
    t.record_reaction_segments(0.1, 0.2, 0.3)              # must be a no-op


# --- the analyzer side ------------------------------------------------------

def test_analyzer_exposes_latency_marks():
    """The tick handler needs three marks to compute the split."""
    from wingman.analyzer import GameStateAnalyzer
    assert hasattr(GameStateAnalyzer, "get_incoming_latency_marks")


def test_marks_are_zero_before_any_detection():
    """Zeros mean 'no data', and the tick handler must skip rather than record
    a fabricated segment."""
    import threading
    from wingman.analyzer import GameStateAnalyzer as G
    stub = G.__new__(G)
    stub._incoming_cache = {"result": (False, 0.0, None), "timestamp": 0.0,
                            "frame_ts": 0.0, "detect_done_ts": 0.0}
    stub._incoming_cache_lock = threading.Lock()
    assert G.get_incoming_latency_marks(stub) == (0.0, 0.0, 0.0)


def test_tick_handler_skips_when_marks_are_absent():
    """Pre-instrumentation state must not produce a bogus split."""
    src = Path("wingman/tick_handlers.py").read_text()
    assert "if frame_ts and detect_done_ts and detect_done_ts >= pass_ts:" in src, \
        "the split must be guarded on the marks being present and ordered"


def test_split_failure_cannot_cost_the_flare_burst():
    """The split is computed in its own try/except, after record_reaction."""
    src = Path("wingman/tick_handlers.py").read_text()
    i_react = src.index("self._perf.record_reaction(now - incoming_ts)")
    i_split = src.index("get_incoming_latency_marks")
    i_flare = src.index("def _flare_burst")
    assert i_react < i_split < i_flare, "ordering: total, then split, then flares"
    assert "ADR096: reaction split unavailable" in src, "the split must swallow its own errors"
