from pathlib import Path

import cv2
import numpy as np

from wingman.controller import Controller, DEPLOY_FLARES_KEY
from wingman.crop_region import CropCoords
from wingman.replay import (
    ReplayAssertionEngine,
    ScreenshotReplayCapture,
    build_required_screenshot_dictionary,
    find_missing_screenshots,
    load_replay_paths,
)


def test_load_replay_paths_accepts_top_level_mapping(tmp_path: Path):
    config = tmp_path / "replay.yaml"
    config.write_text(
        """
PATH1:
  - [CANCEL.png, 0.0]
  - [RESPAWN.png, 1.5]
""".strip(),
        encoding="utf-8",
    )

    paths = load_replay_paths(config)

    assert "PATH1" in paths
    assert [s.screenshot_name for s in paths["PATH1"]] == ["CANCEL.png", "RESPAWN.png"]
    assert [s.injection_time_s for s in paths["PATH1"]] == [0.0, 1.5]


def test_replay_capture_persists_frame_until_replaced(tmp_path: Path):
    screenshot_dir = tmp_path / "shots"
    screenshot_dir.mkdir(parents=True)

    red = np.full((8, 8, 3), (0, 0, 255), dtype=np.uint8)
    green = np.full((8, 8, 3), (0, 255, 0), dtype=np.uint8)
    cv2.imwrite(str(screenshot_dir / "A.png"), red)
    cv2.imwrite(str(screenshot_dir / "B.png"), green)

    config = tmp_path / "replay.yaml"
    config.write_text(
        """
PATH1:
  - [A.png, 0.0]
  - [B.png, 1.0]
""".strip(),
        encoding="utf-8",
    )
    steps = load_replay_paths(config)["PATH1"]

    now = {"t": 0.0}
    cap = ScreenshotReplayCapture(
        region=(0, 0, 8, 8),
        screenshot_dir=screenshot_dir,
        steps=steps,
        time_fn=lambda: now["t"],
    )

    frame_t0 = cap.get_frame()
    now["t"] = 0.5
    frame_t05 = cap.get_frame()
    now["t"] = 1.1
    frame_t11 = cap.get_frame()

    # BGR channel checks to verify replacement timing.
    assert int(frame_t0[0, 0, 2]) == 255
    assert int(frame_t05[0, 0, 2]) == 255
    assert int(frame_t11[0, 0, 1]) == 255


def test_replay_capture_reports_activated_steps(tmp_path: Path):
    screenshot_dir = tmp_path / "shots"
    screenshot_dir.mkdir(parents=True)

    a = np.full((8, 8, 3), (0, 0, 255), dtype=np.uint8)
    b = np.full((8, 8, 3), (0, 255, 0), dtype=np.uint8)
    cv2.imwrite(str(screenshot_dir / "A.png"), a)
    cv2.imwrite(str(screenshot_dir / "B.png"), b)

    steps = load_replay_paths(
        _write_config(
            tmp_path,
            """
PATH1:
  - [A.png, 0.0]
  - [B.png, 1.0]
""".strip(),
        )
    )["PATH1"]

    now = {"t": 0.0}
    cap = ScreenshotReplayCapture(
        region=(0, 0, 8, 8),
        screenshot_dir=screenshot_dir,
        steps=steps,
        time_fn=lambda: now["t"],
    )

    cap.get_frame()
    activated_t0 = cap.consume_activated_steps()
    assert [s.screenshot_name for s in activated_t0] == ["A.png"]
    assert cap.consume_activated_steps() == []

    now["t"] = 1.1
    cap.get_frame()
    activated_t11 = cap.consume_activated_steps()
    assert [s.screenshot_name for s in activated_t11] == ["B.png"]


def test_required_and_missing_screenshot_detection(tmp_path: Path):
    required = build_required_screenshot_dictionary(
        {
            "PATH1": load_replay_paths(
                _write_config(
                    tmp_path,
                    """
PATH1:
  - [HAS.png, 0.0]
  - [MISSING.png, 1.0]
""".strip(),
                )
            )["PATH1"]
        }
    )

    screenshot_dir = tmp_path / "shots"
    screenshot_dir.mkdir(parents=True)
    cv2.imwrite(str(screenshot_dir / "HAS.png"), np.zeros((8, 8, 3), dtype=np.uint8))

    missing = find_missing_screenshots(required, screenshot_dir)

    assert missing["PATH1"] == ["MISSING.png"]


def test_load_replay_paths_parses_step_expectations(tmp_path: Path):
    config = _write_config(
        tmp_path,
        """
PATH1:
  - screenshot_name: WAITING_CANCEL.png
    injection_time_s: 1.0
    expected_state: GAME_WAITING
    expected_trigger: cancel_detected
    max_settle_time_s: 2.5
""".strip(),
    )
    step = load_replay_paths(config)["PATH1"][0]

    assert step.screenshot_name == "WAITING_CANCEL.png"
    assert step.injection_time_s == 1.0
    assert step.expected_state == "GAME_WAITING"
    assert step.expected_trigger == "cancel_detected"
    assert step.max_settle_time_s == 2.5


