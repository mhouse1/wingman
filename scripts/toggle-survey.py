"""Toggle the survey mission (Design 017, phase 4b) on or off for this machine.

On: `mission.default_mission: survey` goes into the untracked `config.local.yaml`
beside the config, and the next launch flies survey passes. Off: that line comes
out again, and the next launch flies the mission the tracked config ships. The
tracked config is never written (CR-018-16).

A session already running keeps the mission it started with: the overlay is
read at startup.
"""

import argparse
import os
import sys
from pathlib import Path

import yaml

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from wingman.config_local import local_path, read_overlay, write_overlay  # noqa: E402

SURVEY = "survey"


def toggle_survey(config_path: Path) -> "tuple[bool, str, str | None]":
    """Returns (survey is now on, the mission the next launch flies, the local
    mission that turning survey on replaced, if there was one)."""
    shipped = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    shipped_mission = shipped.get("mission") if isinstance(shipped, dict) else None
    shipped_default = shipped_mission.get("default_mission") if isinstance(shipped_mission, dict) else None
    if not isinstance(shipped_default, str):
        raise ValueError("config must define mission.default_mission")
    if shipped_default == SURVEY:
        raise ValueError(f"{config_path} itself ships the survey mission, so there is no other "
                         "mission to go back to; change mission.default_mission there")

    overlay = read_overlay(config_path)
    local = overlay.get("mission", {})
    if not isinstance(local, dict):
        raise ValueError(f"{local_path(config_path)}: mission must be a mapping")
    local = dict(local)
    current = local.get("default_mission", shipped_default)

    replaced = None
    if current == SURVEY:
        local.pop("default_mission")
    else:
        if "default_mission" in local:
            replaced = str(current)
        local["default_mission"] = SURVEY
    if local:
        overlay["mission"] = local
    else:
        overlay.pop("mission", None)

    write_overlay(config_path, overlay)
    survey_on = current != SURVEY
    return survey_on, (SURVEY if survey_on else shipped_default), replaced


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("wingman/config.yaml"))
    args = parser.parse_args()

    try:
        survey_on, mission, replaced = toggle_survey(args.config)
    except (OSError, ValueError, yaml.YAMLError) as exc:
        parser.error(str(exc))

    if survey_on:
        print("Survey mode: ON (the next launch flies survey passes)")
        if replaced:
            print(f"This replaced the local mission '{replaced}'; `make survey` again goes "
                  "back to the shipped mission, not to that one.")
    else:
        print(f"Survey mode: OFF (the next launch flies the shipped mission, {mission})")


if __name__ == "__main__":
    main()
