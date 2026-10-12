"""The operator's local config overlay. CR-018-16.

`wingman/config.yaml` is tracked, and tests pin its shipped values. A setting the
operator flips for a session used to be written into it: `scripts/toggle-invite.py`
rewrote `accept_invite`, and the test pinning the shipped default turned the
suite red (CR-018-11). Such settings now live in an untracked `config.local.yaml`
beside it. A live run merges the overlay over the shipped file at startup and
validates the result with the same schema, so a misspelt local key still fails
fast. Tests read only the shipped file, and replay and capture runs ignore the
overlay so their results do not depend on one machine's settings.
"""

from pathlib import Path

import yaml

LOCAL_NAME = "config.local.yaml"

_HEADER = ("# Per-machine settings, merged over config.yaml by a live run (CR-018-16).\n"
           "# Untracked. Written by `make invite` and `make survey`; edit by hand for\n"
           "# anything else.\n")


def local_path(config_path) -> Path:
    """The overlay that belongs to `config_path`: same folder, fixed name."""
    return Path(config_path).with_name(LOCAL_NAME)


def read_overlay(config_path) -> dict:
    """The overlay's contents, or {} when there is none or it is empty."""
    path = local_path(config_path)
    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ValueError(f"{path} must be a YAML mapping, not {type(data).__name__}")
    return data


def write_overlay(config_path, overlay: dict) -> None:
    """Replace the overlay with `overlay`. Comments in the old file are not kept."""
    body = yaml.safe_dump(overlay, sort_keys=False) if overlay else ""
    local_path(config_path).write_text(_HEADER + body, encoding="utf-8")


def merge(base: dict, overlay: dict) -> dict:
    """`base` with `overlay` on top. Mappings merge key by key; anything else,
    lists included, is replaced whole."""
    out = dict(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = merge(out[key], value)
        else:
            out[key] = value
    return out


def overlay_keys(overlay: dict, prefix: str = "") -> "list[str]":
    """Dotted names of every value the overlay sets, for the startup log."""
    keys = []
    for key, value in overlay.items():
        name = f"{prefix}{key}"
        if isinstance(value, dict) and value:
            keys.extend(overlay_keys(value, prefix=f"{name}."))
        else:
            keys.append(name)
    return keys