def test_replay_assertion_engine_passes_when_state_and_trigger_arrive_in_window(tmp_path: Path):
    steps = load_replay_paths(
        _write_config(
            tmp_path,
            """
PATH1:
  - screenshot_name: WAITING_CANCEL.png
    injection_time_s: 1.0
    expected_state: GAME_WAITING
    expected_trigger: cancel_detected
    max_settle_time_s: 2.0
""".strip(),
        )
    )["PATH1"]
    engine = ReplayAssertionEngine("PATH1", steps)

    engine.on_step_activated(steps[0], 1.0)
    engine.on_state("GAME_WAITING", 1.2)
    engine.on_event("cancel_detected", 1.8)
    engine.tick(2.0)

    assert engine.has_failures() is False
    assert engine.is_complete() is True


def test_replay_assertion_engine_buffers_next_checkpoints_early_trigger(tmp_path: Path):
    """An FSM cascade delivering the NEXT checkpoint's trigger early must not fail.

    manual_takeover -> state_enter:game_battle_manual arrive back-to-back; the
    second lands while the first checkpoint is still current. The engine used
    to hard-fail with "Out-of-order replay trigger" (the PATH2_OCR red lane);
    now it buffers the early trigger and credits it when the checkpoint
    becomes current — matching LivePathCaptureEngine's behavior.
    """
    steps = load_replay_paths(
        _write_config(
            tmp_path,
            """
PATH1:
  - screenshot_name: TAKEOVER.png
    injection_time_s: 0.0
    expected_state: GAME_BATTLE
    expected_trigger: manual_takeover
    max_settle_time_s: 2.0
  - screenshot_name: MANUAL_HUD.png
    injection_time_s: 1.0
    expected_state: GAME_BATTLE_MANUAL
    expected_trigger: state_enter:GAME_BATTLE_MANUAL
    max_settle_time_s: 2.0
""".strip(),
        )
    )["PATH1"]
    engine = ReplayAssertionEngine("PATH1", steps)

    engine.on_step_activated(steps[0], 0.0)
    # The current checkpoint is NOT yet complete (its expected_state has not
    # been observed) when its trigger AND the next checkpoint's cascade event
    # arrive back-to-back — the exact PATH2_OCR production shape, where the
    # FSM sat in GAME_BATTLE_EJECT so P2_020's stale expected_state never met.
    engine.on_event("manual_takeover", 0.2)
    engine.on_event("state_enter:GAME_BATTLE_MANUAL", 0.201)
    # Historically the second event hard-failed here ("Out-of-order replay
    # trigger") even though the run itself was correct.
    assert engine.has_failures() is False, engine.failures
    engine.on_state("GAME_BATTLE", 0.3)   # current completes late
    engine.on_state("GAME_BATTLE_MANUAL", 0.4)
    engine.on_step_activated(steps[1], 1.0)
    engine.tick(1.5)

    assert engine.has_failures() is False, engine.failures
    assert engine.is_complete() is True


def test_replay_assertion_engine_still_fails_on_genuinely_out_of_order_trigger(tmp_path: Path):
    """A trigger matching a checkpoint BEYOND the next is still a hard failure."""
    steps = load_replay_paths(
        _write_config(
            tmp_path,
            """
PATH1:
  - screenshot_name: A.png
    injection_time_s: 0.0
    expected_trigger: trigger_a
    max_settle_time_s: 2.0
  - screenshot_name: B.png
    injection_time_s: 1.0
    expected_trigger: trigger_b
    max_settle_time_s: 2.0
  - screenshot_name: C.png
    injection_time_s: 2.0
    expected_trigger: trigger_c
    max_settle_time_s: 2.0
""".strip(),
        )
    )["PATH1"]
    engine = ReplayAssertionEngine("PATH1", steps)

    engine.on_step_activated(steps[0], 0.0)
    engine.on_event("trigger_c", 0.1)  # two checkpoints ahead — genuinely wrong

    assert engine.has_failures() is True
    assert any("Out-of-order" in msg for msg in engine.failures)


def test_replay_assertion_engine_fails_on_timeout(tmp_path: Path):
    steps = load_replay_paths(
        _write_config(
            tmp_path,
            """
PATH1:
  - screenshot_name: BATTLE.png
    injection_time_s: 0.0
    expected_state: GAME_BATTLE
    expected_trigger: restart_last_mission
    max_settle_time_s: 1.0
""".strip(),
        )
    )["PATH1"]
    engine = ReplayAssertionEngine("PATH1", steps)

    engine.on_step_activated(steps[0], 0.0)
    engine.on_state("GAME_BATTLE", 0.1)
    engine.tick(1.2)

    assert engine.has_failures() is True
    assert any("restart_last_mission" in msg for msg in engine.failures)


def test_controller_simulated_input_records_action_intents():
    ctrl = Controller(
        region=(0, 0, 100, 100),
        simulate_os_input=True,
        disable_hotkeys=True,
    )

    ctrl.deploy_flares(hold_seconds=0.01, block=True)
    ctrl.click_crop(CropCoords(0.1, 0.2, 0.3, 0.4), block=True, count=2, region_name="PLAY")

    intents = ctrl.get_action_intents()

    assert any(i["action_type"] == "key_press" and i.get("key") == DEPLOY_FLARES_KEY for i in intents)
    assert any(i["action_type"] == "key_release" and i.get("key") == DEPLOY_FLARES_KEY for i in intents)
    assert any(i["action_type"] == "click_crop" and i.get("region_name") == "PLAY" for i in intents)


def _write_config(tmp_path: Path, content: str) -> Path:
    config = tmp_path / "replay.yaml"
    config.write_text(content, encoding="utf-8")
    return config
