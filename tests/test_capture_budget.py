"""Cross-session disk budget for capture features (wingman/capture_budget.py)."""

import os
from collections import namedtuple

import pytest

from wingman import capture_budget

_Usage = namedtuple("_Usage", "total used free")


@pytest.fixture(autouse=True)
def _reset_budget():
    """Module state is global — restore defaults around every test."""
    capture_budget.configure({})
    yield
    capture_budget.configure({})


def _make(directory, names, size=10):
    directory.mkdir(parents=True, exist_ok=True)
    for i, name in enumerate(names):
        p = directory / name
        p.write_bytes(b"x" * size)
        os.utime(p, (1000 + i, 1000 + i))       # oldest first, deterministic


def test_prune_deletes_oldest_down_to_file_cap(tmp_path):
    _make(tmp_path, [f"f{i}.png" for i in range(5)])
    deleted, freed = capture_budget.prune(tmp_path, max_files=3)
    assert (deleted, freed) == (2, 20)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["f2.png", "f3.png", "f4.png"]


def test_prune_reserve_leaves_room_for_the_next_write(tmp_path):
    _make(tmp_path, [f"f{i}.png" for i in range(3)])
    capture_budget.prune(tmp_path, max_files=3, reserve=1)
    assert len(list(tmp_path.iterdir())) == 2


def test_prune_by_bytes(tmp_path):
    _make(tmp_path, [f"f{i}.png" for i in range(4)], size=100)
    capture_budget.prune(tmp_path, max_bytes=250)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["f2.png", "f3.png"]


def test_prune_only_touches_matching_top_level_files(tmp_path):
    _make(tmp_path, ["screenshot_a.png", "screenshot_b.png", "live_hud.png", "notes.txt"])
    _make(tmp_path / "curated", ["keep.png"])
    capture_budget.prune(tmp_path, "screenshot_*.png", max_files=1)
    names = sorted(p.name for p in tmp_path.iterdir())
    assert names == ["curated", "live_hud.png", "notes.txt", "screenshot_b.png"]
    assert (tmp_path / "curated" / "keep.png").exists()


def test_prune_zero_means_unlimited(tmp_path):
    _make(tmp_path, [f"f{i}.png" for i in range(5)])
    assert capture_budget.prune(tmp_path, max_files=0, max_bytes=0) == (0, 0)
    assert len(list(tmp_path.iterdir())) == 5


def test_prune_missing_directory_is_a_noop(tmp_path):
    assert capture_budget.prune(tmp_path / "nope", max_files=1) == (0, 0)


def test_admit_uses_per_dir_override(tmp_path):
    target = tmp_path / "tt"
    _make(target, [f"f{i}.png" for i in range(5)])
    capture_budget.configure({"capture_budget": {
        "min_free_gb": 0, "dirs": {str(target): {"max_files": 3}}}})
    assert capture_budget.admit(target, "test") is True
    assert len(list(target.iterdir())) == 2      # room left for the write


def test_admit_refuses_below_free_space_floor(tmp_path, monkeypatch, caplog):
    _make(tmp_path, ["f0.png"])
    capture_budget.configure({"capture_budget": {"min_free_gb": 10}})
    monkeypatch.setattr(capture_budget.shutil, "disk_usage",
                        lambda p: _Usage(100, 99, 5 * 1024 ** 3))
    with caplog.at_level("DEBUG", logger="wingman.capture_budget"):
        assert capture_budget.admit(tmp_path, "label") is False
        assert capture_budget.admit(tmp_path, "label") is False
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1                    # warned once, not every tick
    assert (tmp_path / "f0.png").exists()        # refusing never deletes


def test_admit_measures_nearest_existing_ancestor(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(capture_budget.shutil, "disk_usage",
                        lambda p: seen.append(p) or _Usage(1, 0, 10 ** 15))
    assert capture_budget.admit(tmp_path / "a" / "b", "x") is True
    assert seen == [tmp_path.resolve()]


def test_admit_never_raises(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("disk gone")
    monkeypatch.setattr(capture_budget, "prune", boom)
    assert capture_budget.admit(tmp_path, "x") is True


def test_section_budget_reads_config_and_falls_back():
    cfg = {"capture_budget": {"rotated_logs": {"max_files": 7, "max_mb": 1}}}
    assert capture_budget.section_budget(cfg, "rotated_logs", (1, 2)) == (7, 1024 ** 2)
    assert capture_budget.section_budget({}, "session_video", (8, 9)) == (8, 9)


def test_shipped_config_validates_budget_block():
    from wingman.main import load_config
    cfg = load_config("wingman/config.yaml")
    assert cfg["capture_budget"]["min_free_gb"] > 0


def test_rolling_prunes_after_the_first_log_at_debug(tmp_path, caplog):
    """2026-09-24 live: one INFO line per archived frame once at budget."""
    _make(tmp_path, [f"f{i}.png" for i in range(5)])
    with caplog.at_level("DEBUG", logger="wingman.capture_budget"):
        capture_budget.prune(tmp_path, max_files=3)
        _make(tmp_path, ["g0.png", "g1.png"])
        capture_budget.prune(tmp_path, max_files=3)
    levels = [r.levelname for r in caplog.records if "pruned" in r.message]
    assert levels == ["INFO", "DEBUG"]
