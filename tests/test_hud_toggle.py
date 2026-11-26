"""`make hud` toggles hud.write_live_png in config.local.yaml, never the tracked config."""

import subprocess
import sys
from pathlib import Path

import yaml

_ROOT = Path(__file__).resolve().parent.parent
_SCRIPT = _ROOT / "scripts" / "toggle-hud.py"


def _toggle(config_path):
    command = [sys.executable, str(_SCRIPT), "--config", str(config_path)]
    return subprocess.run(command, check=True, capture_output=True, text=True).stdout.strip()


def _local(tmp_path):
    return yaml.safe_load((tmp_path / "config.local.yaml").read_text(encoding="utf-8"))


def test_make_hud_toggles_the_local_overlay_and_never_the_tracked_config(tmp_path):
    config_path = tmp_path / "config.yaml"
    shipped = "hud:\n  enabled: true\n  write_live_png: false # off\nother: unchanged\n"
    config_path.write_text(shipped, encoding="utf-8")

    assert _toggle(config_path).startswith("Live HUD: ON")
    assert _local(tmp_path) == {"hud": {"write_live_png": True}}

    assert _toggle(config_path).startswith("Live HUD: OFF")
    # Back at the shipped value, the override and its empty hud section are gone.
    assert not _local(tmp_path)

    assert config_path.read_text(encoding="utf-8") == shipped


def test_the_toggle_keeps_other_local_settings(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text("hud:\n  write_live_png: false\n", encoding="utf-8")
    (tmp_path / "config.local.yaml").write_text(
        "accept_invite: true\nhud:\n  interval_sec: 2.0\n", encoding="utf-8")
    _toggle(config_path)
    assert _local(tmp_path) == {
        "accept_invite": True, "hud": {"interval_sec": 2.0, "write_live_png": True}}
    _toggle(config_path)
    assert _local(tmp_path) == {"accept_invite": True, "hud": {"interval_sec": 2.0}}


def test_a_shipped_config_without_the_key_is_refused(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text("hud:\n  enabled: true\n", encoding="utf-8")
    result = subprocess.run([sys.executable, str(_SCRIPT), "--config", str(config_path)],
                            capture_output=True, text=True)
    assert result.returncode != 0
    assert "hud.write_live_png" in result.stderr
    assert not (tmp_path / "config.local.yaml").exists()
