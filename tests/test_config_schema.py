"""Schema validation for config.yaml (Future 002 A-03).

The regression this suite protects: a misspelled config key used to fall through
`cfg.get(key, default)` silently, so the program ran on a code default while the
YAML said otherwise. Code review 015 found all five `behavior_tree.missile_evade`
values in that state in a shipped config.
"""

import ast
import copy
import pathlib

import pytest
import yaml

from wingman.config_schema import (
    SCHEMA,
    ConfigError,
    MapOf,
    Section,
    assert_valid_config,
    validate_config,
)

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_CONFIG_PATH = _ROOT / "wingman" / "config.yaml"


@pytest.fixture(scope="module")
def shipped_cfg():
    return yaml.safe_load(_CONFIG_PATH.read_text(encoding="utf-8"))


def test_shipped_config_is_valid(shipped_cfg):
    """The config the app actually ships with must pass its own gate."""
    assert validate_config(shipped_cfg) == []


def test_americanised_manoeuvre_key_is_rejected(shipped_cfg):
    """The exact CR-015 trap: the American spelling must not pass silently."""
    cfg = copy.deepcopy(shipped_cfg)
    me = cfg["behavior_tree"]["missile_evade"]
    me["max_maneuver_s"] = me.pop("max_manoeuvre_s")

    errors = validate_config(cfg)

    assert len(errors) == 1
    assert "max_maneuver_s" in errors[0]
    assert "unknown key" in errors[0]
    assert "max_manoeuvre_s" in errors[0], "the suggestion must name the correct spelling"


@pytest.mark.parametrize("path, value, expected", [
    (("telemetry", "smoothing_window"), True, "got bool"),
    (("minimap", "ema_alpha"), "0.4", "expected int or float"),
    (("minimap", "ema_alpha"), 1.4, "above the maximum"),
    (("behavior_tree", "mode"), "activ", "is not one of"),
    (("respawn_detection", "ocr_cooldown"), -1.0, "below the minimum"),
    (("enemy_hsv", "lower"), [0, 120], "expected 3 items"),
    (("hud", "output_path"), 7, "expected str"),
])
def test_type_and_range_violations_are_caught(shipped_cfg, path, value, expected):
    cfg = copy.deepcopy(shipped_cfg)
    section, key = cfg, path[-1]
    for part in path[:-1]:
        section = section[part]
    section[key] = value

    errors = validate_config(cfg)

    assert any(expected in e for e in errors), errors


def test_bool_does_not_satisfy_an_int_leaf(shipped_cfg):
    """`bool` subclasses `int`, so a naive isinstance check would accept it."""
    cfg = copy.deepcopy(shipped_cfg)
    cfg["performance"]["regression"]["min_sessions"] = False
    assert any("got bool" in e for e in validate_config(cfg))


def test_missing_required_key_is_caught(shipped_cfg):
    cfg = copy.deepcopy(shipped_cfg)
    del cfg["region"]
    assert any(e.startswith("region: required key is missing") for e in validate_config(cfg))


def test_null_is_rejected_unless_declared_nullable(shipped_cfg):
    cfg = copy.deepcopy(shipped_cfg)
    cfg["game_window_offset"]["x"] = None          # declared allow_none
    cfg["loop_interval_sec"] = None                # not nullable
    errors = validate_config(cfg)
    assert errors == ["loop_interval_sec: must not be null"]


def test_crop_names_are_free_but_crop_shape_is_not(shipped_cfg):
    """`crops:` keys are calibration targets, so unknown-key rejection must not
    apply to the names — only to each value's shape."""
    cfg = copy.deepcopy(shipped_cfg)
    cfg["crops"]["A_BRAND_NEW_CROP"] = {"coords": [[0.1, 0.2], [0.3, 0.4]]}
    assert validate_config(cfg) == []

    cfg["crops"]["BROKEN"] = {"text": ["X"]}
    assert any("crops.BROKEN.coords: required key is missing" in e
               for e in validate_config(cfg))


def test_all_problems_are_reported_at_once(shipped_cfg):
    """One startup should surface every problem, not one per run."""
    cfg = copy.deepcopy(shipped_cfg)
    cfg["nonsense_one"] = 1
    cfg["nonsense_two"] = 2
    cfg["minimap"]["ema_alpha"] = 5.0

    with pytest.raises(ConfigError) as exc:
        assert_valid_config(cfg, source="test.yaml")

    message = str(exc.value)
    assert "3 problems" in message
    assert "nonsense_one" in message and "nonsense_two" in message
    assert "ema_alpha" in message


def test_valid_config_does_not_raise(shipped_cfg):
    assert_valid_config(shipped_cfg)   # must not raise


# ---------------------------------------------------------------------------
# Drift guard: the schema is only useful while it matches what the code reads.
# ---------------------------------------------------------------------------

_CFG_RECEIVERS = (
    "cfg", "config", "_cfg", "perf_cfg", "reg_cfg", "hist_cfg", "mission_cfg",
    "startup_cfg", "bt_cfg", "j20_cfg", "minimap_cfg", "tracking_cfg",
    "telemetry_cfg", "climb_cfg", "fuel_cfg", "missile_evade_cfg", "me_cfg",
    "sg_cfg", "sustain_cfg", "ecl_cfg", "health_cfg",
)


def _declared_keys(spec, out=None):
    out = set() if out is None else out
    if isinstance(spec, Section):
        for name, child in spec.children.items():
            out.add(name)
            _declared_keys(child, out)
    elif isinstance(spec, MapOf):
        _declared_keys(spec.value, out)
    return out


def _config_reads():
    """Every `<something>_cfg.get("key")` literal in wingman/."""
    found = {}
    for path in sorted((_ROOT / "wingman").glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "get"
                    and node.args
                    and isinstance(node.args[0], ast.Constant)
                    and isinstance(node.args[0].value, str)):
                continue
            recv = node.func.value
            name = getattr(recv, "id", None) or getattr(recv, "attr", None) or ""
            if any(name == r or name.endswith(r) for r in _CFG_RECEIVERS):
                found.setdefault(node.args[0].value, []).append(f"{path.name}:{node.lineno}")
    return found


def test_no_config_key_is_read_without_being_declared():
    """A key the code reads but the schema omits would be rejected as unknown
    the moment someone actually set it — a false failure that only appears in
    production. Adding a config key means adding it to the schema."""
    declared = _declared_keys(SCHEMA)
    undeclared = {k: v for k, v in _config_reads().items() if k not in declared}
    assert not undeclared, (
        "config keys read by wingman/ but missing from config_schema.SCHEMA: "
        + repr(undeclared)
    )
