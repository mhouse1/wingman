"""Toggle Wingman's party-invite accept/decline policy in config.yaml."""

import argparse
from pathlib import Path

import yaml


def toggle_invite_policy(config_path: Path) -> bool:
    text = config_path.read_text(encoding="utf-8")
    config = yaml.safe_load(text)
    if not isinstance(config, dict) or type(config.get("accept_invite")) is not bool:
        raise ValueError("config must define accept_invite as true or false")

    root = yaml.compose(text)
    value_node = next(
        (
            value
            for key, value in root.value
            if key.value == "accept_invite"
        ),
        None,
    )
    if value_node is None:
        raise ValueError("config must define accept_invite as true or false")

    new_value = not config["accept_invite"]
    updated = text[: value_node.start_mark.index] + str(new_value).lower() + text[value_node.end_mark.index :]
    config_path.write_text(updated, encoding="utf-8")
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
