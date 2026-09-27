"""CR-018-16: the operator's untracked config.local.yaml overlays the shipped config.

A live run merges it over wingman/config.yaml and validates the result with the
same schema. Tests and replay runs see only the shipped file.
"""

import pathlib

import pytest
import yaml

from wingman.config_local import local_path, merge, overlay_keys, read_overlay
from wingman.config_schema import ConfigError
from wingman.main import load_config

_SHIPPED = pathlib.Path("wingman/config.yaml")


def _config_pair(tmp_path, overlay_text=None):
    config = tmp_path / "config.yaml"
    config.write_text(_SHIPPED.read_text(encoding="utf-8"), encoding="utf-8")
    if overlay_text is not None:
        (tmp_path / "config.local.yaml").write_text(overlay_text, encoding="utf-8")
    return config


def test_mappings_merge_and_everything_else_is_replaced():
    base = {"a": 1, "nested": {"x": 1, "y": 2}, "items": [1, 2]}
    out = merge(base, {"nested": {"y": 3}, "items": [9], "b": True})
    assert out == {"a": 1, "nested": {"x": 1, "y": 3}, "items": [9], "b": True}
    assert base["nested"] == {"x": 1, "y": 2}, "the shipped dict must not be mutated"


def test_a_live_run_takes_the_overlay(tmp_path):
    config = _config_pair(tmp_path, "accept_invite: true\n")
    assert load_config(config, local_overlay=True)["accept_invite"] is True


def test_without_the_flag_the_overlay_is_ignored(tmp_path):
    """What tests, tooling and replay runs get."""
    config = _config_pair(tmp_path, "accept_invite: true\n")
    assert load_config(config)["accept_invite"] is False


def test_a_misspelt_local_key_fails_startup_like_a_shipped_one(tmp_path):
    config = _config_pair(tmp_path, "accept_invites: true\n")
    with pytest.raises(ConfigError) as exc:
        load_config(config, local_overlay=True)
    assert "config.local.yaml" in str(exc.value), "the error must name the overlay"


def test_an_empty_or_missing_overlay_changes_nothing(tmp_path):
    config = _config_pair(tmp_path)
    assert read_overlay(config) == {}
    (tmp_path / "config.local.yaml").write_text("# only a comment\n", encoding="utf-8")
    assert read_overlay(config) == {}
    shipped = yaml.safe_load(_SHIPPED.read_text(encoding="utf-8"))
    assert load_config(config, local_overlay=True) == shipped


def test_a_non_mapping_overlay_is_rejected(tmp_path):
    config = _config_pair(tmp_path, "- not\n- a mapping\n")
    with pytest.raises(ValueError):
        read_overlay(config)


def test_overlay_keys_name_every_leaf_for_the_startup_log():
    assert overlay_keys({"accept_invite": True, "mission": {"default_mission": "su30"}}) == [
        "accept_invite", "mission.default_mission"]


def test_the_overlay_is_untracked():
    ignore = pathlib.Path(".gitignore").read_text(encoding="utf-8").splitlines()
    assert str(local_path(_SHIPPED)) in ignore


def test_only_a_live_run_asks_for_the_overlay():
    src = pathlib.Path("wingman/main.py").read_text(encoding="utf-8")
    assert "_live_run = not (args.replay_config or args.capture_path_config)" in src
    assert "load_config(args.config, local_overlay=_live_run)" in src
