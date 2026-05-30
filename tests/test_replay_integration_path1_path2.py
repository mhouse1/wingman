"""ADR037 — PATH1 and PATH2 OCR regression tests.

These tests run the full replay pipeline with the real GameStateAnalyzer (no
monkeypatching of OCR).  Screenshots in test_screenshots/integration_test/ must
be real game captures; all-black placeholder images cause the test to be skipped
automatically.

Run with:
    uv run --active pytest tests/test_replay_integration_path1_path2.py -v -s
or via the Makefile:
    make ocr

Marked @pytest.mark.slow so they are excluded from the default `make test` run.
"""

import json
import sys
from pathlib import Path

import cv2
import numpy as np
import pytest
import yaml

import wingman.main as wingman_main

# ---------------------------------------------------------------------------
# Skip guard: easyocr must be importable
# ---------------------------------------------------------------------------
try:
    import easyocr as _easyocr_check  # noqa: F401
    _EASYOCR_AVAILABLE = True
except ImportError:
    _EASYOCR_AVAILABLE = False

pytestmark = pytest.mark.slow

_REPO = Path(__file__).resolve().parent.parent
_REAL_CONFIG_PATH = _REPO / "wingman" / "config.yaml"
_SCREENSHOT_DIR = _REPO / "test_screenshots" / "integration_test"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _is_dummy_screenshot(path: Path) -> bool:
    """Return True when a screenshot is an all-black placeholder (all zeros)."""
    img = cv2.imread(str(path))
    if img is None:
        return True
    return not np.any(img)


def _check_screenshots_ready(names: list[str]) -> None:
    """Skip the test if any required screenshot is missing or still a placeholder."""
    if not _EASYOCR_AVAILABLE:
        pytest.skip("easyocr not installed — OCR integration tests require easyocr")
    for name in names:
        p = _SCREENSHOT_DIR / name
        if not p.exists():
            pytest.skip(f"Screenshot not found: {p}")
        if _is_dummy_screenshot(p):
            pytest.skip(
                f"Screenshot is an all-black placeholder: {name} — "
                "replace with a real game capture before running OCR tests"
            )


