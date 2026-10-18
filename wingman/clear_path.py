"""Clear-path steering: terrain perception on the pursuit's own frames.

Code Review 019, step 3a. Shadow only: nothing here steers.

HLDD 001's forward-view detectors run on the main tick, once every 1.5 s, on
frames the pursuit never steers by, and the live HUD drew their outlines on a
later pursuit frame. Here the same measurement runs on the frames the pursuit
grabs for its steering, about 0.17 s apart, so what the HUD shows was measured
on the picture under it, and the planner of step 3d can read it on the cycle
it steers.

One reading per new picture, from the last two distinct pictures: the pair
reading of `terrain_loom` (its expansion point is where the aircraft is
heading, and it keeps the learned HUD mask warm) and the outlines of
`terrain_shapes`, each with the time to contact of the dots inside it. A grab
that repeats the previous picture gives no reading: two equal pictures show
no growth, and no growth must not read as far away.

The measurement runs on a worker thread of its own, one per pursuit: the
pursuit hands it each picture (`submit`) and steers on. Measured inline on
2026-10-15, its median 46 ms slowed the pursuit's steering cycle from a
median 0.165 s to 0.210 s. A picture the worker has not started when the next
arrives is replaced by it, so the work is bounded and never queues.

The detector instances are this class's own, so the main tick's and these
never share state across threads. Standard library, numpy and OpenCV only.

Step 3d adds the planner (`PathPlanner`): with the goal the pursuit is flying
at on the same picture, it says whether the straight line there is clear, and
if not, where to aim instead. Shadow: it logs `PATH:` lines and draws its
corridor on the HUD; nothing steers on it.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import Counter
from dataclasses import dataclass, replace
from statistics import median

import cv2
import numpy as np

from .config_schema import schema_default
from .suppressed import log_suppressed
from .terrain_loom import LoomReading, TerrainLoom, fmt_tau
from .terrain_shapes import _MIN_GROWTH, Shape, ShapeReading, TerrainShapes, fmt_shape

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PathView:
    """What the pursuit's perception made of one picture.

    `picture_ts` is the monotonic time the picture was first grabbed, the
    pursuit's own picture clock. `shape` and `pair` are None when the reading
    failed before either was taken.
    """

    status: str                         # "ok", or why the view has no outlines
    picture_ts: float = 0.0
    dt: float = 0.0                     # since the previous distinct picture
    shape: "ShapeReading | None" = None
    pair: "LoomReading | None" = None
    tau_warn_s: float = 8.0
    cost_ms: float = 0.0
    frame_size: "tuple[int, int]" = (0, 0)      # width, height of the picture
    plan: "PathPlan | None" = None             # step 3d, when the pursuit had a goal
    # Cycle 11: the depth view's newest reading when this view was drawn, for
    # the HUD only (depth_view.DepthReading); the planner does not read it.
    depth: object = None

    @property
    def readable(self) -> bool:
        return self.status == "ok"

    @property
    def nearest(self) -> "Shape | None":
        """The measured outline closing fastest, wherever it is in the view."""
        if self.shape is None:
            return None
        closing = [sh for sh in self.shape.shapes if sh.tau is not None]
        return min(closing, key=lambda sh: sh.tau) if closing else None

    @property
    def heading_point(self) -> "tuple[float, float] | None":
        """Where the picture expands from, when the pair reading found one: the
        estimate of where the aircraft is going (Code Review 019, CR-019-11)."""
        pair = self.pair
        if pair is None or not pair.readable or pair.tau is None:
            return None
        return pair.fixed

    def verdict(self) -> str:
        """One word or figure for the log line and the HUD banner."""
        if not self.readable:
            return f"n/a({self.status})"
        near = self.nearest
        if near is not None and near.tau < self.tau_warn_s:
            return f"closing {near.tau:.1f}s{' middle' if near.centred else ''}"
        if self.shape.blind:
            return "blind"
        return "clear"


def fmt_view(view: "PathView | None") -> str:
    """The log line's fields for one view."""
    if view is None:
        return "n/a"
    shape = view.shape
    free = None if shape is None else shape.free_frac
    near = view.nearest
    point = view.heading_point
    return (f"status={view.status} dt={view.dt:.3f}s verdict={view.verdict()} "
            f"shape={fmt_shape(shape)} outlines={0 if shape is None else len(shape.shapes)} "
            f"nearest={'-' if near is None else f'{near.tau:.1f}s' + ('/mid' if near.centred else '')} "
            f"free={'n/a' if free is None else f'{free:.2f}'} pair={fmt_tau(view.pair)} "
            f"heading={'-' if point is None else f'({point[0]:.0f},{point[1]:.0f})'} "
            f"cost={view.cost_ms:.0f}ms")


