"""Design 012 — paired session video + behavior tree trace.

Both are opt-in (`--record-session`, the Makefile's `v` argument) and share
one session id (`PerformanceTracker.run_id`) so a specific elapsed-seconds
mark in the video lines up directly with a line in the trace: "the trace
says a transition at t=1842.3" means "look at 1842.3s into the video."

Deliberately not the ADR 044/045 replay-gate machinery (`wingman/replay.py`)
— that captures screenshots to feed BACK into wingman for testing. This
records what a real session actually showed, for a human or Claude to
review afterward.
"""

from __future__ import annotations

import contextlib
import json
import logging
import threading
import time
from pathlib import Path

import cv2

from .capture import Capture

logger = logging.getLogger(__name__)


class BtTraceWriter:
    """One JSON object per tactic-selection change, appended and flushed
    immediately — a crash loses at most the in-flight line, not the whole
    session (unlike a single JSON blob written at shutdown)."""

    def __init__(self, path: "str | Path", clock=time.time, start=None):
        self._clock = clock
        self._start = self._clock() if start is None else start
        self._path = Path(path)
        # Held open for the object's whole lifetime (one line per transition,
        # flushed immediately) — not a single scoped read/write, so a `with`
        # block doesn't fit; closed explicitly via close().
        self._fh = open(self._path, "w", encoding="utf-8")  # noqa: SIM115

    def record(self, from_tactic: str, to_tactic: str, statuses: dict[str, str]) -> None:
        line = json.dumps({
            "t": round(self._clock() - self._start, 3),
            "from": from_tactic,
            "to": to_tactic,
            "statuses": statuses,
        })
        try:
            self._fh.write(line + "\n")
            self._fh.flush()
        except OSError as e:
            logger.warning("BtTraceWriter: write failed (%s) — trace may be incomplete", e)

    def close(self) -> None:
        with contextlib.suppress(OSError):
            self._fh.close()


class VideoRecorder:
    """Stoppable daemon thread (CLAUDE.md convention) recording the same
    view `Capture` gives the main loop, at a low fps — this is a debugging
    aid reviewed at tactic-selection cadence, not a cinematic capture, and
    keeping the rate low bounds file size and CPU cost over a multi-hour
    session."""

    def __init__(self, region, monitor_index, game_window_offset, display,
                 out_path: "str | Path", fps: float = 2.0, scale: float = 0.5,
                 capture_factory=None):
        self._region = region
        self._monitor_index = monitor_index
        self._game_window_offset = game_window_offset
        self._display = display
        self._out_path = str(out_path)
        self._fps = float(fps)
        self._scale = float(scale)
        # Injectable so tests can stand in a fake Capture — real Capture
        # wraps mss, which needs a live X11/Windows session (see
        # _make_capture's docstring for why it can't just be constructed
        # up front and handed in instead).
        self._capture_factory = capture_factory or self._default_capture
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="VideoRecorder", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        self._thread.join(timeout=timeout)

    def _default_capture(self):
        return Capture(self._region, self._monitor_index,
                       game_window_offset=self._game_window_offset,
                       display=self._display)

    def _make_capture(self):
        # mss is thread-local (CLAUDE.md) — this MUST be constructed on the
        # recorder's own thread, never passed in from the caller's.
        return self._capture_factory()

    def _run(self) -> None:
        cap = self._make_capture()
        writer = None
        try:
            while not self._stop.wait(timeout=1.0 / self._fps):
                try:
                    frame = cap.get_frame()
                    if frame is None:
                        continue
                    if self._scale != 1.0:
                        frame = cv2.resize(frame, None, fx=self._scale, fy=self._scale)
                    if writer is None:
                        h, w = frame.shape[:2]
                        writer = cv2.VideoWriter(
                            self._out_path, cv2.VideoWriter_fourcc(*"mp4v"),
                            self._fps, (w, h))
                        if not writer.isOpened():
                            logger.warning("VideoRecorder: writer failed to open for %s "
                                          "— recording disabled for this session",
                                          self._out_path)
                            return
                    writer.write(frame)
                except Exception as e:
                    # An instrumentation failure must never take down the
                    # main loop — this thread is fire-and-forget from the
                    # caller's perspective.
                    logger.warning("VideoRecorder: frame capture failed: %s", e)
        finally:
            if writer is not None:
                writer.release()
            if hasattr(cap, "cleanup"):
                cap.cleanup()