def _build_test_config(tmp_path: Path) -> Path:
    """Load the real config and apply test-friendly overrides."""
    cfg = yaml.safe_load(_REAL_CONFIG_PATH.read_text(encoding="utf-8"))

    cfg["loop_interval_sec"] = 0.5

    mission = cfg.setdefault("mission", {})
    mission["waiting_fallback_enabled"] = False
    # Long fallback timeout ensures the path-based alive_event fires first,
    # before the fallback restart loop fires (which has no replay_assertions hook).
    mission["respawn_fallback_timeout"] = 60.0
    mission["restart_delay_after_unlock"] = 1.0
    mission["restart_retry_interval"] = 1.0

    debug = cfg.setdefault("debug", {})
    debug["show_window"] = False
    debug["show_grid_highlighted"] = False
    debug["draw_markers"] = False
    debug["debug_output_dir"] = str(tmp_path / "debug")

    out = tmp_path / "test_config.yaml"
    out.write_text(
        yaml.dump(cfg, default_flow_style=None, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    return out


def _build_path1_ocr_yaml(tmp_path: Path) -> Path:
    """Write PATH1_OCR with generous settle windows for real OCR timing."""
    payload = {
        "PATH1_OCR": [
            {
                "screenshot_name": "P1_000_LOBBY_PLAY.png",
                "injection_time_s": 0.0,
                "expected_state": "GAME_LOBBY",
            },
            {
                "screenshot_name": "P1_010_WAITING_CANCEL_VISIBLE.png",
                "injection_time_s": 10.0,
                "expected_state": "GAME_WAITING",
                "expected_trigger": "cancel_detected",
                "max_settle_time_s": 8.0,
            },
            {
                "screenshot_name": "P1_020_GOOD_LUCK_VISIBLE.png",
                "injection_time_s": 20.0,
                "expected_state": "GAME_STARTING",
                "expected_trigger": "good_luck_detected",
                "max_settle_time_s": 8.0,
            },
            {
                "screenshot_name": "P1_030_BATTLE_HUD_MISSILES_4.png",
                "injection_time_s": 30.0,
                "expected_state": "GAME_BATTLE",
                "expected_trigger": "battle_started",
                "max_settle_time_s": 5.0,
            },
            {
                "screenshot_name": "P1_040_BATTLE_HUD_MISSILES_0_HEALTH_ALIVE.png",
                "injection_time_s": 40.0,
                "expected_state": "GAME_BATTLE",
                "expected_trigger": "missiles_empty",
                "max_settle_time_s": 8.0,
            },
            {
                "screenshot_name": "P1_050_RESPAWN_VISIBLE_NO_HEALTH.png",
                "injection_time_s": 60.0,
                "expected_state": "GAME_BATTLE",
                "expected_trigger": "respawn_detected",
                "max_settle_time_s": 12.0,
            },
            {
                "screenshot_name": "P1_060_BATTLE_HUD_HEALTH_ALIVE_MISSILES_4.png",
                "injection_time_s": 75.0,
                "expected_state": "GAME_BATTLE",
                "expected_trigger": "restart_last_mission",
                "max_settle_time_s": 12.0,
            },
            {
                "screenshot_name": "P1_070_CLICK_TO_CONTINUE.png",
                "injection_time_s": 88.0,
                "expected_state": "GAME_END_B",
                "expected_trigger": "click_to_detected",
                "max_settle_time_s": 5.0,
                "inject_trigger": "click_to_detected",
            },
            {
                "screenshot_name": "P1_080_LOBBY_AFTER_MISSION.png",
                "injection_time_s": 89.0,
                "expected_state": "GAME_LOBBY",
                "expected_trigger": "continue_clicked",
                "max_settle_time_s": 5.0,
                "inject_trigger": "continue_clicked",
            },
        ]
    }
    out = tmp_path / "path1_ocr.yaml"
    out.write_text(
        yaml.dump(payload, default_flow_style=None, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    return out


def _build_path2_ocr_yaml(tmp_path: Path) -> Path:
    """Write PATH2_OCR with generous settle windows for real OCR timing."""
    content = """\
# PATH2_OCR: real-OCR timing clone of PATH2.
# inject_trigger: manual_force_battle on step 0 seeds the FSM into GAME_BATTLE.
# inject_trigger: manual_takeover on P2_020 fires the manual-mode transition.
PATH2_OCR:
  # Seed GAME_BATTLE immediately via inject_trigger; no assertion on this step.
  - screenshot_name: P2_000_BATTLE_HUD_MISSILES_4.png
    injection_time_s: 0.0
    inject_trigger: manual_force_battle
  # BATTLE (missiles=0) at t=8 s.  Ammo OCR detects 0 within 10 s.
  - screenshot_name: P2_010_BATTLE_HUD_MISSILES_0.png
    injection_time_s: 8.0
    expected_state: GAME_BATTLE
    expected_trigger: missiles_empty
    max_settle_time_s: 10.0
  # Manual takeover: inject_trigger fires manual_takeover immediately.
  - screenshot_name: P2_020_MANUAL_TAKEOVER_MOMENT.png
    injection_time_s: 20.0
    expected_state: GAME_BATTLE
    expected_trigger: manual_mode
    max_settle_time_s: 5.0
    inject_trigger: manual_takeover
  # GAME_BATTLE_MANUAL confirmed immediately after inject.
  - screenshot_name: P2_030_GAME_BATTLE_MANUAL_HUD.png
    injection_time_s: 23.0
    expected_state: GAME_BATTLE_MANUAL
    expected_trigger: manual_mode_entered
    max_settle_time_s: 5.0
  # RESPAWN at t=35 s.  Respawn OCR runs in background; allow 12 s.
  - screenshot_name: P2_040_RESPAWN_VISIBLE_NO_HEALTH.png
    injection_time_s: 35.0
    expected_state: GAME_BATTLE_MANUAL
    expected_trigger: respawn_detected
    max_settle_time_s: 12.0
  # ALIVE at t=52 s.  Health OCR detects alive; restart_last_mission fires.
  - screenshot_name: P2_050_RESPAWN_CLEAR_HEALTH_ALIVE_MISSILES_4.png
    injection_time_s: 52.0
    expected_state: GAME_BATTLE
    expected_trigger: restart_last_mission
    max_settle_time_s: 12.0
  # End-of-mission screen at t=65 s.  inject_trigger forces FSM into GAME_END_B.
  # (P2_050 deadline is t=64 s; 1 s buffer before this step activates.)
  - screenshot_name: P2_060_CLICK_TO_CONTINUE.png
    injection_time_s: 65.0
    expected_state: GAME_END_B
    expected_trigger: click_to_detected
    max_settle_time_s: 5.0
    inject_trigger: click_to_detected
  # Back to LOBBY at t=66 s.  inject_trigger fires continue_clicked.
  - screenshot_name: P2_070_LOBBY_AFTER_MISSION.png
    injection_time_s: 66.0
    expected_state: GAME_LOBBY
    expected_trigger: continue_clicked
    max_settle_time_s: 5.0
    inject_trigger: continue_clicked
"""
    out = tmp_path / "path2_ocr.yaml"
    out.write_text(content, encoding="utf-8")
    return out


# ---------------------------------------------------------------------------
# Fake infrastructure (no real OS input; real GameStateAnalyzer is kept)
# ---------------------------------------------------------------------------

class FakePerformanceTracker:
    def __init__(self, *_args, **_kwargs):
        pass

    def on_enter_game_lobby(self):
        pass

    def on_session_end(self):
        pass


class FakeController:
    """Minimal controller stub: records intents, simulates mission-running state."""

    def __init__(self, *_args, **_kwargs):
        self._intents: list[dict] = []
        self._mission_running = True  # mission is "running" at battle start
        self._auto_respawn = True

    # --- Mission state ---
    def is_mission_running(self) -> bool:
        return self._mission_running

    def is_auto_respawn_restart_enabled(self) -> bool:
        return self._auto_respawn

    def set_auto_respawn_restart(self, enabled: bool) -> None:
        self._auto_respawn = enabled

    def eject_and_dive(self) -> None:
        self._mission_running = False
        self._intents.append({"action_type": "eject_and_dive"})

    def restart_last_mission(self) -> bool:
        self._mission_running = True
        self._intents.append({"action_type": "restart_last_mission"})
        return True

    def stop_eject_sequence(self) -> None:
        self._intents.append({"action_type": "stop_eject_sequence"})

    # --- Clicks / crops ---
    def click_crop(self, _crop, block=False, count=1, region_name=None) -> None:
        self._intents.append(
            {"action_type": "click_crop", "region_name": region_name, "count": count}
        )

    def start_game_starting_loop(self) -> None:
        pass

    def cancel_mission(self) -> None:
        self._intents.append({"action_type": "cancel_mission"})

    def deploy_flares(self, hold_seconds=0.05, block=True, ignore_cancel=True) -> None:
        self._intents.append({"action_type": "deploy_flares"})

    def reload_flares(self) -> None:
        self._intents.append({"action_type": "reload_flares"})

    def disengage_roll_right(self) -> None:
        self._intents.append({"action_type": "disengage_roll_right"})

    def popup_click_allowed(self, _popup) -> bool:
        return False

    def record_popup_click(self, _popup) -> None:
        pass

    def cleanup(self) -> None:
        pass

    def get_action_intents(self) -> list[dict]:
        return list(self._intents)


# ---------------------------------------------------------------------------
# Test runner helper
# ---------------------------------------------------------------------------

def _run_replay(
    *,
    tmp_path: Path,
    monkeypatch,
    config_path: Path,
    replay_yaml: Path,
    path_name: str,
    exit_after: float = 15.0,
) -> tuple[dict, object | None]:
    """Run wingman_main.main() with the given config and return the assertions payload."""
    report = tmp_path / "report.json"
    intents = tmp_path / "intents.json"
    assertions_out = tmp_path / "assertions.json"

    # Capture the ReplayAssertionEngine instance so we can inspect it after main().
    saved: dict = {}
    _orig_engine = wingman_main.ReplayAssertionEngine

    class _TrackerEngine(wingman_main.ReplayAssertionEngine):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            saved["engine"] = self

    monkeypatch.setattr(wingman_main, "ReplayAssertionEngine", _TrackerEngine)
    monkeypatch.setattr(wingman_main, "Controller", FakeController)
    monkeypatch.setattr(wingman_main, "PerformanceTracker", FakePerformanceTracker)

    old_argv = sys.argv
    sys.argv = [
        "wingman",
        "--config", str(config_path),
        "--replay-config", str(replay_yaml),
        "--replay-path", path_name,
        "--replay-screenshot-dir", str(_SCREENSHOT_DIR),
        "--replay-exit-after", str(exit_after),
        "--replay-report", str(report),
        "--replay-intents-output", str(intents),
        "--replay-assertions-output", str(assertions_out),
        "--log-level", "WARNING",
    ]
    try:
        wingman_main.main()
    finally:
        sys.argv = old_argv
        monkeypatch.setattr(wingman_main, "ReplayAssertionEngine", _orig_engine)

    assert assertions_out.exists(), "replay_assertions.json was not written"
    payload = json.loads(assertions_out.read_text(encoding="utf-8"))
    return payload, saved.get("engine")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_path1_ocr_regression(tmp_path, monkeypatch):
    """PATH1 full OCR regression: LOBBY → WAITING → STARTING → BATTLE → RESPAWN → ALIVE."""
    path1_screenshots = [
        "P1_000_LOBBY_PLAY.png",
        "P1_010_WAITING_CANCEL_VISIBLE.png",
        "P1_020_GOOD_LUCK_VISIBLE.png",
        "P1_030_BATTLE_HUD_MISSILES_4.png",
        "P1_040_BATTLE_HUD_MISSILES_0_HEALTH_ALIVE.png",
        "P1_050_RESPAWN_VISIBLE_NO_HEALTH.png",
        "P1_060_BATTLE_HUD_HEALTH_ALIVE_MISSILES_4.png",
        "P1_070_CLICK_TO_CONTINUE.png",
        "P1_080_LOBBY_AFTER_MISSION.png",
    ]
    _check_screenshots_ready(path1_screenshots)

    config_path = _build_test_config(tmp_path)
    replay_yaml = _build_path1_ocr_yaml(tmp_path)

    payload, _engine = _run_replay(
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
        config_path=config_path,
        replay_yaml=replay_yaml,
        path_name="PATH1_OCR",
        exit_after=5.0,
    )

    assertions = payload["assertions"]
    assert not assertions["has_failures"], (
        "PATH1_OCR has assertion failures:\n" + "\n".join(assertions.get("failures", []))
    )
    assert assertions["is_complete"], (
        "PATH1_OCR did not complete all checkpoints. Results:\n"
        + json.dumps(assertions.get("results", []), indent=2)
    )


def test_path2_ocr_regression(tmp_path, monkeypatch):
    """PATH2 full OCR regression: BATTLE (seeded) → missiles_empty → manual_takeover → RESPAWN → ALIVE."""
    path2_screenshots = [
        "P2_000_BATTLE_HUD_MISSILES_4.png",
        "P2_010_BATTLE_HUD_MISSILES_0.png",
        "P2_020_MANUAL_TAKEOVER_MOMENT.png",
        "P2_030_GAME_BATTLE_MANUAL_HUD.png",
        "P2_040_RESPAWN_VISIBLE_NO_HEALTH.png",
        "P2_050_RESPAWN_CLEAR_HEALTH_ALIVE_MISSILES_4.png",
        "P2_060_CLICK_TO_CONTINUE.png",
        "P2_070_LOBBY_AFTER_MISSION.png",
    ]
    _check_screenshots_ready(path2_screenshots)

    config_path = _build_test_config(tmp_path)
    replay_yaml = _build_path2_ocr_yaml(tmp_path)

    payload, _engine = _run_replay(
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
        config_path=config_path,
        replay_yaml=replay_yaml,
        path_name="PATH2_OCR",
        exit_after=5.0,
    )

    assertions = payload["assertions"]
    assert not assertions["has_failures"], (
        "PATH2_OCR has assertion failures:\n" + "\n".join(assertions.get("failures", []))
    )
    assert assertions["is_complete"], (
        "PATH2_OCR did not complete all checkpoints. Results:\n"
        + json.dumps(assertions.get("results", []), indent=2)
    )
