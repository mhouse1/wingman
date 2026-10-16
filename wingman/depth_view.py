"""Code Review 019 cycle 11 (operator, 2026-10-15: "shadow first"): terrain
depth from one picture, from a monocular depth model.

The dot tracker measures growth only where there is texture to follow. On
smooth or blurred ground at speed it reads "few points" exactly where the
aircraft is about to hit (2026-10-15 18:32 and 18:39, both into grass slopes
at over 1,100 KPH). A monocular depth model (Depth Anything V2 Small) orders
everything in one picture near to far, sky and smooth rock included; an
offline spike on recorded frames showed it separating sky from terrain and a
canyon's spires from each other. It gives relative depth only, so the figures
here are ranks within the picture, not metres:

- `mid_rank`: the share of the analysed view that is farther than the middle
  of it. 1.0: the aircraft flies at the nearest thing in view. 0.0: the middle
  is the farthest, open sky or a far valley.
- `nearest`: where the nearest terrain in the analysed view is, as fractions
  of the frame.

The model runs in a process of its own (`wingman.depth_worker`): this PC has no
usable CUDA device, and on the CPU a 252 px picture takes 95 to 200 ms with 4
threads, so it must share neither torch's thread pool with the OCR nor the
pursuit's thread. One I/O thread here feeds it the newest picture and reads its
answers. Shadow: logged as DEPTH lines and drawn on the HUD; nothing steers on it.
"""

from __future__ import annotations

import contextlib
import logging
import os
import subprocess
import sys
import threading
import time
from collections import Counter
from dataclasses import dataclass
from statistics import median

import cv2
import numpy as np

from .config_schema import schema_default
from .depth_worker import ERROR, OK, READY, REQUEST, RESPONSE, read_exact

logger = logging.getLogger(__name__)

# The analysed view, as the clear-path planner's (x1, y1, x2, y2 fractions of
# the frame): above the own airframe and inside the game HUD's side ladders.
VIEW_BOX = (0.20, 0.12, 0.80, 0.60)
# The middle of it, round the bore sight.
MID_BOX = (0.44, 0.36, 0.56, 0.52)


@dataclass(frozen=True)
class DepthReading:
    """The depth model's answer for one picture."""

    status: str                                 # "ok" or "error"
    picture_ts: float = 0.0
    cost_ms: float = 0.0
    near: "np.ndarray | None" = None            # the analysed view, 0 far .. 1 near
    mid_rank: "float | None" = None
    nearest: "tuple[float, float] | None" = None
    detail: str = ""


def _box(shape, frac):
    h, w = shape[:2]
    x1, y1, x2, y2 = frac
    return (int(round(x1 * w)), int(round(y1 * h)),
            max(int(round(x1 * w)) + 1, int(round(x2 * w))),
            max(int(round(y1 * h)) + 1, int(round(y2 * h))))


def depth_stats(depth: np.ndarray) -> "tuple[np.ndarray, float, tuple[float, float]]":
    """The analysed view's nearness (0 far .. 1 near, by its own 2nd and 98th
    percentiles), the middle's rank and the nearest point, from one relative
    depth map (larger is nearer) covering the whole frame."""
    depth = np.asarray(depth, np.float32)
    vx1, vy1, vx2, vy2 = _box(depth.shape, VIEW_BOX)
    mx1, my1, mx2, my2 = _box(depth.shape, MID_BOX)
    view = depth[vy1:vy2, vx1:vx2]
    lo, hi = np.percentile(view, (2.0, 98.0))
    near = np.clip((view - lo) / max(float(hi - lo), 1e-6), 0.0, 1.0).astype(np.float32)
    mid_rank = float((view < float(depth[my1:my2, mx1:mx2].mean())).mean())
    smooth = cv2.blur(view, (5, 5)) if min(view.shape) >= 5 else view
    iy, ix = np.unravel_index(int(np.argmax(smooth)), smooth.shape)
    h, w = depth.shape
    return near, mid_rank, ((vx1 + ix + 0.5) / w, (vy1 + iy + 0.5) / h)


def fmt_depth(reading: "DepthReading | None") -> str:
    """The DEPTH log line's fields."""
    if reading is None:
        return "status=none"
    if reading.status != "ok":
        return "status=%s %s" % (reading.status, reading.detail)
    return ("status=ok ms=%.0f mid_rank=%.2f nearest=(%.2f,%.2f)" % (
        reading.cost_ms, reading.mid_rank, reading.nearest[0], reading.nearest[1]))


