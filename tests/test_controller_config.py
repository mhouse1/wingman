"""ControllerConfig parameter object (Future 002 A-02).

Covers the two things the dataclass exists to guarantee: that the
`config.yaml` -> controller-setting mapping lives in exactly one place, and
that a mistyped run-mode override fails loudly instead of being dropped.
"""

import dataclasses
import pathlib

import pytest
import yaml

from wingman.controller import Controller
from wingman.controller_config import ControllerConfig

_ROOT = pathlib.Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def shipped_cfg():
    return yaml.safe_load((_ROOT / "wingman" / "config.yaml").read_text(encoding="utf-8"))


def test_from_config_reads_each_block_from_its_real_home(shipped_cfg):
    cc = ControllerConfig.from_config(shipped_cfg)

    mission = shipped_cfg["mission"]
    assert cc.weapon_loop_interval == mission["weapon_loop_interval"]
    assert cc.starting_max_wait_s == mission["starting_max_wait_s"]
    assert cc.good_luck_wait_s == mission["good_luck_wait_s"]
    assert cc.capture_stale_inject_s == mission["capture_stale_inject_s"]
    assert cc.target_painting_mode == shipped_cfg["j20_mission"]["target_painting_mode"]
    assert cc.capture_with_overlay == shipped_cfg["debug"]["capture_with_overlay"]
    assert cc.missile_evade == shipped_cfg["behavior_tree"]["missile_evade"]
    assert cc.climb == shipped_cfg["behavior_tree"]["climb"]
    assert cc.telemetry == shipped_cfg["telemetry"]
    assert cc.fuel == shipped_cfg["fuel"]


def test_overrides_apply_without_touching_the_rest(shipped_cfg):
    base = ControllerConfig.from_config(shipped_cfg)
    overridden = ControllerConfig.from_config(
        shipped_cfg, simulate_os_input=True, disable_hotkeys=True)

    assert overridden.simulate_os_input is True
    assert overridden.disable_hotkeys is True
    assert overridden.weapon_loop_interval == base.weapon_loop_interval
    assert overridden.missile_evade == base.missile_evade


def test_misspelled_override_raises(shipped_cfg):
    """The same fail-fast contract the config schema enforces on the YAML."""
    with pytest.raises(TypeError) as exc:
        ControllerConfig.from_config(shipped_cfg, disable_hotkyes=True)
    assert "disable_hotkyes" in str(exc.value)


def test_empty_config_falls_back_to_declared_defaults():
    cc = ControllerConfig.from_config(None)
    assert cc == ControllerConfig.from_config({})
    assert cc.weapon_loop_interval == 0.5
    assert cc.missile_evade == {}


def test_config_is_frozen():
    """A tactic thread must not be able to retune the controller mid-flight."""
    cc = ControllerConfig()
    with pytest.raises(dataclasses.FrozenInstanceError):
        cc.disable_hotkeys = True


def test_controller_accepts_the_object_and_keeps_it(shipped_cfg):
    cc = ControllerConfig.from_config(shipped_cfg, disable_hotkeys=True)
    ctrl = Controller((0, 0, 1920, 1200), config=cc)

    assert ctrl.config is cc
    assert ctrl._disable_hotkeys is True
    assert ctrl._starting_max_wait_s == cc.starting_max_wait_s
    assert ctrl._weapon_loop_interval == cc.weapon_loop_interval


def test_controller_defaults_when_no_config_given():
    ctrl = Controller((0, 0, 1920, 1200), config=ControllerConfig(disable_hotkeys=True))
    plain = Controller((0, 0, 1920, 1200), config=ControllerConfig(disable_hotkeys=True))
    assert ctrl.config == plain.config


def test_tuning_values_are_no_longer_constructor_arguments():
    """The point of A-02: tuning must arrive via `config`, not as a widening
    parameter list. A stray keyword is a TypeError, not a silent no-op."""
    with pytest.raises(TypeError):
        Controller((0, 0, 1920, 1200), disable_hotkeys=True)
