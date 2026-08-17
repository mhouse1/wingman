import json
import sys
import threading
import time
from pathlib import Path

import cv2
import numpy as np

import wingman.main as wingman_main
from wingman.replay import load_replay_paths


class FakePerformanceTracker:
    def __init__(self, *_args, **_kwargs):
        pass

    def on_enter_game_lobby(self):
        pass

    def on_session_end(self):
        pass


class FakeController:
    def __init__(self, *_args, **_kwargs):
        self._intents = []

    def start_game_starting_loop(self):
        pass

    def click_crop(self, _crop, block=False, count=1, region_name=None):
        self._intents.append(
            {
                "action_type": "click_crop",
                "region_name": region_name,
                "count": count,
                "block": bool(block),
            }
        )

    def popup_click_allowed(self, _popup):
        return False

    def record_popup_click(self, _popup):
        pass

    def cancel_mission(self):
        pass

    def is_mission_running(self):
        return False

    def is_auto_respawn_restart_enabled(self):
        return True

    def restart_last_mission(self):
        self._intents.append({"action_type": "restart_last_mission"})
        return True

    def set_auto_respawn_restart(self, _enabled):
        pass

    def stop_eject_sequence(self):
        pass

    # ADR 076 spawn-attitude guard hooks (called by the respawn flow).
    def start_spawn_guard(self):
        pass

    def notify_spawn_alive(self):
        pass

    def deploy_flares(self, hold_seconds=0.05, block=True, ignore_cancel=True):
        self._intents.append(
            {
                "action_type": "deploy_flares",
                "hold_seconds": hold_seconds,
                "block": bool(block),
                "ignore_cancel": bool(ignore_cancel),
            }
        )

    def reload_flares(self):
        self._intents.append({"action_type": "reload_flares"})

    def eject_and_dive(self):
        self._intents.append({"action_type": "eject_and_dive"})

    def disengage_roll_right(self):
        self._intents.append({"action_type": "disengage_roll_right"})

    def cleanup(self):
        pass

    def get_action_intents(self):
        return list(self._intents)


class FakeAnalyzer:
    def __init__(self, _cfg, tracker=None):
        self._tracker = tracker
        self._game_state = wingman_main.GameState.GAME_LOBBY
        self._on_cancel_mission = None
        self._on_start_game_starting_loop = None
        self._on_lobby_play_click = None
        self._on_lobby_popup_click = None
        self._on_fsm_transition = None

        self._tick = 0
        self._ammo_missiles = 4

        self.incoming_event = threading.Event()
        self.alive_event = threading.Event()
        self.low_flares_event = threading.Event()
        self.no_missiles_event = threading.Event()

        # Minimal crop map used by main paths that reference crop keys.
        self.crops = {
            "PLAY": (0.0, 0.0, 1.0, 1.0),
            "READY": (0.0, 0.0, 1.0, 1.0),
            "CANCEL": (0.0, 0.0, 1.0, 1.0),
        }

    @property
    def game_state(self):
        return self._game_state

    def _transition(self, trigger_name, next_state):
        prev_state = self._game_state
        self._game_state = next_state
        if self._on_fsm_transition is not None:
            self._on_fsm_transition(trigger_name, prev_state.name, next_state.name, time.time())
        if next_state == wingman_main.GameState.GAME_STARTING and self._on_start_game_starting_loop:
            self._on_start_game_starting_loop()

    def analyze_frame(self, _frame):
        self._tick += 1
        if self._tick == 2 and self._game_state == wingman_main.GameState.GAME_LOBBY:
            self._transition("play_clicked", wingman_main.GameState.GAME_WAITING)
        elif self._tick == 3 and self._game_state == wingman_main.GameState.GAME_WAITING:
            self._transition("cancel_detected", wingman_main.GameState.GAME_STARTING)
        elif self._tick == 4 and self._game_state == wingman_main.GameState.GAME_STARTING:
            self._transition("good_luck_detected", wingman_main.GameState.GAME_BATTLE)

        return {
            "game_state": self._game_state,
            "is_respawning": False,
            "respawn_confidence": 0.0,
        }

    def trigger_event(self, name):
        if name == "cancel_detected" and self._game_state == wingman_main.GameState.GAME_WAITING:
            self._transition("cancel_detected", wingman_main.GameState.GAME_STARTING)
            return True
        return False

    def set_on_cancel_mission(self, callback):
        self._on_cancel_mission = callback

    def set_on_start_game_starting_loop(self, callback):
        self._on_start_game_starting_loop = callback

    def set_on_lobby_play_click(self, callback):
        self._on_lobby_play_click = callback

    def set_on_lobby_popup_click(self, callback):
        self._on_lobby_popup_click = callback

    def set_on_fsm_transition(self, callback):
        self._on_fsm_transition = callback

    def scan_region_for_cancel(self, _frame):
        return False

    def scan_region_for_play_button(self, _frame):
        return None

    def compute_waiting_cancel_diff(self, _frame):
        return None

    def get_ammo_missiles(self):
        return self._ammo_missiles

    def get_respawn_cache_result(self):
        return (False, 0.0, None)

    def get_incoming_cache_result(self):
        return (False, 0.0, None)

    def get_incoming_cache_timestamp(self):
        return 0.0

    def get_click_to_cache_result(self):
        return (False, 0.0, None)

    def get_click_to_cache_timestamp(self):
        return 0.0

    def detect_enemy_red(self, _frame):
        return True

    def cleanup(self):
        if self._tracker is not None:
            self._tracker.on_session_end()