@dataclass(frozen=True)
class PathGoal:
    """What the pursuit flies at on one picture, in full-frame pixels."""

    kind: str                       # "resupply", "control point", "crown" or "target"
    x: float
    y: float
    size: "tuple[int, int] | None" = None   # the marker's box, when it has one


@dataclass(frozen=True)
class PathPlan:
    """The planner's answer for one picture (Code Review 019 step 3d, shadow).

    `aim` is where the nose would point instead of the goal and `origin` the
    flight-path point the corridor starts from, both in full-frame pixels.
    `wall_tau` is the time to contact of the measured outline the goal is
    drawn against, when it is closing: the surface the aircraft must pull up
    from once past the goal, and `wall_close` whether that is under the
    planner's `wall_warn_s`. `wall_local_tau` is the same surface's time from
    the dots round the goal alone. Reported only; no mode depends on them.
    """

    mode: str                       # "DIRECT", "DETOUR", "ESCAPE" or "BLIND"
    goal: PathGoal
    aim: "tuple[float, float]"
    origin: "tuple[float, float]"
    side: "str | None" = None       # "over", "under", "left" or "right"
    blocker_tau: "float | None" = None
    goal_clear_px: float = 0.0
    reason: str = ""
    wall_tau: "float | None" = None
    wall_close: bool = False
    wall_local_tau: "float | None" = None

    @property
    def label(self) -> str:
        return self.mode if self.side is None else f"{self.mode} {self.side.upper()}"


def fmt_plan(plan: "PathPlan | None") -> str:
    """The log line's fields for one plan."""
    if plan is None:
        return "n/a"
    goal = plan.goal
    tau = "-" if plan.blocker_tau is None else f"{plan.blocker_tau:.1f}s"
    wall = "-" if plan.wall_tau is None else f"{plan.wall_tau:.1f}s"
    local = "-" if plan.wall_local_tau is None else f"{plan.wall_local_tau:.1f}s"
    size = "-" if goal.size is None else f"{goal.size[0]}x{goal.size[1]}"
    return (f"mode={plan.label.replace(' ', '_')} goal={goal.kind.replace(' ', '_')} "
            f"at=({goal.x:.0f},{goal.y:.0f}) size={size} "
            f"aim=({plan.aim[0]:.0f},{plan.aim[1]:.0f}) "
            f"origin=({plan.origin[0]:.0f},{plan.origin[1]:.0f}) "
            f"clear={'inf' if plan.goal_clear_px == float('inf') else f'{plan.goal_clear_px:.0f}px'} "
            f"blocker={tau} wall={wall} wall_local={local} "
            f"why={plan.reason.replace(' ', '_')}")


def fmt_own(own) -> str:
    """The aircraft's airspeed (KPH) and flight-path angle (degrees) at the
    picture, for the PATH line; `-` for what is not known."""
    speed, path = own if own is not None else (None, None)
    return "spd=%s path=%s" % ("-" if speed is None else "%.0f" % speed,
                               "-" if path is None else "%+.0f" % path)


def _side(dx: float, dy: float) -> str:
    """Which way an aim point lies from the goal, as the pilot would say it."""
    if -dy >= abs(dx):
        return "over"
    if dy >= abs(dx):
        return "under"
    return "left" if dx < 0 else "right"