class DepthPerception:
    """The depth model's worker process and the thread that talks to it. One
    per session: `start` it once, `submit` pictures from the pursuit, `stop` it
    at shutdown. `last` is the newest reading."""

    def __init__(self, cfg: "dict | None" = None, command: "list[str] | None" = None) -> None:
        cfg = cfg or {}

        def _d(key):
            return cfg.get(key, schema_default(f"depth_view.{key}"))

        self.enabled = bool(_d("enabled"))
        self.min_interval_s = float(_d("min_interval_s"))
        self.input_px = int(_d("input_px"))
        self._command = command or [
            sys.executable, "-m", "wingman.depth_worker", "--model", str(_d("model")),
            "--threads", str(int(_d("threads"))), "--input-px", str(self.input_px)]
        self.status = "off"          # "loading", "ready", "unavailable"
        self.last: "DepthReading | None" = None
        self._proc: "subprocess.Popen | None" = None
        self._thread: "threading.Thread | None" = None
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._lock = threading.Lock()
        self._pending: "tuple[np.ndarray, float] | None" = None
        self._sent_at = 0.0
        self._statuses: Counter = Counter()
        self._costs: "list[float]" = []
        self._dropped = 0

    def start(self) -> None:
        """Start the worker process and the thread that feeds it. The model
        loads in the background; until it is ready pictures are not taken."""
        if not self.enabled or self._thread is not None:
            return
        env = dict(os.environ, TRANSFORMERS_VERBOSITY="error", HF_HUB_DISABLE_PROGRESS_BARS="1",
                   TOKENIZERS_PARALLELISM="false")
        try:
            self._proc = subprocess.Popen(self._command, stdin=subprocess.PIPE,
                                          stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                          env=env)
        except OSError as exc:
            self.status = "unavailable"
            logger.warning("Depth view: worker did not start (%s); no depth this session", exc)
            return
        self.status = "loading"
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="depth-view", daemon=True)
        self._thread.start()

    def submit(self, frame, picture_ts: float) -> None:
        """Hand the worker this picture and return at once; a picture it has
        not taken yet is replaced by this one."""
        if self.status != "ready" or not isinstance(frame, np.ndarray) or frame.ndim != 3:
            return
        with self._lock:
            if self._pending is not None:
                self._dropped += 1
            self._pending = (frame, picture_ts)
        self._wake.set()

    def stop(self, timeout: float = 2.0) -> None:
        """Stop the thread and the worker, waiting at most `timeout` for each."""
        self._stop.set()
        self._wake.set()
        proc = self._proc
        if proc is not None and proc.stdin is not None:
            with contextlib.suppress(OSError):
                proc.stdin.close()          # the worker's end-of-input: it exits
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None
        if proc is not None:
            try:
                proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=timeout)
            self._proc = None
        if self.status in ("loading", "ready"):
            self.status = "off"

    def end(self) -> "str | None":
        """The pursuit's summary line, and the counts start again; None when
        nothing was read."""
        readings = sum(self._statuses.values())
        if not readings:
            self._dropped = 0
            return None
        statuses = " ".join(f"{k}={v}" for k, v in self._statuses.most_common())
        costs = self._costs or [0.0]
        line = (f"DEPTHVIEW SUMMARY: readings={readings} {statuses} dropped={self._dropped} "
                f"cost_median={median(costs):.0f}ms cost_max={max(costs):.0f}ms")
        self._statuses.clear()
        self._costs.clear()
        self._dropped = 0
        return line

    # -- the I/O thread ---------------------------------------------------

    def _response(self) -> "tuple[float, int, float, bytes, tuple[int, int]] | None":
        out = self._proc.stdout
        head = read_exact(out, RESPONSE.size)
        if head is None:
            return None
        picture_ts, status, cost_ms, h, w = RESPONSE.unpack(head)
        size = h * w * 2 if status == OK else (h if status == ERROR else 0)
        payload = read_exact(out, size) if size else b""
        if payload is None:
            return None
        return picture_ts, status, cost_ms, payload, (h, w)

    def _run(self) -> None:
        first = self._response()
        if first is None or first[1] != READY:
            self.status = "unavailable"
            why = ("the worker exited" if first is None
                   else first[3].decode("utf-8", "replace"))
            logger.warning("Depth view: no model (%s); no depth this session", why)
            return
        self.status = "ready"
        logger.info("Depth view: model ready (%s)", " ".join(self._command[3:]))
        while not self._stop.is_set():
            if not self._wake.wait(timeout=0.5):
                continue
            self._wake.clear()
            wait = self._sent_at + self.min_interval_s - time.monotonic()
            if wait > 0 and self._stop.wait(timeout=wait):
                break
            with self._lock:
                item, self._pending = self._pending, None
            if item is None:
                continue
            frame, picture_ts = item
            try:
                self._send(frame, picture_ts)
                answer = self._response()
            except (OSError, ValueError) as exc:
                answer = None
                logger.debug("Depth view: worker I/O failed: %s", exc)
            if answer is None:
                if not self._stop.is_set():
                    self.status = "unavailable"
                    logger.warning("Depth view: the worker stopped answering; no depth "
                                   "for the rest of the session")
                return
            self._take(answer)

    def _send(self, frame: np.ndarray, picture_ts: float) -> None:
        h, w = frame.shape[:2]
        th = max(14, self.input_px)
        tw = max(14, int(round(w * th / max(1, h))))
        rgb = cv2.cvtColor(cv2.resize(frame, (tw, th), interpolation=cv2.INTER_AREA),
                           cv2.COLOR_BGR2RGB)
        self._sent_at = time.monotonic()
        self._proc.stdin.write(REQUEST.pack(picture_ts, th, tw) + rgb.tobytes())
        self._proc.stdin.flush()

    def _take(self, answer) -> None:
        picture_ts, status, cost_ms, payload, (h, w) = answer
        if status != OK:
            reading = DepthReading("error", picture_ts, cost_ms,
                                   detail=payload.decode("utf-8", "replace")[:200])
        else:
            depth = np.frombuffer(payload, np.float16).reshape(h, w).astype(np.float32)
            near, mid_rank, nearest = depth_stats(depth)
            reading = DepthReading("ok", picture_ts, cost_ms, near, mid_rank, nearest)
            self._costs.append(cost_ms)
        self._statuses[reading.status] += 1
        self.last = reading
        logger.debug("DEPTH: %s", fmt_depth(reading))
