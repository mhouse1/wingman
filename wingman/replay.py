import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import cv2
import numpy as np
import yaml


@dataclass
class ReplayStep:
    screenshot_name: str
    injection_time_s: float


@dataclass
class ReplayPath:
    path_name: str
    steps: list[ReplayStep]


def _normalize_step(step: Any) -> ReplayStep:
    if isinstance(step, dict):
        screenshot = step.get("screenshot_name") or step.get("SCREENSHOTNAME")
        inject = step.get("injection_time_s")
        if inject is None:
            inject = step.get("TIME_TO_INJECT")
        if screenshot is None or inject is None:
            raise ValueError(f"Invalid replay step dict: {step!r}")
        return ReplayStep(str(screenshot), float(inject))

    if isinstance(step, (list, tuple)) and len(step) >= 2:
        return ReplayStep(str(step[0]), float(step[1]))

    raise ValueError(f"Unsupported replay step format: {step!r}")


def load_replay_paths(config_path: Path) -> dict[str, list[ReplayStep]]:
    data = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    if data is None:
        raise ValueError(f"Replay config is empty: {config_path}")

    # Accept either:
    # 1) top-level path mapping: { PATH1: [[file, t], ...], PATH2: ... }
    # 2) nested mapping under 'paths'
    if isinstance(data, dict) and "paths" in data and isinstance(data["paths"], dict):
        path_map = data["paths"]
    elif isinstance(data, dict):
        path_map = data
    else:
        raise ValueError("Replay config must be a mapping of paths")

    normalized: dict[str, list[ReplayStep]] = {}
    for path_name, raw_steps in path_map.items():
        if not isinstance(raw_steps, list):
            continue
        steps = [_normalize_step(step) for step in raw_steps]
        steps.sort(key=lambda s: s.injection_time_s)
        normalized[str(path_name)] = steps

    if not normalized:
        raise ValueError("Replay config contains no valid paths")

    return normalized


def select_replay_path(config_path: Path, path_name: str | None = None) -> ReplayPath:
    path_map = load_replay_paths(config_path)
    chosen_name = path_name or sorted(path_map.keys())[0]
    if chosen_name not in path_map:
        raise ValueError(f"Replay path '{chosen_name}' not found in {config_path}")
    return ReplayPath(path_name=chosen_name, steps=path_map[chosen_name])


def build_required_screenshot_dictionary(path_map: dict[str, list[ReplayStep]]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for path_name, steps in path_map.items():
        # Preserve first-seen order while deduplicating
        seen: set[str] = set()
        required: list[str] = []
        for step in steps:
            if step.screenshot_name not in seen:
                required.append(step.screenshot_name)
                seen.add(step.screenshot_name)
        out[path_name] = required
    return out


def find_missing_screenshots(required: dict[str, list[str]], screenshot_dir: Path) -> dict[str, list[str]]:
    missing: dict[str, list[str]] = {}
    for path_name, names in required.items():
        missing_names = [name for name in names if not (screenshot_dir / name).exists()]
        missing[path_name] = missing_names
    return missing


def write_required_screenshot_report(
    report_path: Path,
    screenshot_dir: Path,
    required: dict[str, list[str]],
    missing: dict[str, list[str]],
) -> None:
    report_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "screenshot_dir": str(screenshot_dir),
        "required_screenshots": required,
        "missing_screenshots": missing,
        "generated_at": time.time(),
    }
    report_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


class ScreenshotReplayCapture:
    """Drop-in capture replacement for replay mode.

    Frames are injected by absolute time since replay start and then persist until
    replaced by the next scheduled screenshot.
    """

    def __init__(
        self,
        region: tuple[int, int, int, int],
        screenshot_dir: Path,
        steps: list[ReplayStep],
        time_fn=time.time,
    ):
        self.region = region
        self.monitor_index = 1  # kept for compatibility with controller logging paths
        self._screenshot_dir = screenshot_dir
        self._steps = sorted(steps, key=lambda s: s.injection_time_s)
        self._time_fn = time_fn
        self._start_ts: float | None = None
        self._active_index = -1
        self._active_frame: np.ndarray | None = None
        self._end_time_s = self._steps[-1].injection_time_s if self._steps else 0.0

    def _load_frame(self, screenshot_name: str) -> np.ndarray:
        image_path = self._screenshot_dir / screenshot_name
        frame = cv2.imread(str(image_path))
        if frame is None:
            raise FileNotFoundError(f"Replay screenshot not found or unreadable: {image_path}")

        _, _, width, height = self.region
        if frame.shape[1] != width or frame.shape[0] != height:
            frame = cv2.resize(frame, (width, height), interpolation=cv2.INTER_AREA)
        return frame

    def elapsed_s(self) -> float:
        if self._start_ts is None:
            return 0.0
        return self._time_fn() - self._start_ts

    def is_finished(self, grace_s: float = 0.0) -> bool:
        return self.elapsed_s() >= (self._end_time_s + grace_s)

    def get_frame(self) -> np.ndarray:
        if self._start_ts is None:
            self._start_ts = self._time_fn()

        elapsed = self.elapsed_s()
        while (self._active_index + 1) < len(self._steps):
            next_idx = self._active_index + 1
            step = self._steps[next_idx]
            if elapsed < step.injection_time_s:
                break
            self._active_index = next_idx
            self._active_frame = self._load_frame(step.screenshot_name)

        if self._active_frame is None:
            # No frame is active yet; return a blank frame until the first injection time.
            _, _, width, height = self.region
            self._active_frame = np.zeros((height, width, 3), dtype=np.uint8)

        return self._active_frame.copy()