def _write_minimal_main_config(path: Path) -> None:
    path.write_text(
        """
region:
  left: 0
  top: 0
  width: 8
  height: 8
monitor: 1
loop_interval_sec: 0.01
mission:
  waiting_fallback_enabled: false
""".strip()
        + "\n",
        encoding="utf-8",
    )


def test_make_y_replay_integration_smoke(tmp_path, monkeypatch):
    replay_cfg = Path(__file__).resolve().parent / "replay_paths" / "adr037_paths.yaml"
    steps = load_replay_paths(replay_cfg)["SMOKE_PATH"]

    screenshot_dir = tmp_path / "shots"
    screenshot_dir.mkdir(parents=True)
    for idx, step in enumerate(steps):
        frame = np.full((8, 8, 3), (idx * 40, 120, 200), dtype=np.uint8)
        ok = cv2.imwrite(str(screenshot_dir / step.screenshot_name), frame)
        assert ok

    cfg_path = tmp_path / "main_config.yaml"
    _write_minimal_main_config(cfg_path)

    replay_report = tmp_path / "replay_required_screenshots.json"
    replay_intents = tmp_path / "replay_action_intents.json"
    replay_assertions = tmp_path / "replay_assertions.json"

    monkeypatch.setattr(wingman_main, "GameStateAnalyzer", FakeAnalyzer)
    monkeypatch.setattr(wingman_main, "Controller", FakeController)
    monkeypatch.setattr(wingman_main, "PerformanceTracker", FakePerformanceTracker)

    old_argv = sys.argv
    sys.argv = [
        "wingman",
        "--config",
        str(cfg_path),
        "--replay-config",
        str(replay_cfg),
        "--replay-path",
        "SMOKE_PATH",
        "--replay-screenshot-dir",
        str(screenshot_dir),
        "--replay-exit-after",
        "2.0",
        "--replay-report",
        str(replay_report),
        "--replay-intents-output",
        str(replay_intents),
        "--replay-assertions-output",
        str(replay_assertions),
    ]
    try:
        wingman_main.main()
    finally:
        sys.argv = old_argv

    assert replay_report.exists()
    assert replay_intents.exists()
    assert replay_assertions.exists()

    assertions_payload = json.loads(replay_assertions.read_text(encoding="utf-8"))
    assertions = assertions_payload["assertions"]
    assert assertions["path_name"] == "SMOKE_PATH"
    assert assertions["has_failures"] is False
    assert assertions["is_complete"] is True
