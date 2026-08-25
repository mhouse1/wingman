"""Comment-preserving config writer in calibrate.py (CR-015-05 / Future 002 A-08).

The regression: `_save_config` was `yaml.dump(cfg)`, so one `make calibrate` run
reformatted the whole file and deleted every comment in it — including the ADR
breadcrumbs above `eject_closed_loop`, `stall_recovery` and the missile-evade
block, which are the only in-file pointers to the decisions behind those values.
"""

import pathlib
import sys

import pytest
import yaml

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "tests"))

import calibrate  # noqa: E402  — needs the sys.path entry above

_CONFIG_PATH = _ROOT / "wingman" / "config.yaml"


@pytest.fixture
def cfg_copy(tmp_path):
    dest = tmp_path / "config.yaml"
    dest.write_text(_CONFIG_PATH.read_text(encoding="utf-8"), encoding="utf-8")
    return dest


def _comments(text):
    return [line.strip() for line in text.splitlines() if line.strip().startswith("#")]


def test_coord_edit_changes_only_the_coord_lines(cfg_copy):
    before = cfg_copy.read_text(encoding="utf-8")
    cfg = yaml.safe_load(before)

    calibrate._update_crop_in_config(cfg, "HEALTH", 0.1111, 0.2222, 0.3333, 0.4444)
    calibrate._save_config(cfg_copy, cfg)

    after = cfg_copy.read_text(encoding="utf-8")
    assert len(after.splitlines()) == len(before.splitlines()), "no lines added or removed"

    changed = [(a, b) for a, b in
               zip(before.splitlines(), after.splitlines(), strict=True) if a != b]
    assert len(changed) == 2, changed
    assert after.count("[0.1111, 0.2222]") == 1
    assert after.count("[0.3333, 0.4444]") == 1


def test_every_comment_survives_a_calibration_write(cfg_copy):
    before = cfg_copy.read_text(encoding="utf-8")
    cfg = yaml.safe_load(before)

    calibrate._update_crop_in_config(cfg, "PLAY", 0.5, 0.5, 0.6, 0.6)
    calibrate._save_config(cfg_copy, cfg)

    after = cfg_copy.read_text(encoding="utf-8")
    assert _comments(after) == _comments(before)
    assert "# ADR 084" in after, "the stall_recovery breadcrumb must survive"


def test_written_config_round_trips_to_the_intended_mapping(cfg_copy):
    cfg = yaml.safe_load(cfg_copy.read_text(encoding="utf-8"))
    calibrate._update_crop_in_config(cfg, "MINIMAP", 0.8, 0.01, 0.99, 0.27)
    calibrate._save_config(cfg_copy, cfg)

    assert yaml.safe_load(cfg_copy.read_text(encoding="utf-8")) == cfg


def test_written_config_still_passes_the_schema(cfg_copy):
    from wingman.config_schema import validate_config

    cfg = yaml.safe_load(cfg_copy.read_text(encoding="utf-8"))
    calibrate._update_crop_in_config(cfg, "CANCEL", 0.1, 0.2, 0.3, 0.4)
    calibrate._save_config(cfg_copy, cfg)

    assert validate_config(yaml.safe_load(cfg_copy.read_text(encoding="utf-8"))) == []


def test_new_crop_is_appended_without_disturbing_the_rest(cfg_copy):
    before = cfg_copy.read_text(encoding="utf-8")
    cfg = yaml.safe_load(before)
    cfg["crops"]["A_NEW_CROP"] = {"coords": [[0.1, 0.2], [0.3, 0.4]], "text": ["FOO", "BAR"]}

    calibrate._save_config(cfg_copy, cfg)

    after = cfg_copy.read_text(encoding="utf-8")
    assert yaml.safe_load(after) == cfg
    assert _comments(after) == _comments(before)
    assert "  A_NEW_CROP:\n    coords:\n" in after
    assert "    text: [FOO, BAR]\n" in after


def test_unrepresentable_change_backs_up_before_falling_back(cfg_copy, capsys):
    """A change the surgical path cannot express must not silently destroy the
    file: the original is preserved and the loss is announced."""
    cfg = yaml.safe_load(cfg_copy.read_text(encoding="utf-8"))
    del cfg["crops"]["HEALTH"]          # crop removal is not a coords-only edit

    calibrate._save_config(cfg_copy, cfg)

    backup = cfg_copy.with_suffix(".yaml.bak")
    assert backup.exists()
    assert "# ADR 084" in backup.read_text(encoding="utf-8")
    assert "WARNING" in capsys.readouterr().out
    assert yaml.safe_load(cfg_copy.read_text(encoding="utf-8")) == cfg