class PathPlanner:
    """Where to point the nose so the line to the goal stays in free air.

    The reactive planner small autonomous vehicles use with one sensor and no
    map (follow-the-gap): free air near the goal, with a margin, reached by a
    straight line from where the aircraft is going that crosses nothing
    closing. Image space only; time to contact stands in for distance.

    - DIRECT: the goal has `clear_px` of free air round it and the line to it
      crosses nothing closing. The aim is the goal, which is today's steering.
    - DETOUR: otherwise, the nearest point to the goal with that much free
      air and a clear line, kept on the same side for `hold_s`.
    - ESCAPE: the flight path is on something closing in under
      `escape_tau_s`, or no point qualifies: the free air nearest the flight
      path, up before down.
    - BLIND: the view is unreadable, or the goal is outside the analysed view.

    An outline is an obstacle when it closes within the view's `tau_warn_s`
    and, when the goal is drawn against a measured outline (the surface it
    sits by), sooner than `nearer_frac` of that surface's time: one that
    closes no sooner is at the goal's depth or behind it. Outlines with too
    few dots to measure are not obstacles but unknowns: a goal against one,
    or a line through one, is BLIND, never a detour. Measured on 2026-10-15:
    treating them as obstacles raised an alarm on 82% of objective approaches,
    6 of 7 successful pickups among them (Code Review 019).

    Each plan also carries the wall: how soon the measured surface behind the
    goal is closing. Before 62 resupply climb-outs on 2026-10-15 it was a
    median 2.2 s ahead of the 26 that ended in a death within 10 s and 4.8 s
    ahead of the 36 that did not. A view where it is under `wall_warn_s` is
    logged and counted. One fit over a whole outline is set by its
    fastest-growing part, which on a canyon map was the ground rushing past
    under the aircraft (2026-10-15 16:44, a pickup survived at 1.5 s), so the
    plan also carries the growth of the dots within `wall_radius_px` of the
    goal, on its own outline, alone.
    Not thread-safe: the perception's worker is its only caller.
    """

    _GRID = 8           # spacing of the candidate aim points, tracking-scale px
    _SAMPLES = 48       # points tested along each line

    def __init__(self, cfg: "dict | None" = None) -> None:
        cfg = cfg or {}

        def _d(key):
            return cfg.get(key, schema_default(f"clear_path.planner.{key}"))

        self.clear_px = float(_d("clear_px"))
        self.hold_s = float(_d("hold_s"))
        self.escape_tau_s = float(_d("escape_tau_s"))
        self.nearer_frac = float(_d("nearer_frac"))
        self.wall_warn_s = float(_d("wall_warn_s"))
        self.wall_radius_px = float(_d("wall_radius_px"))
        self.wall_min_dots = int(_d("wall_min_dots"))
        self._side: "str | None" = None
        self._side_ts = -1e9

    @staticmethod
    def closing(shape: Shape, horizon_s: float) -> bool:
        """Measured, and closing within the horizon."""
        return shape.tau is not None and shape.tau < horizon_s

    @staticmethod
    def _clearance(mask: np.ndarray) -> np.ndarray:
        """Distance from each pixel to the nearest pixel of `mask`; inf without one."""
        if not mask.any():
            return np.full(mask.shape, np.inf, np.float32)
        return cv2.distanceTransform((~mask).astype(np.uint8), cv2.DIST_L2, 5)

    def _hits(self, mask: np.ndarray, start, ends: np.ndarray) -> np.ndarray:
        """For each end point, whether the line from `start` crosses `mask`."""
        t = np.linspace(0.0, 1.0, self._SAMPLES, dtype=np.float32)
        xs = start[0] + (ends[:, :1] - start[0]) * t
        ys = start[1] + (ends[:, 1:2] - start[1]) * t
        xs = np.clip(np.rint(xs).astype(np.int32), 0, mask.shape[1] - 1)
        ys = np.clip(np.rint(ys).astype(np.int32), 0, mask.shape[0] - 1)
        return mask[ys, xs].any(axis=1)

    def plan(self, view: "PathView", goal: PathGoal, now: float) -> PathPlan:
        plan = self._plan(view, goal, now)
        changes = {}
        if plan.wall_tau is not None and plan.wall_tau < self.wall_warn_s:
            changes["wall_close"] = True
        local = self._local_tau(view, goal)
        if local is not None:
            changes["wall_local_tau"] = local
        return replace(plan, **changes) if changes else plan

    def _local_tau(self, view: "PathView", goal: PathGoal) -> "float | None":
        """Time to contact of the surface round the goal: one fit to the dots
        within `wall_radius_px` of it on the outline it is drawn against. None
        when the goal is against nothing, there are too few such dots, or they
        are not growing."""
        reading = None if view is None else view.shape
        if reading is None or not view.readable or view.dt <= 0.0:
            return None
        after, before = reading.points, reading.points_before
        if len(after) < self.wall_min_dots or len(before) != len(after):
            return None
        surface = next((sh for sh in reading.shapes if cv2.pointPolygonTest(
            sh.outline.reshape(-1, 1, 2).astype(np.float32),
            (float(goal.x), float(goal.y)), False) >= 0), None)
        if surface is None:
            return None
        near = np.hypot(after[:, 0] - goal.x, after[:, 1] - goal.y) <= self.wall_radius_px
        poly = surface.outline.reshape(-1, 1, 2).astype(np.float32)
        near[near] = [cv2.pointPolygonTest(poly, (float(x), float(y)), False) >= 0
                      for x, y in after[near]]
        if int(near.sum()) < self.wall_min_dots:
            return None
        fit, inl = cv2.estimateAffinePartial2D(
            before[near], after[near], method=cv2.RANSAC,
            ransacReprojThreshold=3.0 / max(reading.view_scale, 1e-6))
        if fit is None or inl is None or int(inl.sum()) < self.wall_min_dots:
            return None
        scale = float(np.hypot(fit[0, 0], fit[1, 0]))
        return view.dt / (scale - 1.0) if scale > 1.0 + _MIN_GROWTH else None

    def _plan(self, view: "PathView", goal: PathGoal, now: float) -> PathPlan:
        width, height = view.frame_size if view is not None else (0, 0)
        centre = (width / 2.0, height / 2.0)
        reading = None if view is None else view.shape
        if reading is None or not view.readable or reading.texture is None:
            return PathPlan("BLIND", goal, (goal.x, goal.y), centre,
                            reason=f"view {'missing' if view is None else view.status}")
        tex = reading.texture
        th, tw = tex.shape[:2]
        s = reading.view_scale or 1.0
        ox, oy = reading.view_origin

        def in_view(x, y):
            return (x - ox) * s, (y - oy) * s

        def in_frame(x, y):
            return x / s + ox, y / s + oy

        hx, hy = in_view(*(view.heading_point or centre))
        if not (0 <= hx < tw and 0 <= hy < th):
            hx, hy = in_view(*centre)      # no usable expansion point: the bore sight
        origin = in_frame(hx, hy)
        gx, gy = in_view(goal.x, goal.y)
        if not (0 <= gx < tw and 0 <= gy < th) or not (0 <= hx < tw and 0 <= hy < th):
            return PathPlan("BLIND", goal, (goal.x, goal.y), origin,
                            reason="goal outside the analysed view")
        labels = np.zeros((th, tw), np.int32)       # every outline, by its index
        shapes: "dict[int, Shape]" = {}
        for idx, sh in enumerate(reading.shapes, start=1):
            pts = np.rint((sh.outline - np.float32([ox, oy])) * s).astype(np.int32)
            cv2.fillPoly(labels, [pts.reshape(-1, 1, 2)], idx)
            shapes[idx] = sh
        horizon = view.tau_warn_s
        closing = {i for i, sh in shapes.items() if self.closing(sh, horizon)}
        start = int(labels[int(hy), int(hx)])
        need = self.clear_px * s
        # The outline the goal is drawn against is the surface it sits by: the
        # wall to pull up from once past the goal, when it is closing.
        goal_id = int(labels[int(gy), int(gx)])
        goal_sh = shapes.get(goal_id)
        wall = goal_sh.tau if goal_id in closing else None
        # Flying at something closing fast: get out first, whatever the goal.
        if start in closing and shapes[start].tau < self.escape_tau_s:
            danger = np.isin(labels, sorted(closing))
            return self._escape(self._clearance(danger), need, (hx, hy), goal, origin,
                                in_frame, shapes[start].tau,
                                "flying at terrain closing in %.1f s" % shapes[start].tau, wall)
        # Not measured: nothing here can say whether the goal is in front of it.
        if goal_sh is not None and not goal_sh.measured:
            return PathPlan("BLIND", goal, (goal.x, goal.y), origin,
                            reason="the goal is against terrain it cannot measure")
        surface_tau = None if goal_sh is None else goal_sh.tau
        # A closing outline counts only when it is measurably nearer than that
        # surface: what closes no sooner is at the goal's depth or behind it.
        nearer = {i for i in closing if i != goal_id
                  and (surface_tau is None or shapes[i].tau < self.nearer_frac * surface_tau)}
        # What the aircraft is flying at, but not about to hit, is being left: it
        # does not block a line that starts inside it, but its edge still counts
        # against the room round the goal.
        line_block = np.isin(labels, sorted(nearer - {start}))
        room_block = np.isin(labels, sorted(nearer))
        unknown = np.isin(labels, sorted(i for i, sh in shapes.items()
                                         if not sh.measured and i not in (start, goal_id)))
        goal_pt = np.float32([[gx, gy]])
        crossed = bool(self._hits(line_block, (hx, hy), goal_pt)[0])
        dist = self._clearance(room_block)
        goal_clear = float(dist[int(gy), int(gx)]) / s
        blocker = self._blocker(labels, (hx, hy), (gx, gy), shapes, nearer, goal_pt, need)
        if goal_clear >= self.clear_px and not crossed:
            self._side = None
            if bool(self._hits(unknown, (hx, hy), goal_pt)[0]):
                return PathPlan("BLIND", goal, (goal.x, goal.y), origin,
                                goal_clear_px=goal_clear,
                                reason="terrain it cannot measure on the line to the goal",
                                wall_tau=wall)
            return PathPlan("DIRECT", goal, (goal.x, goal.y), origin,
                            goal_clear_px=goal_clear, reason="line to the goal is clear",
                            wall_tau=wall)
        grid = dist[::self._GRID, ::self._GRID]
        ys, xs = np.nonzero((grid >= need) & ~unknown[::self._GRID, ::self._GRID])
        cand = np.stack([xs * self._GRID, ys * self._GRID], axis=1).astype(np.float32)
        if len(cand):
            cand = cand[~self._hits(line_block | unknown, (hx, hy), cand)]
        if not len(cand):
            return self._escape(dist, need, (hx, hy), goal, origin, in_frame, blocker,
                                "no clear corridor near the goal", wall)
        cost = np.hypot(cand[:, 0] - gx, cand[:, 1] - gy)
        if self._side is not None and now - self._side_ts < self.hold_s:
            same = np.array([_side(cx - gx, cy - gy) == self._side for cx, cy in cand])
            cost = np.where(same, cost, cost * 1.5)
        ax, ay = (float(v) for v in cand[int(np.argmin(cost))])
        side = _side(ax - gx, ay - gy)
        if side != self._side:
            self._side, self._side_ts = side, now
        return PathPlan("DETOUR", goal, in_frame(ax, ay), origin, side=side,
                        blocker_tau=blocker, goal_clear_px=goal_clear,
                        reason="terrain on the line to the goal" if crossed
                        else "the goal is against nearer terrain", wall_tau=wall)

    def _blocker(self, labels, start_pt, goal_xy, shapes, nearer, goal_pt, need) -> "float | None":
        """Time to contact of the fastest-closing nearer outline, on the line to
        the goal or within the room round it."""
        t = np.linspace(0.0, 1.0, 64)
        xs = np.clip(np.rint(start_pt[0] + (goal_xy[0] - start_pt[0]) * t).astype(int),
                     0, labels.shape[1] - 1)
        ys = np.clip(np.rint(start_pt[1] + (goal_xy[1] - start_pt[1]) * t).astype(int),
                     0, labels.shape[0] - 1)
        ids = {int(v) for v in labels[ys, xs]}
        r = int(max(1, need))
        gx, gy = int(goal_pt[0, 0]), int(goal_pt[0, 1])
        ids |= {int(v) for v in np.unique(labels[max(0, gy - r):gy + r + 1,
                                                 max(0, gx - r):gx + r + 1])}
        found = [shapes[i].tau for i in ids if i in nearer]
        return min(found) if found else None

    def _escape(self, dist, need, start_pt, goal, origin, in_frame, tau, reason,
                wall=None) -> PathPlan:
        """The free air nearest the flight path, up before down; straight up
        from it when the view has none."""
        hx, hy = start_pt
        ys, xs = np.nonzero(dist >= need)
        if len(xs):
            dx, dy = xs - hx, ys - hy
            cost = np.hypot(dx, dy) + 0.5 * np.maximum(dy, 0)
            k = int(np.argmin(cost))
            ax, ay = float(xs[k]), float(ys[k])
        else:
            ax, ay = hx, max(0.0, hy - dist.shape[0] / 4.0)
        self._side = None
        return PathPlan("ESCAPE", goal, in_frame(ax, ay), origin,
                        side=_side(ax - hx, ay - hy), blocker_tau=tau, reason=reason,
                        wall_tau=wall)


