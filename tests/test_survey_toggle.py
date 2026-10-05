"""`make survey`: the survey mission (Design 017, phase 4b) on or off for this machine.

The toggle writes only the untracked overlay beside the config (CR-018-16), and
turning survey off has to leave the machine flying the shipped mission again
with every other local setting intact.
"""

import pathlib
import shutil
import subprocess
import sys

import yaml

from wingman.config_local import merge, read_overlay
from wingman.config_schema import validate_config

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_SHIPPED = "mission: {default_mission: su30, padlock_spread_missiles: 0}  # tracked\n"


def _toggle(config_path, check=True):
    command = [sys.executable, str(_ROOT / "scripts" / "toggle-survey.py"), "--config", str(config_path)]
    return subprocess.run(command, check=check, capture_output=True, text=True)


def _config(tmp_path, shipped=_SHIPPED, local=None):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(shipped, encoding="utf-8")
    if local is not None:
        (tmp_path / "config.local.yaml").write_text(local, encoding="utf-8")
    return config_path


def test_on_then_off_writes_the_overlay_and_never_the_tracked_config(tmp_path):
    config_path = _config(tmp_path)

    on = _toggle(config_path)
    assert on.stdout.strip() == "Survey mode: ON (the next launch flies survey passes)"
    assert read_overlay(config_path) == {"mission": {"default_mission": "survey"}}

    off = _toggle(config_path)
    assert off.stdout.strip() == "Survey mode: OFF (the next launch flies the shipped mission, su30)"
    # Back on the shipped mission, the override is gone rather than set to su30.
    assert read_overlay(config_path) == {}

    assert config_path.read_text(encoding="utf-8") == _SHIPPED


def test_off_keeps_every_other_local_setting(tmp_path):
    """The state this machine was in when `make survey` was written."""
    config_path = _config(tmp_path, local=(
        "accept_invite: true\n"
        "mission:\n  default_mission: survey\n  padlock_spread_missiles: 2\n"
        "nested:\n  isolate_pointer: false\n"))

    assert "OFF" in _toggle(config_path).stdout
    assert read_overlay(config_path) == {
        "accept_invite": True, "mission": {"padlock_spread_missiles": 2},
        "nested": {"isolate_pointer": False}}

    assert "ON" in _toggle(config_path).stdout
    assert read_overlay(config_path) == {
        "accept_invite": True, "mission": {"padlock_spread_missiles": 2, "default_mission": "survey"},
        "nested": {"isolate_pointer": False}}


def test_on_says_so_when_it_replaces_a_local_mission(tmp_path):
    config_path = _config(tmp_path, local="mission: {default_mission: j20}\n")

    on = _toggle(config_path)
    assert on.stdout.splitlines()[0] == "Survey mode: ON (the next launch flies survey passes)"
    assert "replaced the local mission 'j20'" in on.stdout
    assert read_overlay(config_path) == {"mission": {"default_mission": "survey"}}

    # Off is the shipped mission, not the one that was replaced.
    assert "shipped mission, su30" in _toggle(config_path).stdout
    assert read_overlay(config_path) == {}


def test_a_config_that_ships_survey_is_refused_and_nothing_is_written(tmp_path):
    config_path = _config(tmp_path, shipped="mission: {default_mission: survey}\n")
    result = _toggle(config_path, check=False)
    assert result.returncode != 0
    assert "no other mission to go back to" in result.stderr
    assert not (tmp_path / "config.local.yaml").exists()


def test_a_config_without_a_default_mission_is_refused(tmp_path):
    result = _toggle(_config(tmp_path, shipped="accept_invite: false\n"), check=False)
    assert result.returncode != 0
    assert "mission.default_mission" in result.stderr


def test_the_shipped_config_with_survey_on_passes_the_schema(tmp_path):
    """A live run validates the merged config; the toggle must not write
    something that stops the next launch."""
    config_path = tmp_path / "config.yaml"
    shutil.copy(_ROOT / "wingman" / "config.yaml", config_path)
    shipped = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert shipped["mission"]["default_mission"] != "survey"

    _toggle(config_path)
    merged = merge(shipped, read_overlay(config_path))
    assert merged["mission"]["default_mission"] == "survey"
    assert validate_config(merged) == []

    _toggle(config_path)
    assert merge(shipped, read_overlay(config_path)) == shipped
