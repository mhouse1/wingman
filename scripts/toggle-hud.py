"""Toggle the live HUD picture (hud.write_live_png) for this machine.

Like `make invite` (CR-018-16), the toggle writes the untracked
`config.local.yaml` beside the config, which a live run merges over it, and
never the tracked config, whose shipped default a test pins. Toggling back to
the shipped value removes the override. A running session reads the config at
startup, so the change applies from the next launch.
"""

import argparse
import os
import sys
from pathlib import Path

import yaml

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from wingman.config_local import local_path, read_overlay, write_overlay  # noqa: E402

KEY = "write_live_png"


def toggle_live_hud(config_path: Path) -> bool:
    shipped = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    hud = shipped.get("hud") if isinstance(shipped, dict) else None
    if not isinstance(hud, dict) or type(hud.get(KEY)) is not bool:
        raise ValueError(f"config must define hud.{KEY} as true or false")
    overlay = read_overlay(config_path)
    local_hud = overlay.get("hud", {})
    if not isinstance(local_hud, dict):
        raise ValueError(f"{local_path(config_path)}: hud must be a mapping")
    current = local_hud.get(KEY, hud[KEY])
    if type(current) is not bool:
        raise ValueError(f"{local_path(config_path)}: hud.{KEY} must be true or false")

    new_value = not current
    if new_value == hud[KEY]:
        local_hud.pop(KEY, None)
    else:
        local_hud[KEY] = new_value
    if local_hud:
        overlay["hud"] = local_hud
    else:
        overlay.pop("hud", None)

    write_overlay(config_path, overlay)
    return new_value


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("wingman/config.yaml"))
    args = parser.parse_args()

    try:
        on = toggle_live_hud(args.config)
    except (OSError, ValueError, yaml.YAMLError) as exc:
        parser.error(str(exc))

    print(f"Live HUD: {'ON' if on else 'OFF'} (from the next wingman launch)")


if __name__ == "__main__":
    main()
