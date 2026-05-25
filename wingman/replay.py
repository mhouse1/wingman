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
    expected_state: str | None = None
    expected_trigger: str | None = None
    max_settle_time_s: float | None = None


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

        expected_state = step.get("expected_state")
        expected_trigger = step.get("expected_trigger")
        max_settle = step.get("max_settle_time_s")
        return ReplayStep(
            screenshot_name=str(screenshot),
            injection_time_s=float(inject),
            expected_state=str(expected_state) if expected_state is not None else None,
            expected_trigger=str(expected_trigger) if expected_trigger is not None else None,
            max_settle_time_s=float(max_settle) if max_settle is not None else None,
        )

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
        self._activated_indices: list[int] = []
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
            self._activated_indices.append(next_idx)

        if self._active_frame is None:
            # No frame is active yet; return a blank frame until the first injection time.
            _, _, width, height = self.region
            self._active_frame = np.zeros((height, width, 3), dtype=np.uint8)

        return self._active_frame.copy()

    def consume_activated_steps(self) -> list[ReplayStep]:
        """Return newly activated steps since the last poll, in order."""
        if not self._activated_indices:
            return []
        out = [self._steps[i] for i in self._activated_indices]
        self._activated_indices = []
        return out


def _normalize_expected_trigger(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip().lower()
    aliases = {
        "manual_mode": "manual_takeover",
        "manual_mode_entered": "state_enter:game_battle_manual",
        "battle_started": "state_enter:game_battle",
    }
    return aliases.get(normalized, normalized)


def _normalize_state(value: str | None) -> str | None:
    if value is None:
        return None
    return value.strip().lower()


@dataclass
class ReplayCheckpointResult:
    screenshot_name: str
    injection_time_s: float
    expected_state: str | None
    expected_trigger: str | None
    max_settle_time_s: float
    status: str = "pending"
    activated_at_s: float | None = None
    state_met_at_s: float | None = None
    trigger_met_at_s: float | None = None
    failure_reason: str | None = None


class ReplayAssertionEngine:
    """Evaluates replay expectations with timing and ordering constraints."""

    def __init__(self, path_name: str, steps: list[ReplayStep], default_settle_s: float = 3.0):
        self.path_name = path_name
        self.default_settle_s = float(default_settle_s)
        self._results: list[ReplayCheckpointResult] = []
        for step in steps:
            if not step.expected_state and not step.expected_trigger:
                continue
            settle = step.max_settle_time_s if step.max_settle_time_s is not None else self.default_settle_s
            self._results.append(
                ReplayCheckpointResult(
                    screenshot_name=step.screenshot_name,
                    injection_time_s=step.injection_time_s,
                    expected_state=_normalize_state(step.expected_state),
                    expected_trigger=_normalize_expected_trigger(step.expected_trigger),
                    max_settle_time_s=float(settle),
                )
            )
        self._active_index = 0
        self._failures: list[str] = []
        self._event_trace: list[dict[str, Any]] = []

    def _current(self) -> ReplayCheckpointResult | None:
        if self._active_index >= len(self._results):
            return None
        return self._results[self._active_index]

    def _mark_satisfied_if_complete(self, checkpoint: ReplayCheckpointResult, now_s: float) -> None:
        state_ok = checkpoint.expected_state is None or checkpoint.state_met_at_s is not None
        trig_ok = checkpoint.expected_trigger is None or checkpoint.trigger_met_at_s is not None
        if state_ok and trig_ok and checkpoint.status == "pending":
            checkpoint.status = "passed"
            if checkpoint.activated_at_s is None:
                checkpoint.activated_at_s = now_s
            self._active_index += 1

    def on_step_activated(self, step: ReplayStep, now_s: float) -> None:
        checkpoint = self._current()
        if checkpoint is None:
            return
        if checkpoint.screenshot_name != step.screenshot_name:
            return
        checkpoint.activated_at_s = now_s
        self._mark_satisfied_if_complete(checkpoint, now_s)

    def on_event(self, event_name: str, now_s: float) -> None:
        normalized_event = _normalize_expected_trigger(event_name)
        self._event_trace.append({"at_s": now_s, "event": normalized_event})
        checkpoint = self._current()
        if checkpoint is None:
            return

        if checkpoint.expected_trigger == normalized_event:
            checkpoint.trigger_met_at_s = now_s
            self._mark_satisfied_if_complete(checkpoint, now_s)
            return

        for future in self._results[self._active_index + 1:]:
            if future.expected_trigger == normalized_event:
                msg = (
                    "Out-of-order replay trigger: "
                    f"'{normalized_event}' matched future checkpoint '{future.screenshot_name}' "
                    f"before current checkpoint '{checkpoint.screenshot_name}'"
                )
                self._failures.append(msg)
                return

    def on_state(self, state_name: str, now_s: float) -> None:
        checkpoint = self._current()
        if checkpoint is None:
            return
        if checkpoint.expected_state == _normalize_state(state_name):
            checkpoint.state_met_at_s = now_s
            self._mark_satisfied_if_complete(checkpoint, now_s)

    def tick(self, now_s: float) -> None:
        checkpoint = self._current()
        if checkpoint is None or checkpoint.status != "pending":
            return
        if checkpoint.activated_at_s is None:
            return
        deadline_s = checkpoint.activated_at_s + checkpoint.max_settle_time_s
        if now_s <= deadline_s:
            return

        missing: list[str] = []
        if checkpoint.expected_state and checkpoint.state_met_at_s is None:
            missing.append(f"state={checkpoint.expected_state}")
        if checkpoint.expected_trigger and checkpoint.trigger_met_at_s is None:
            missing.append(f"trigger={checkpoint.expected_trigger}")
        checkpoint.status = "failed"
        checkpoint.failure_reason = (
            "Checkpoint timeout waiting for "
            + ", ".join(missing)
            + f" within {checkpoint.max_settle_time_s:.1f}s"
        )
        self._failures.append(checkpoint.failure_reason)
        self._active_index += 1

    @property
    def failures(self) -> list[str]:
        return list(self._failures)

    def has_failures(self) -> bool:
        return bool(self._failures) or any(r.status == "failed" for r in self._results)

    def is_complete(self) -> bool:
        return self._active_index >= len(self._results)

    def to_dict(self) -> dict[str, Any]:
        return {
            "path_name": self.path_name,
            "has_failures": self.has_failures(),
            "is_complete": self.is_complete(),
            "failures": self.failures,
            "checkpoints": [
                {
                    "screenshot_name": r.screenshot_name,
                    "injection_time_s": r.injection_time_s,
                    "expected_state": r.expected_state,
                    "expected_trigger": r.expected_trigger,
                    "max_settle_time_s": r.max_settle_time_s,
                    "status": r.status,
                    "activated_at_s": r.activated_at_s,
                    "state_met_at_s": r.state_met_at_s,
                    "trigger_met_at_s": r.trigger_met_at_s,
                    "failure_reason": r.failure_reason,
                }
                for r in self._results
            ],
            "event_trace": self._event_trace,
        }
