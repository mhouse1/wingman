"""Toggle Wingman's party-invite accept/decline policy for this machine.

CR-018-16: the toggle writes the untracked `config.local.yaml` beside the
config, which a live run merges over it, and never the tracked config itself.
Rewriting the tracked file made the suite red, because a test pins its shipped
default (CR-018-11). Toggling back to the shipped value removes the override.
"""

import argparse
import os
import sys
from pathlib import Path

import yaml

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from wingman.config_local import local_path, read_overlay, write_overlay  # noqa: E402


def toggle_invite_policy(config_path: Path) -> bool:
    shipped = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    if not isinstance(shipped, dict) or type(shipped.get("accept_invite")) is not bool:
        raise ValueError("config must define accept_invite as true or false")
    overlay = read_overlay(config_path)
    current = overlay.get("accept_invite", shipped["accept_invite"])
    if type(current) is not bool:
        raise ValueError(f"{local_path(config_path)}: accept_invite must be true or false")

    new_value = not current
    if new_value == shipped["accept_invite"]:
        overlay.pop("accept_invite", None)
    else:
        overlay["accept_invite"] = new_value

    write_overlay(config_path, overlay)
    return new_value


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("wingman/config.yaml"))
    args = parser.parse_args()

    try:
        accepting = toggle_invite_policy(args.config)
    except (OSError, ValueError, yaml.YAMLError) as exc:
        parser.error(str(exc))

    print(f"Party invites: {'ACCEPT' if accepting else 'REJECT'}")


if __name__ == "__main__":
    main()