class PathPerception:
    """Terrain perception on the pursuit's frames. One per session.

    `observe` measures one picture where it is called and is not thread-safe.
    In flight only the worker calls it: the pursuit `start`s the worker,
    `submit`s each picture and `stop`s it when the pursuit ends."""

    def __init__(self, loom_cfg: "dict | None" = None, cfg: "dict | None" = None) -> None:
        cfg = cfg or {}
        self.enabled = bool(cfg.get("enabled", schema_default("clear_path.enabled")))
        loom_cfg = dict(loom_cfg or {})
        shapes_cfg = dict(loom_cfg.get("shapes") or {})
        # The main tick's switches for these detectors do not apply here:
        # this class has its own instances and its own switch.
        loom_cfg["enabled"] = True
        shapes_cfg["enabled"] = True
        loom_cfg["shapes"] = shapes_cfg
        self._loom = TerrainLoom(loom_cfg)
        self._shapes = TerrainShapes(self._loom, shapes_cfg)
        self._planner = PathPlanner(cfg.get("planner"))
        # Code Review 019 cycle 25: each ESCAPE plan is noted here.
        self.agreement = None
        self._plan_modes: Counter = Counter()
        self._plan_label: "str | None" = None
        self._plan_logged = -1e9
        self._walls = 0             # planned views with the wall under wall_warn_s
        self._wall_on = False
        self._wall_logged = -1e9
        self._prev: "tuple[np.ndarray, float] | None" = None
        self.last: "PathView | None" = None
        self._statuses: Counter = Counter()
        self._costs: "list[float]" = []
        self._repeats = 0
        self._no_health = 0     # pictures not taken: health not being read
        # The worker: one picture waiting at most, the newest.
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._pending = None        # (frame, picture_ts, on_view)
        self._thread: "threading.Thread | None" = None
        self._dropped = 0

    @property
    def tau_warn_s(self) -> float:
        return self._shapes.tau_warn_s

    def reset(self) -> None:
        """Forget the previous picture, so a new pursuit does not pair its first
        frame with the last frame of the one before. The learned HUD mask is
        kept: the HUD is the same."""
        self._prev = None
        self.last = None

    def observe(self, frame, picture_ts: float) -> "PathView | None":
        """The view of this picture; None when the picture repeats the one
        before, when it is the first, or when this is switched off."""
        if not self.enabled or not isinstance(frame, np.ndarray) or frame.ndim != 3:
            return None
        prev = self._prev
        if prev is None or prev[0].shape != frame.shape:
            self._prev = (frame, picture_ts)
            return None
        prev_frame, prev_ts = prev
        dt = picture_ts - prev_ts
        if dt <= 0.0 or self._loom.same_view(prev_frame, frame):
            self._repeats += 1
            return None
        self._prev = (frame, picture_ts)
        started = time.perf_counter()
        try:
            pair = self._loom.measure(prev_frame, frame, dt)
            shape = self._shapes.measure([prev_frame, frame], dt, dt)
        except Exception as exc:
            log_suppressed(logger, "clear_path_perception", exc)
            view = PathView(status="error", picture_ts=picture_ts, dt=dt,
                            tau_warn_s=self.tau_warn_s,
                            frame_size=(frame.shape[1], frame.shape[0]))
        else:
            view = PathView(status=shape.status, picture_ts=picture_ts, dt=dt,
                            shape=shape, pair=pair, tau_warn_s=self.tau_warn_s,
                            cost_ms=(time.perf_counter() - started) * 1000.0,
                            frame_size=(frame.shape[1], frame.shape[0]))
        self.last = view
        self._statuses[view.status] += 1
        self._costs.append(view.cost_ms)
        logger.debug("PATHVIEW: %s", fmt_view(view))
        return view

    def start(self) -> None:
        """Start the worker, for one pursuit. A no-op while one is running."""
        if not self.enabled or (self._thread is not None and self._thread.is_alive()):
            return
        self._stop.clear()
        self._wake.clear()
        with self._lock:
            self._pending = None
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="PathPerception")
        self._thread.start()

    def submit(self, frame, picture_ts: float, on_view=None,
               goal: "PathGoal | None" = None, own=None) -> None:
        """Hand the worker this picture and return at once. `on_view(frame,
        view)` is called on the worker thread when the picture gives a view;
        with a `goal`, the view carries the planner's answer for it, and `own`
        (airspeed KPH, flight-path angle) goes on its log line. A picture the
        worker has not started yet is replaced by this one."""
        if not self.enabled or self._thread is None:
            return
        with self._lock:
            if self._pending is not None:
                self._dropped += 1
            self._pending = (frame, picture_ts, on_view, goal, own)
        self._wake.set()

    def plan(self, view: PathView, goal: "PathGoal | None", own=None) -> PathView:
        """The view with the planner's answer for `goal` on it, logged with
        the aircraft's `own` airspeed and flight-path angle."""
        if goal is None:
            self._plan_label = None
            self._wall_on = False
            return view
        plan = self._planner.plan(view, goal, view.picture_ts)
        self._plan_modes[plan.mode] += 1
        if plan.mode.startswith("ESCAPE") and self.agreement is not None:
            self.agreement.note("ESCAPE")
        logger.debug("PATH: %s %s", fmt_plan(plan), fmt_own(own))
        label = f"{goal.kind} {plan.label}"
        if label != self._plan_label and view.picture_ts - self._plan_logged >= 2.0:
            logger.info("PATH[shadow]: %s for the %s (%s)%s", plan.label, goal.kind,
                        plan.reason, "" if plan.blocker_tau is None
                        else ", blocker closing in %.1f s" % plan.blocker_tau)
            self._plan_logged = view.picture_ts
        self._plan_label = label
        if plan.wall_close:
            self._walls += 1
            if not self._wall_on and view.picture_ts - self._wall_logged >= 2.0:
                logger.info("PATH[shadow]: wall behind the %s closing in %.1f s, "
                            "round the goal %s", goal.kind, plan.wall_tau,
                            "-" if plan.wall_local_tau is None
                            else "%.1f s" % plan.wall_local_tau)
                self._wall_logged = view.picture_ts
        self._wall_on = plan.wall_close
        return replace(view, plan=plan)

    def skip_no_health(self) -> None:
        """Count a picture the pursuit did not hand over because health is not
        being read: the battle HUD is gone (a death, the round-end card)."""
        self._no_health += 1

    def stop(self, timeout: float = 1.0) -> None:
        """Stop the worker and wait for it, at most `timeout` seconds."""
        self._stop.set()
        self._wake.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=timeout)
        self._thread = None

    def _run(self) -> None:
        while not self._stop.is_set():
            if not self._wake.wait(timeout=0.5):
                continue
            self._wake.clear()
            with self._lock:
                item, self._pending = self._pending, None
            if item is None or self._stop.is_set():
                continue
            frame, picture_ts, on_view, goal, own = item
            view = self.observe(frame, picture_ts)
            if view is not None and goal is not None:
                try:
                    view = self.plan(view, goal, own)
                except Exception as exc:
                    log_suppressed(logger, "clear_path_plan", exc)
                self.last = view
            if view is not None and on_view is not None:
                try:
                    on_view(frame, view)
                except Exception as exc:
                    log_suppressed(logger, "clear_path_on_view", exc)

    def end(self) -> "str | None":
        """The pursuit's summary line, and the counts start again; None when
        nothing was read."""
        readings = sum(self._statuses.values())
        if not readings:
            self._repeats = 0
            self._dropped = 0
            self._no_health = 0
            return None
        statuses = " ".join(f"{k}={v}" for k, v in self._statuses.most_common())
        plans = " ".join(f"{k}={v}" for k, v in self._plan_modes.most_common()) or "none"
        line = (f"PATHVIEW SUMMARY: readings={readings} {statuses} "
                f"repeats={self._repeats} dropped={self._dropped} no_health={self._no_health} "
                f"cost_median={median(self._costs):.0f}ms cost_max={max(self._costs):.0f}ms "
                f"plans: {plans} wall_close={self._walls}")
        self._plan_modes.clear()
        self._plan_label = None
        self._walls = 0
        self._wall_on = False
        self._statuses.clear()
        self._costs.clear()
        self._repeats = 0
        self._dropped = 0
        self._no_health = 0
        return line
