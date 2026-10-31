"""Design 012 — paired session video + behavior tree trace.

Both are opt-in (`--record-session`, the Makefile's `v` argument) and share
one session id (`PerformanceTracker.run_id`).

The video's own timeline is not the clock. Until 2026-10-19 each frame waited
a full period after the previous one was written, so every grab stretched the
period: the 2026-10-04 videos ran about 5% slow (a death the trace put at
10,050.9 s was on screen 9,571 s into the video, 8 minutes early), and an
elapsed-seconds mark in the video did not line up with the trace. Frames are
now paced on an absolute schedule, and each one's wall-clock time goes to
`<video>.frames.csv` (`frame,time`), which lines the video up with the wingman
log to the frame (Research 017 step 3 labels frames by the log's crash times).

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


def next_frame_due(due: float, now: float, period: float) -> float:
    """When the frame after the one due at `due` is due, on an absolute
    schedule: one period on, so the time a grab takes does not stretch it.
    A recorder that has fallen a whole period behind (a slow grab) restarts
    the schedule from `now` instead of bursting frames to catch up."""
    nxt = due + period
    return nxt if nxt > now else now + period


def frames_path(out_path: "str | Path") -> Path:
    """The per-frame times file beside a session video."""
    return Path(str(out_path)).with_suffix(".frames.csv")


class VideoRecorder:
    """Stoppable daemon thread (CLAUDE.md convention) recording the same
    view `Capture` gives the main loop, at a low fps — this is a debugging
    aid reviewed at tactic-selection cadence, not a cinematic capture, and
    keeping the rate low bounds file size and CPU cost over a multi-hour
    session. Each written frame's wall-clock time goes to `frames_path`."""

    def __init__(self, region, monitor_index, game_window_offset, display,
                 out_path: "str | Path", fps: float = 2.0, scale: float = 0.5,
                 capture_factory=None, clock=time.time):
        self._region = region
        self._monitor_index = monitor_index
        self._game_window_offset = game_window_offset
        self._display = display
        self._out_path = str(out_path)
        self._fps = float(fps)
        self._scale = float(scale)
        self._clock = clock
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
        times = None
        written = 0
        period = 1.0 / self._fps
        due = self._clock() + period
        try:
            while not self._stop.wait(timeout=max(0.0, due - self._clock())):
                due = next_frame_due(due, self._clock(), period)
                try:
                    frame = cap.get_frame()
                    stamp = self._clock()
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
                        # Held open for the thread's life, like BtTraceWriter's
                        # file; closed in the finally below.
                        times = open(frames_path(self._out_path), "w",  # noqa: SIM115
                                     encoding="utf-8")
                        times.write("frame,time\n")
                    writer.write(frame)
                    times.write(f"{written},{stamp:.3f}\n")
                    written += 1
                    if written % 120 == 0:
                        times.flush()
                except Exception as e:
                    # An instrumentation failure must never take down the
                    # main loop — this thread is fire-and-forget from the
                    # caller's perspective.
                    logger.warning("VideoRecorder: frame capture failed: %s", e)
        finally:
            if writer is not None:
                writer.release()
            if times is not None:
                with contextlib.suppress(OSError):
                    times.close()
            if hasattr(cap, "cleanup"):
                cap.cleanup()
