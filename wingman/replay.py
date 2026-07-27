import json
import shutil
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
    inject_trigger: str | None = None  # FSM trigger to fire when this step activates
    reuse_from: str | None = None      # Optional screenshot filename to copy instead of recapturing


@dataclass
class ReplayPath:
    path_name: str
    steps: list[ReplayStep]


@dataclass
class LiveCaptureStepResult:
    screenshot_name: str
    status: str = "pending"
    captured: bool = False
    capture_time_s: float | None = None
    readiness_source: str | None = None
    timeout: bool = False
    notes: str | None = None


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
        inject_trigger = step.get("inject_trigger")
        reuse_from = step.get("reuse_from")
        return ReplayStep(
            screenshot_name=str(screenshot),
            injection_time_s=float(inject),
            expected_state=str(expected_state) if expected_state is not None else None,
            expected_trigger=str(expected_trigger) if expected_trigger is not None else None,
            max_settle_time_s=float(max_settle) if max_settle is not None else None,
            inject_trigger=str(inject_trigger) if inject_trigger is not None else None,
            reuse_from=str(reuse_from) if reuse_from is not None else None,
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

    def grab_from_thread(self) -> np.ndarray:
        """Alias for get_frame() — replay frames are already thread-safe."""
        return self.get_frame()

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


class LivePathCaptureEngine:
    """Captures real runtime frames to replay fixture filenames.

    Each path step is evaluated in order and captured once when its readiness
    condition is met and passes basic quality gates.
    """

    def __init__(
        self,
        *,
        path_name: str,
        steps: list[ReplayStep],
        screenshot_dir: Path,
        region: tuple[int, int, int, int],
        overwrite: bool,
        timeout_s: float,
        allow_inject: bool,
        start_at_step: str | None = None,
        debounce_required: int = 2,
        auto_resume: bool = True,
        timeout_advances: bool = True,
        out_of_order: bool = False,
        trigger_freshness_s: float = 2.0,
        time_fn=time.time,
    ):
        if timeout_s <= 0:
            raise ValueError("capture timeout must be > 0")

        self.path_name = path_name
        self._steps = list(steps)
        self._screenshot_dir = screenshot_dir
        self._overwrite = overwrite
        self._timeout_s = float(timeout_s)
        self._allow_inject = allow_inject
        self._debounce_required = max(1, int(debounce_required))
        self._auto_resume = bool(auto_resume)
        self._timeout_advances = bool(timeout_advances)
        self._out_of_order = bool(out_of_order)
        self._trigger_freshness_s = max(0.1, float(trigger_freshness_s))
        self._time_fn = time_fn

        _, _, width, height = region
        self._expected_width = int(width)
        self._expected_height = int(height)

        self._start_ts: float | None = None
        self._end_ts: float | None = None
        self._first_evaluate_s: float | None = None
        self._oo_deadline_s: float = self._timeout_s
        self._current_index = 0
        self._current_started_s: float | None = None
        self._current_injected = False

        self._trigger_ready = False
        self._state_ready = False
        self._ready_source: str | None = None
        self._ready_count = 0
        self._pending_inject_trigger: str | None = None
        # Lookahead buffer: holds a trigger that fired for the *next* step while the
        # current step was still active.  Consumed by _advance() so the next step
        # starts with _trigger_ready = True immediately, avoiding a missed-event
        # timeout when the trigger fires slightly before the step becomes current.
        self._pending_next_trigger: str | None = None
        self._timeout_retries = 0
        self._seen_events: set[str] = set()
        self._event_last_seen_s: dict[str, float] = {}
        self._step_ready_source: dict[int, str | None] = {}
        self._step_ready_count: dict[int, int] = {}

        self._results: list[LiveCaptureStepResult] = [
            LiveCaptureStepResult(screenshot_name=step.screenshot_name) for step in self._steps
        ]
        self._auto_resume_applied = False

        if start_at_step is not None:
            start_index = next(
                (i for i, step in enumerate(self._steps) if step.screenshot_name == start_at_step),
                None,
            )
            if start_index is None:
                raise ValueError(f"capture start step not found: {start_at_step}")
            for i in range(start_index):
                self._results[i].notes = "skipped_before_start"
            self._current_index = start_index

    def _elapsed_s(self) -> float:
        if self._start_ts is None:
            return 0.0
        return self._time_fn() - self._start_ts

    def _current_step(self) -> ReplayStep | None:
        if self._current_index >= len(self._steps):
            return None
        return self._steps[self._current_index]

    def _current_result(self) -> LiveCaptureStepResult | None:
        if self._current_index >= len(self._results):
            return None
        return self._results[self._current_index]

    def _auto_resume_to_state(self, state_name: str) -> None:
        if not self._auto_resume:
            return
        if self._auto_resume_applied or self.is_complete() or self._current_started_s is not None:
            return

        normalized_state = _normalize_state(state_name)
        if normalized_state in {None, "game_unknown"}:
            return

        self._auto_resume_applied = True

        resume_index = None
        for i, step in enumerate(self._steps):
            expected_state = _normalize_state(step.expected_state)
            expected_trigger = _normalize_expected_trigger(step.expected_trigger)
            if expected_state == normalized_state:
                resume_index = i
                break
            if expected_trigger == f"state_enter:{normalized_state}":
                resume_index = i
                break

        if resume_index is None or resume_index == 0:
            return

        for i in range(resume_index):
            self._results[i].notes = "skipped_before_resume"
            self._results[i].status = "skipped"
        self._current_index = resume_index

    def is_complete(self) -> bool:
        if self._out_of_order:
            return all(r.status in {"captured", "failed", "skipped"} for r in self._results)
        return self._current_index >= len(self._steps)

    def has_failures(self) -> bool:
        return any(
            r.status == "failed"
            or (
                r.status == "skipped"
                and r.notes not in {"skipped_before_start", "skipped_before_resume"}
            )
            for r in self._results
        )

    def not_updated_screenshot_names(self) -> list[str]:
        return [
            r.screenshot_name
            for r in self._results
            if r.status != "captured" and r.notes != "skipped_before_start"
        ]

    def _mark_ended(self, ended_at_s: float | None = None) -> None:
        if self._end_ts is None:
            self._end_ts = ended_at_s if ended_at_s is not None else self._time_fn()

    def on_event(self, event_name: str, now_s: float) -> None:
        normalized_event = _normalize_expected_trigger(event_name)
        self._seen_events.add(normalized_event)
        self._event_last_seen_s[normalized_event] = now_s
        if self._out_of_order:
            return

        step = self._current_step()
        if step is None or step.expected_trigger is None:
            return
        if normalized_event == _normalize_expected_trigger(step.expected_trigger):
            self._trigger_ready = True
        else:
            # Lookahead: if the event matches the *next* step's trigger, buffer it so
            # it isn't lost when we advance.
            next_index = self._current_index + 1
            if next_index < len(self._steps):
                next_step = self._steps[next_index]
                if (next_step.expected_trigger is not None
                        and normalized_event
                        == _normalize_expected_trigger(next_step.expected_trigger)):
                    self._pending_next_trigger = normalized_event

    def on_state(self, state_name: str, now_s: float) -> None:
        step = self._current_step()
        if step is None:
            return

        normalized_state = _normalize_state(state_name)
        if step.expected_state is not None and normalized_state == _normalize_state(step.expected_state):
            self._state_ready = True

        # If a step waits on a state-enter trigger (for example battle_started ->
        # state_enter:game_battle), allow readiness to be satisfied by the
        # current state even if the transition callback occurred before the step
        # became active.
        if step.expected_trigger is not None:
            expected_trigger = _normalize_expected_trigger(step.expected_trigger)
            if expected_trigger == f"state_enter:{normalized_state}":
                self._trigger_ready = True

    def consume_pending_inject_trigger(self) -> str | None:
        if not self._allow_inject or self._pending_inject_trigger is None:
            return None
        trigger = self._pending_inject_trigger
        self._pending_inject_trigger = None
        return trigger

    def _advance(self) -> None:
        self._current_index += 1
        self._current_started_s = None
        self._current_injected = False
        self._timeout_retries = 0
        self._trigger_ready = False
        self._state_ready = False
        self._ready_source = None
        self._ready_count = 0
        # Apply any lookahead-buffered trigger for the step we just moved to.
        step = self._current_step()
        if (self._pending_next_trigger is not None
                and step is not None
                and step.expected_trigger is not None
                and self._pending_next_trigger
                == _normalize_expected_trigger(step.expected_trigger)):
            self._trigger_ready = True
        self._pending_next_trigger = None


    def _readiness_source(self, step: ReplayStep) -> str | None:
        if step.expected_trigger is not None:
            return "trigger" if self._trigger_ready else None
        if step.expected_state is not None:
            return "state" if self._state_ready else None
        return "manual"

    def _quality_error(self, frame: np.ndarray) -> str | None:
        if frame.shape[1] != self._expected_width or frame.shape[0] != self._expected_height:
            return (
                f"quality_fail_resolution:{frame.shape[1]}x{frame.shape[0]}"
                f" expected {self._expected_width}x{self._expected_height}"
            )
        if not np.any(frame):
            return "quality_fail_black_frame"
        return None

    def _step_readiness_source(self, step: ReplayStep, normalized_state: str | None, now_s: float) -> str | None:
        expected_state = _normalize_state(step.expected_state)
        expected_trigger = _normalize_expected_trigger(step.expected_trigger)

        if expected_trigger is not None:
            trigger_seen_s = self._event_last_seen_s.get(expected_trigger)
            trigger_ready = (
                trigger_seen_s is not None
                and (now_s - trigger_seen_s) <= self._trigger_freshness_s
            )
            if normalized_state is not None and expected_trigger == f"state_enter:{normalized_state}":
                trigger_ready = True

            # In out-of-order mode, when both fields exist we require both to avoid
            # capturing an unrelated frame long after the trigger fired.
            if expected_state is not None:
                return "trigger" if (trigger_ready and normalized_state == expected_state) else None
            return "trigger" if trigger_ready else None

        if expected_state is not None:
            return "state" if normalized_state == expected_state else None

        return "manual" if normalized_state not in {None, "game_unknown"} else None

    def _capture_step(self, index: int, frame: np.ndarray, now_s: float, source: str) -> None:
        step = self._steps[index]
        result = self._results[index]

        target = self._screenshot_dir / step.screenshot_name

        if step.reuse_from:
            source_path = self._screenshot_dir / step.reuse_from
            if not source_path.exists():
                result.notes = f"reuse_source_missing:{step.reuse_from}"
                return
            if target.exists() and not self._overwrite:
                result.status = "skipped"
                result.notes = "skipped_exists"
                if self.is_complete():
                    self._mark_ended(now_s)
                return
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_path, target)
            result.captured = True
            result.status = "captured"
            result.capture_time_s = now_s
            result.readiness_source = "reused"
            result.notes = f"captured_reused_from:{step.reuse_from}"
            if self.is_complete():
                self._mark_ended(now_s)
            return

        quality_error = self._quality_error(frame)
        if quality_error is not None:
            result.notes = quality_error
            return

        if target.exists() and not self._overwrite:
            result.status = "skipped"
            result.notes = "skipped_exists"
            if self.is_complete():
                self._mark_ended(now_s)
            return

        target.parent.mkdir(parents=True, exist_ok=True)
        if not cv2.imwrite(str(target), frame):
            result.timeout = True
            result.status = "failed"
            result.notes = f"write_failed:{target}"
            self._mark_ended(now_s)
            return

        result.captured = True
        result.status = "captured"
        result.capture_time_s = now_s
        result.readiness_source = source
        result.notes = "captured"
        if step.inject_trigger and self._allow_inject:
            self._pending_inject_trigger = step.inject_trigger
        if self.is_complete():
            self._mark_ended(now_s)

    def evaluate(self, frame: np.ndarray, state_name: str, now_s: float) -> None:
        if self._start_ts is None:
            self._start_ts = self._time_fn()
        if self.is_complete():
            return

        if self._out_of_order:
            # Out-of-order mode has no per-step ordering, so the timeout is
            # lane-wide: the deadline is the last scheduled screenshot
            # (max injection_time_s) plus timeout_s of slack — timeout_s alone
            # would expire before late-scheduled steps can even appear. Once
            # past the deadline, every still-pending step is failed so
            # is_complete() terminates the main loop instead of waiting
            # forever on a step whose readiness can never occur. Measured on
            # caller-supplied now_s, the same clock the sequential per-step
            # timeout uses.
            if self._first_evaluate_s is None:
                self._first_evaluate_s = now_s
                self._oo_deadline_s = (
                    max((step.injection_time_s or 0.0) for step in self._steps)
                    + self._timeout_s
                )
            elif now_s - self._first_evaluate_s > self._oo_deadline_s:
                for result in self._results:
                    if result.status not in {"captured", "failed", "skipped"}:
                        result.timeout = True
                        result.status = "failed"
                        result.notes = f"timeout_after_{self._oo_deadline_s:.1f}s"
                self._mark_ended(now_s)
                return
            normalized_state = _normalize_state(state_name)
            for i, step in enumerate(self._steps):
                result = self._results[i]
                if result.status in {"captured", "failed", "skipped"}:
                    continue

                source = self._step_readiness_source(step, normalized_state, now_s)
                if source is None:
                    self._step_ready_source[i] = None
                    self._step_ready_count[i] = 0
                    continue

                previous = self._step_ready_source.get(i)
                if source == previous:
                    self._step_ready_count[i] = self._step_ready_count.get(i, 0) + 1
                else:
                    self._step_ready_source[i] = source
                    self._step_ready_count[i] = 1

                if self._step_ready_count[i] < self._debounce_required:
                    continue

                self._capture_step(i, frame, now_s, source)
                self._step_ready_source[i] = None
                self._step_ready_count[i] = 0
            return

        if self._current_started_s is None:
            self._auto_resume_to_state(state_name)

        # Keep state readiness live even when expected_trigger drives readiness.
        self.on_state(state_name, now_s)

        step = self._current_step()
        result = self._current_result()
        if step is None or result is None:
            return

        if self._current_started_s is None:
            normalized_state = _normalize_state(state_name)
            if self._current_index == 0 and normalized_state in {None, "game_unknown"}:
                return
            self._current_started_s = now_s

        if now_s - self._current_started_s > self._timeout_s:
            if self._timeout_advances:
                result.timeout = True
                result.status = "failed"
                result.notes = f"timeout_after_{self._timeout_s:.1f}s"
                self._mark_ended(now_s)
                self._advance()
            else:
                self._timeout_retries += 1
                result.notes = f"timeout_retry_{self._timeout_retries}_after_{self._timeout_s:.1f}s"
                self._current_started_s = now_s
                self._ready_source = None
                self._ready_count = 0
                self._trigger_ready = False
                self._state_ready = False
            return

        source = self._readiness_source(step)
        if source is None:
            self._ready_source = None
            self._ready_count = 0
            return

        if source == self._ready_source:
            self._ready_count += 1
        else:
            self._ready_source = source
            self._ready_count = 1

        if self._ready_count < self._debounce_required:
            return

        quality_error = self._quality_error(frame)
        if quality_error is not None:
            result.notes = quality_error
            self._ready_count = 0
            return

        target = self._screenshot_dir / step.screenshot_name
        if target.exists() and not self._overwrite:
            result.status = "skipped"
            result.notes = "skipped_exists"
            self._mark_ended(now_s)
            self._advance()
            return

        target.parent.mkdir(parents=True, exist_ok=True)
        if not cv2.imwrite(str(target), frame):
            result.timeout = True
            result.status = "failed"
            result.notes = f"write_failed:{target}"
            self._mark_ended(now_s)
            self._advance()
            return

        if self._start_ts is None:
            self._start_ts = now_s
        result.captured = True
        result.status = "captured"
        result.capture_time_s = now_s
        result.readiness_source = source
        result.notes = "captured"
        if step.inject_trigger and self._allow_inject:
            self._pending_inject_trigger = step.inject_trigger
        self._advance()
        if self.is_complete():
            self._mark_ended(now_s)

    def to_dict(self) -> dict[str, Any]:
        started_at_s = self._start_ts if self._start_ts is not None else self._time_fn()
        ended_at_s = self._end_ts if self._end_ts is not None else self._time_fn()
        return {
            "path_name": self.path_name,
            "started_at_s": started_at_s,
            "ended_at_s": ended_at_s,
            "complete": self.is_complete(),
            "has_failures": self.has_failures(),
            "not_updated_screenshots": self.not_updated_screenshot_names(),
            "steps": [
                {
                    "screenshot_name": r.screenshot_name,
                    "status": r.status,
                    "captured": r.captured,
                    "capture_time_s": r.capture_time_s,
                    "readiness_source": r.readiness_source,
                    "timeout": r.timeout,
                    "notes": r.notes,
                }
                for r in self._results
            ],
        }


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
