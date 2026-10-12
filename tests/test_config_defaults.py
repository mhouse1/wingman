"""CR-018-16: a config key read in two places has one default.

`telemetry.steep_dive_min_sin` defaulted to 0.8 in the eject controller and to
0.5 in TelemetryProcessor. The shipped config sets 0.8, which hid the split; any
run without the key had the two components disagree about what a steep dive is.
"""

import pathlib
import threading
import types

import yaml

import wingman.controller as controller_module
from wingman.analyzer import GameState
from wingman.controller import Controller
from wingman.controller_config import ControllerConfig
from wingman.telemetry import STEEP_DIVE_MIN_SIN_DEFAULT, TelemetryProcessor


def test_the_processor_and_the_eject_controller_agree_without_the_key(monkeypatch):
    monkeypatch.setattr(controller_module, "keyboard_module", None)
    ctrl = Controller((0, 0, 1920, 1200),
                      analyzer=types.SimpleNamespace(game_state=GameState.GAME_BATTLE),
                      exit_event=threading.Event(),
                      config=ControllerConfig(simulate_os_input=True, telemetry={}))
    assert TelemetryProcessor({}).steep_min_sin == ctrl._eject_steep_min_sin


def test_the_shared_default_is_the_shipped_value():
    """So a run without the key behaves like the shipped config, not like an
    untested third setting."""
    shipped = yaml.safe_load(pathlib.Path("wingman/config.yaml").read_text(encoding="utf-8"))
    assert shipped["telemetry"]["steep_dive_min_sin"] == STEEP_DIVE_MIN_SIN_DEFAULT


def test_the_schema_is_where_the_default_lives():
    """CR-018-16, the later step: a key's default declared once, in the schema."""
    from wingman.config_schema import schema_default
    assert schema_default("telemetry.steep_dive_min_sin") == STEEP_DIVE_MIN_SIN_DEFAULT == 0.8


def test_a_key_without_a_declared_default_says_so():
    import pytest
    from wingman.config_schema import schema_default
    with pytest.raises(KeyError):
        schema_default("telemetry.level_max_sin")
    with pytest.raises(KeyError):
        schema_default("telemetry.no_such_key")


def test_resupply_priority_defaults_to_shadow_and_ships_actuation_trial():
    """A config without the key stays in shadow; shipped config enables the
    ADR 152 live actuation trial."""
    from wingman.config_schema import schema_default

    shipped = yaml.safe_load(pathlib.Path("wingman/config.yaml").read_text(encoding="utf-8"))
    assert schema_default("pursuit_mode.resupply_priority.enabled") is True
    assert schema_default("pursuit_mode.resupply_priority.actuate") is False
    assert schema_default("pursuit_mode.resupply_priority.rearm_climb_s") == 3.0
    # The candidate-frame capture is off unless a config asks for it
    # (operator, 2026-10-05).
    assert schema_default("pursuit_mode.resupply_priority.save_candidate_frames") is False
    assert shipped["pursuit_mode"]["resupply_priority"] == {
        "enabled": True,
        "actuate": True,
        "rearm_climb_s": 3.0,
        "save_candidate_frames": False,
    }


def test_every_declared_default_passes_its_own_leaf():
    """A default the validator would reject is a trap for the first run without
    the key."""
    from wingman.config_schema import SCHEMA, NO_DEFAULT, Leaf, MapOf, Section, validate_config

    def _walk(node, path):
        if isinstance(node, Section):
            for k, v in node.children.items():
                yield from _walk(v, path + (k,))
        elif isinstance(node, Leaf) and node.default is not NO_DEFAULT:
            yield path, node
        elif isinstance(node, MapOf):
            yield from _walk(node.value, path + ("*",))

    found = list(_walk(SCHEMA, ()))
    assert found, "the walk must find at least the steep_dive_min_sin default"
    for path, leaf in found:
        probe = Section(children={"k": leaf})
        assert validate_config({"k": leaf.default}, schema=probe) == [], ".".join(path)
