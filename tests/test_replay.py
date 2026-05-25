from pathlib import Path

import cv2
import numpy as np

from wingman.controller import Controller, DEPLOY_FLARES_KEY
from wingman.crop_region import CropCoords
from wingman.replay import (
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
