"""A replay path pins the configuration it models (`config_overrides:`).

The shipped default mission became su30 on 2026-09-24, whose pursuit leaves
GAME_BATTLE before missiles run out. PATH1 asserts the J20 missiles-empty eject,
so `make rr-path1-gate` failed on every run from then on, identically before and
after the code review 018 changes (measured 2026-09-27).
"""

import pathlib

import pytest
import yaml

from wingman.config_local import merge
from wingman.config_schema import validate_config
from wingman.replay import load_replay_config_overrides, load_replay_paths

PATH1 = pathlib.Path("tests/replay_paths/adr044_runtime_path1.yaml")


def test_path1_flies_the_mission_its_checkpoints_model():
    assert load_replay_config_overrides(PATH1) == {"mission": {"default_mission": "j20"}}


def test_the_override_block_is_not_mistaken_for_a_path():
    assert set(load_replay_paths(PATH1)) == {"PATH1_RUNTIME"}


def test_the_overridden_config_still_passes_the_schema():
    shipped = yaml.safe_load(pathlib.Path("wingman/config.yaml").read_text(encoding="utf-8"))
    assert validate_config(merge(shipped, load_replay_config_overrides(PATH1))) == []


def test_a_path_without_overrides_changes_nothing(tmp_path):
    f = tmp_path / "p.yaml"
    f.write_text("P:\n  - [a.png, 0.0]\n", encoding="utf-8")
    assert load_replay_config_overrides(f) == {}


def test_a_malformed_block_is_rejected(tmp_path):
    f = tmp_path / "p.yaml"
    f.write_text("config_overrides: [not, a, mapping]\nP:\n  - [a.png, 0.0]\n", encoding="utf-8")
    with pytest.raises(ValueError):
        load_replay_config_overrides(f)


def test_main_applies_and_validates_them_only_in_replay_mode():
    src = pathlib.Path("wingman/main.py").read_text(encoding="utf-8")
    block = src[src.index("    if args.replay_config:\n        # A replay path pins"):]
    block = block[:block.index("    if _live_run:")]
    assert "load_replay_config_overrides(Path(args.replay_config))" in block
    assert "assert_valid_config(cfg" in block
