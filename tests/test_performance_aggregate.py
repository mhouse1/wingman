"""Tests for `_aggregate_folder` artifact selection.

MissionStatsTracker (ADR 055) writes `run_<id>_stats.json` beside the
PerformanceTracker's `run_<id>.json`. Both match `glob("run_*.json")`, and
"_stats.json" sorts AFTER ".json", so an unfiltered listing puts a stats file
last — which is exactly the file the aggregate reads its percentiles and its
version label from.

Observed on the 2026-08-23 session before the fix: release reported 901
sessions against 616 real ones, every aggregate p50/p95/p99 came back 0.0, and
the version label resolved to "?" — which made the version-filtered regression
lane match zero sessions and silently do nothing.
"""

import json

from wingman.performance import _aggregate_folder, _load_run_file


def _run(path, version="1.8.5", n=100, mean=0.5, p50=0.4, p95=0.9, p99=1.2):
    path.write_text(json.dumps({
        "version": version,
        "run_id": path.stem,
        "start_ts": 1.0,
        "end_ts": 2.0,
        "rounds": [],
        "ocr_crops": {
            "incoming": {"n": n, "mean": mean, "p50": p50, "p95": p95, "p99": p99},
        },
        "reaction": {"n": 5, "mean": 0.3, "p50": 0.3, "p95": 0.7, "p99": 0.9},
    }), encoding="utf-8")


def _stats(path, version="1.8.5"):
    """A MissionStatsTracker artifact — note `wingman_version`, no `ocr_crops`."""
    path.write_text(json.dumps({
        "wingman_version": version,
        "run_id": path.stem,
        "session_start_ts": 1.0,
        "session_duration_s": 60,
        "missions_started": 3,
    }), encoding="utf-8")


def test_stats_file_is_not_loaded_as_a_run(tmp_path):
    _stats(tmp_path / "run_20260823_000000_stats.json")
    assert _load_run_file(tmp_path / "run_20260823_000000_stats.json") is None


def test_stats_files_excluded_from_session_count(tmp_path):
    _run(tmp_path / "run_20260823_000001.json")
    _run(tmp_path / "run_20260823_000002.json")
    _stats(tmp_path / "run_20260823_000001_stats.json")
    _stats(tmp_path / "run_20260823_000002_stats.json")

    agg = _aggregate_folder(tmp_path)
    assert agg["session_count"] == 2, "stats files must not be counted as sessions"


def test_percentiles_come_from_the_newest_run_not_a_stats_file(tmp_path):
    _run(tmp_path / "run_20260823_000001.json", p50=0.1, p95=0.2, p99=0.3)
    _run(tmp_path / "run_20260823_000002.json", p50=0.4, p95=0.9, p99=1.2)
    # Sorts last of the three; before the fix it supplied the percentiles.
    _stats(tmp_path / "run_20260823_000002_stats.json")

    incoming = _aggregate_folder(tmp_path)["ocr_crops"]["incoming"]
    assert (incoming["p50"], incoming["p95"], incoming["p99"]) == (0.4, 0.9, 1.2)


def test_version_label_comes_from_the_newest_run_not_a_stats_file(tmp_path):
    _run(tmp_path / "run_20260823_000001.json", version="1.8.4")
    _run(tmp_path / "run_20260823_000002.json", version="1.8.5")
    _stats(tmp_path / "run_20260823_000002_stats.json")

    assert _aggregate_folder(tmp_path)["version"] == "1.8.5"


def test_version_filter_matches_after_stats_exclusion(tmp_path):
    """The regression lane's whole purpose: compare like version with like."""
    _run(tmp_path / "run_20260823_000001.json", version="1.8.4")
    _run(tmp_path / "run_20260823_000002.json", version="1.8.5")
    _run(tmp_path / "run_20260823_000003.json", version="1.8.5")
    _stats(tmp_path / "run_20260823_000003_stats.json")

    agg = _aggregate_folder(tmp_path)
    filtered = _aggregate_folder(tmp_path, version_filter=agg["version"])
    assert filtered is not None, "version baseline must not be empty"
    assert filtered["session_count"] == 2


def test_means_are_weighted_by_n_and_unaffected_by_stats_files(tmp_path):
    _run(tmp_path / "run_20260823_000001.json", n=100, mean=1.0)
    _run(tmp_path / "run_20260823_000002.json", n=300, mean=2.0)
    _stats(tmp_path / "run_20260823_000002_stats.json")

    incoming = _aggregate_folder(tmp_path)["ocr_crops"]["incoming"]
    assert incoming["n"] == 400
    assert incoming["mean"] == 1.75  # (100*1.0 + 300*2.0) / 400


def test_unrelated_json_under_the_run_prefix_is_skipped(tmp_path):
    """A future sibling artifact must be excluded by default, not aggregated."""
    _run(tmp_path / "run_20260823_000001.json")
    (tmp_path / "run_20260823_000001_notes.json").write_text(
        json.dumps({"version": "1.8.5", "note": "not a run"}), encoding="utf-8")

    assert _aggregate_folder(tmp_path)["session_count"] == 1


def test_folder_with_only_stats_files_aggregates_to_nothing(tmp_path):
    _stats(tmp_path / "run_20260823_000001_stats.json")
    assert _aggregate_folder(tmp_path) is None
