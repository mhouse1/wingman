"""Design 017 Phase 1: the free-airspace record of an arena, built offline from flown samples.

A sample is one tick's position fix (Design 020's ``TERRAIN:`` line) with the HUD altitude read
around it: an aircraft was at this place, at this height, alive. Terrain is a surface, so air at
or above the lowest altitude flown through a cell is free (Design 017, "What the record claims").

The record is a grid over the arena, ``CELL_M`` to a cell (64 m, a starting value). Each cell
holds:

- ``floor``: the lowest altitude any accepted pass reached in it;
- ``floor2``: the second pass's lowest altitude. Two separate lives agree the ground is below
  it, so this is the floor the record trusts (Design 017: "trusted once two separate passes
  agree");
- ``passes``: how many separate lives flew through it, and ``near_floor``, how many of those
  came within ``NEAR_FLOOR_M`` of ``floor``;
- ``last_t``: when it was last flown (epoch seconds);
- ``crash_alt`` and ``crash_n``: the highest crash altitude placed in it, and how many.

A pass is one life's visit: two visits by the same life are not independent evidence.

Acceptance rules applied here (the log-side rules, a real fix, a clean altitude and not the
last 8 s before a death, are applied by ``scripts/airspace-record.py``):

- a point is dropped when its altitude is more than ``INSIDE_GROUND_M`` below the mesh's top
  surface under it, unless it lies in an overhang's free gap (Design 020's ``inside_ground``);
- between two samples of the same life at most ``MAX_JOIN_S`` apart, the cells on the
  straight line between them are filled at the higher of the two altitudes, never an
  interpolated lower one, and only when the step is flyable (``MAX_SPEED_MPS``);
- a sample that both its neighbours reach only faster than ``MAX_SPEED_MPS`` is a fix off by
  kilometres, and is dropped.

Built offline; wingman does not write the record in flight. Standard library and numpy only.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field

import numpy as np

CELL_M = 64.0               # two minimap pixels (Design 017; Open question 1)
HALF_SPAN_M = 10240.0       # the minimap texture's 10 km, rounded out to whole cells
NEAR_FLOOR_M = 50.0         # a pass "reached the floor" within this
INSIDE_GROUND_M = 30.0      # terrain_service's inside_ground tolerance
MAX_JOIN_S = 2.0            # fill between fixes no further apart than this
MAX_SPEED_MPS = 700.0       # terrain_service's top plausible ground speed
FILL_STEP_FRACTION = 0.5    # fill points every half a cell


@dataclass(frozen=True)
class Sample:
    """One accepted fix: an aircraft was here, at this height, alive."""
    t: float
    x: float
    z: float
    alt: float
    life: int
    manual: bool = False


@dataclass(frozen=True)
class Crash:
    """Where a death the kill card calls a crash happened, as near as the last fix places it."""
    t: float
    x: float
    z: float
    alt: float
    fix_age_s: float


@dataclass(frozen=True)
class Rearm:
    """The last fix before a re-arm: a point near a resupply ring. `manual`: flown by hand and found
    from the missile count, not confirmed by wingman's own pursuit, so a rack switch can pass for
    one (scripts/airspace-record.py, manual_rearms)."""
    t: float
    x: float
    z: float
    alt: float
    manual: bool = False


class HeightMap:
    """Design 020's height map: the highest collision surface per cell, and overhang gaps.

    Rows run from the south (z = origin) up, columns from the west (x = origin); Unity
    metres, y up. ``floor``/``ceil`` are NaN except under an overhang, where the air between
    them is free."""

    def __init__(self, top: np.ndarray, cell: float, origin: float,
                 floor: "np.ndarray | None" = None, ceil: "np.ndarray | None" = None,
                 water: "float | None" = None):
        self.top = np.asarray(top, dtype=np.float32)
        self.cell = float(cell)
        self.origin = float(origin)
        n = self.top.shape[0]
        self.floor = np.full_like(self.top, np.nan) if floor is None else np.asarray(floor, np.float32)
        self.ceil = np.full_like(self.top, np.nan) if ceil is None else np.asarray(ceil, np.float32)
        self.water = float(self.top.min()) if water is None else float(water)
        self.n = n

    @classmethod
    def load(cls, path: str) -> "HeightMap":
        hm = np.load(path)
        water = float(hm["water"]) if "water" in hm.files else None
        floor = hm["floor"] if "floor" in hm.files else None
        ceil = hm["ceil"] if "ceil" in hm.files else None
        return cls(hm["top"], float(hm["cell"]), float(hm["origin"]), floor, ceil, water)

    def _index(self, x, z):
        x = np.asarray(x, dtype=np.float64)
        z = np.asarray(z, dtype=np.float64)
        i = np.floor((z - self.origin) / self.cell).astype(np.int64)
        j = np.floor((x - self.origin) / self.cell).astype(np.int64)
        on = (i >= 0) & (i < self.n) & (j >= 0) & (j < self.n)
        return np.where(on, i, 0), np.where(on, j, 0), on

    def ground(self, x, z) -> np.ndarray:
        """The top surface under each point; off the map, the water plane."""
        i, j, on = self._index(x, z)
        return np.where(on, self.top[i, j], self.water)

    def inside(self, x, z, alt, tol: float = INSIDE_GROUND_M) -> np.ndarray:
        """Whether each point is inside the ground: more than `tol` under the top surface and
        not in an overhang's free gap."""
        i, j, on = self._index(x, z)
        alt = np.asarray(alt, dtype=np.float64)
        top = np.where(on, self.top[i, j], self.water)
        lo = np.where(on, self.floor[i, j], np.nan)
        hi = np.where(on, self.ceil[i, j], np.nan)
        with np.errstate(invalid="ignore"):
            in_gap = np.isfinite(hi) & (alt >= lo - tol) & (alt <= hi)
        return (alt < top - tol) & ~in_gap

    def per_cell(self, cell: float, origin: float, n: int) -> "tuple[np.ndarray, np.ndarray, np.ndarray]":
        """The highest and lowest top surface, and whether any overhang lies, in each cell of
        a coarser grid (cell, origin, n): what the mesh holds inside one record cell."""
        centres = self.origin + (np.arange(self.n) + 0.5) * self.cell
        idx = np.floor((centres - origin) / cell).astype(np.int64)
        keep = (idx >= 0) & (idx < n)
        rows = np.flatnonzero(keep)
        gi = idx[keep]
        top = self.top[np.ix_(rows, rows)]
        flat = (gi[:, None] * n + gi[None, :]).ravel()
        gmax = np.full(n * n, -np.inf, dtype=np.float32)
        gmin = np.full(n * n, np.inf, dtype=np.float32)
        np.maximum.at(gmax, flat, top.ravel())
        np.minimum.at(gmin, flat, top.ravel())
        over = np.zeros(n * n, dtype=bool)
        np.logical_or.at(over, flat, np.isfinite(self.ceil[np.ix_(rows, rows)]).ravel())
        gmax[~np.isfinite(gmax)] = self.water
        gmin[~np.isfinite(gmin)] = self.water
        return gmax.reshape(n, n), gmin.reshape(n, n), over.reshape(n, n)


@dataclass
class AirspaceRecord:
    """The free-airspace record of one arena (see the module docstring)."""
    cell: float
    origin: float
    floor: np.ndarray
    floor2: np.ndarray
    passes: np.ndarray
    near_floor: np.ndarray
    last_t: np.ndarray
    crash_alt: np.ndarray
    crash_n: np.ndarray
    overhang: np.ndarray
    crashes: np.ndarray = field(default_factory=lambda: np.zeros((0, 5)))   # t, x, z, alt, fix_age_s
    rearms: np.ndarray = field(default_factory=lambda: np.zeros((0, 5)))    # t, x, z, alt, manual
    meta: dict = field(default_factory=dict)

    @property
    def n(self) -> int:
        return int(self.floor.shape[0])

    def index(self, x: float, z: float) -> "tuple[int, int] | None":
        i = math.floor((z - self.origin) / self.cell)
        j = math.floor((x - self.origin) / self.cell)
        if 0 <= i < self.n and 0 <= j < self.n:
            return i, j
        return None

    def floor_at(self, x: float, z: float, trusted: bool = True) -> "float | None":
        """The flown floor of the cell holding (x, z); with `trusted`, only a floor two passes
        agree on. None for a cell nobody has flown (or only one pass, when trusted)."""
        ij = self.index(x, z)
        if ij is None:
            return None
        v = float((self.floor2 if trusted else self.floor)[ij])
        return None if math.isnan(v) else v

    def cell_centre(self, i: int, j: int) -> "tuple[float, float]":
        return self.origin + (j + 0.5) * self.cell, self.origin + (i + 0.5) * self.cell

    def save(self, path: str) -> None:
        np.savez_compressed(path, cell=self.cell, origin=self.origin, floor=self.floor,
                            floor2=self.floor2, passes=self.passes, near_floor=self.near_floor,
                            last_t=self.last_t, crash_alt=self.crash_alt, crash_n=self.crash_n,
                            overhang=self.overhang, crashes=self.crashes, rearms=self.rearms,
                            meta=json.dumps(self.meta, sort_keys=True))

    @classmethod
    def load(cls, path: str) -> "AirspaceRecord":
        z = np.load(path)
        return cls(float(z["cell"]), float(z["origin"]), z["floor"], z["floor2"], z["passes"],
                   z["near_floor"], z["last_t"], z["crash_alt"], z["crash_n"], z["overhang"],
                   z["crashes"], z["rearms"], json.loads(str(z["meta"])))


def _columns(samples) -> "tuple[np.ndarray, ...]":
    if not samples:
        e = np.zeros(0)
        return e, e, e, e, np.zeros(0, dtype=np.int64)
    a = np.array([(s.t, s.x, s.z, s.alt) for s in samples], dtype=np.float64)
    life = np.array([s.life for s in samples], dtype=np.int64)
    return a[:, 0], a[:, 1], a[:, 2], a[:, 3], life


def _spikes(t, x, z, life, max_speed) -> np.ndarray:
    """Samples (already sorted by life, then time) that both neighbours in the same life, within
    MAX_JOIN_S, reach only faster than `max_speed`: a fix off by kilometres."""
    n = len(t)
    bad = np.zeros(n, dtype=bool)
    if n < 3:
        return bad
    dt = np.diff(t)
    step = np.hypot(np.diff(x), np.diff(z))
    same = (life[1:] == life[:-1]) & (dt > 0) & (dt <= MAX_JOIN_S)
    with np.errstate(divide="ignore", invalid="ignore"):
        fast = same & (step / np.where(dt > 0, dt, 1.0) > max_speed)
    bad[1:-1] = fast[:-1] & fast[1:]
    return bad


def build_record(samples, crashes=(), rearms=(), height: "HeightMap | None" = None,
                 cell: float = CELL_M, half_span: float = HALF_SPAN_M,
                 near_m: float = NEAR_FLOOR_M, max_join_s: float = MAX_JOIN_S,
                 max_speed: float = MAX_SPEED_MPS,
                 meta: "dict | None" = None) -> "tuple[AirspaceRecord, dict]":
    """The record from accepted samples, and counts of what each rule kept or dropped."""
    n = int(round(2 * half_span / cell))
    origin = -half_span
    stats = {"samples": len(samples), "spikes": 0, "samples_inside": 0, "fill_points": 0,
             "fill_inside": 0, "segments": 0, "segments_too_fast": 0}
    t, x, z, alt, life = _columns(list(samples))
    order = np.lexsort((t, life))
    t, x, z, alt, life = t[order], x[order], z[order], alt[order], life[order]

    spike = _spikes(t, x, z, life, max_speed)
    stats["spikes"] = int(spike.sum())
    t, x, z, alt, life = t[~spike], x[~spike], z[~spike], alt[~spike], life[~spike]

    # Fill between consecutive samples of a life at the higher altitude.
    px, pz, palt, plife, pt = [x], [z], [alt], [life], [t]
    if len(t) >= 2:
        dt = np.diff(t)
        dx, dz = np.diff(x), np.diff(z)
        length = np.hypot(dx, dz)
        join = (life[1:] == life[:-1]) & (dt > 0) & (dt <= max_join_s)
        stats["segments"] = int(join.sum())
        too_fast = join & (length > max_speed * np.where(dt > 0, dt, 0.0))
        stats["segments_too_fast"] = int(too_fast.sum())
        join &= ~too_fast
        seg = np.flatnonzero(join)
        step = cell * FILL_STEP_FRACTION
        m = np.maximum(1, np.ceil(length[seg] / step).astype(np.int64))
        inner = m - 1
        if inner.sum() > 0:
            rep = np.repeat(np.arange(len(seg)), inner)
            starts = np.cumsum(inner) - inner
            k = np.arange(int(inner.sum())) - np.repeat(starts, inner) + 1
            frac = k / m[rep]
            s = seg[rep]
            px.append(x[s] + dx[s] * frac)
            pz.append(z[s] + dz[s] * frac)
            palt.append(np.maximum(alt[s], alt[s + 1]))
            plife.append(life[s])
            pt.append(t[s] + dt[s] * frac)
    nsamp = len(t)
    X, Z, A = np.concatenate(px), np.concatenate(pz), np.concatenate(palt)
    L, T = np.concatenate(plife), np.concatenate(pt)
    is_fill = np.arange(len(X)) >= nsamp
    stats["fill_points"] = int(is_fill.sum())

    keep = np.ones(len(X), dtype=bool)
    if height is not None and len(X):
        inside = height.inside(X, Z, A)
        stats["samples_inside"] = int((inside & ~is_fill).sum())
        stats["fill_inside"] = int((inside & is_fill).sum())
        keep &= ~inside
    i = np.floor((Z - origin) / cell).astype(np.int64)
    j = np.floor((X - origin) / cell).astype(np.int64)
    keep &= (i >= 0) & (i < n) & (j >= 0) & (j < n)
    cellf, L, A, T = (i * n + j)[keep], L[keep], A[keep], T[keep]

    floor = np.full(n * n, np.nan, dtype=np.float32)
    floor2 = np.full(n * n, np.nan, dtype=np.float32)
    passes = np.zeros(n * n, dtype=np.int32)
    near = np.zeros(n * n, dtype=np.int32)
    last_t = np.full(n * n, np.nan, dtype=np.float64)
    if len(cellf):
        o = np.lexsort((A, L, cellf))
        c, lf, a = cellf[o], L[o], A[o]
        first = np.ones(len(c), dtype=bool)
        first[1:] = (c[1:] != c[:-1]) | (lf[1:] != lf[:-1])
        pc, pa = c[first], a[first]                  # one row per pass: its cell, its lowest
        o2 = np.lexsort((pa, pc))
        pc, pa = pc[o2], pa[o2]
        starts = np.flatnonzero(np.r_[True, pc[1:] != pc[:-1]])
        counts = np.diff(np.r_[starts, len(pc)])
        cells = pc[starts]
        floor[cells] = pa[starts]
        two = counts >= 2
        floor2[cells[two]] = pa[starts[two] + 1]
        passes[cells] = counts
        group = np.repeat(np.arange(len(cells)), counts)
        near[cells] = np.bincount(group, weights=(pa <= pa[starts][group] + near_m)).astype(np.int32)
        np.fmax.at(last_t, cellf, T)

    crash_alt = np.full(n * n, np.nan, dtype=np.float32)
    crash_n = np.zeros(n * n, dtype=np.int32)
    crash_rows = np.array([(c.t, c.x, c.z, c.alt, c.fix_age_s) for c in crashes],
                          dtype=np.float64).reshape(-1, 5)
    if len(crash_rows):
        ci = np.floor((crash_rows[:, 2] - origin) / cell).astype(np.int64)
        cj = np.floor((crash_rows[:, 1] - origin) / cell).astype(np.int64)
        on = (ci >= 0) & (ci < n) & (cj >= 0) & (cj < n) & np.isfinite(crash_rows[:, 3])
        flat = (ci * n + cj)[on]
        np.fmax.at(crash_alt, flat, crash_rows[on, 3].astype(np.float32))
        np.add.at(crash_n, flat, 1)
    rearm_rows = np.array([(r.t, r.x, r.z, r.alt, float(r.manual)) for r in rearms],
                          dtype=np.float64).reshape(-1, 5)

    if height is not None:
        _, _, overhang = height.per_cell(cell, origin, n)
    else:
        overhang = np.zeros((n, n), dtype=bool)
    rec = AirspaceRecord(cell, origin, floor.reshape(n, n), floor2.reshape(n, n),
                         passes.reshape(n, n), near.reshape(n, n), last_t.reshape(n, n),
                         crash_alt.reshape(n, n), crash_n.reshape(n, n), overhang,
                         crash_rows, rearm_rows, dict(meta or {}))
    rec.meta.setdefault("stats", stats)
    return rec, stats


# ---- the planner's query (Design 017 phase 2) ----------------------------------------------
FREE, NOT_KNOWN, GROUND = 0, 1, 2
ANSWERS = ("free", "not known", "ground")
# Design 017 phase 2 (2026-11-26, scripts/airspace-score.py): a 100 m side margin cost 14 points
# of ordinary flight and separated crashes from kills no better; 50 m up kept.
MARGIN_SIDE_M = 0.0         # a point is held to the highest trusted floor within this
MARGIN_UP_M = 50.0          # and must clear it by this


class AirspaceQuery:
    """Free, not known or ground, for points and paths (Design 017, "The planner's query").

    - **ground**: the point is inside the mesh (Design 020's height map, at its own 16 m
      resolution, overhang gaps free). Crash samples are not used: phase 1 found only 3 to 22% of
      them within 100 m of the ground.
    - **free**: the point's own cell has a trusted floor, and the point clears the highest
      trusted floor among the cells within `margin_side` by `margin_up`.
    - **not known**: anything else. Not known is never clear.

    The mesh check is what keeps a coarse cell honest: phase 1 found rock above the trusted floor
    somewhere inside 72 to 301 cells per arena at 64 m, and a point on that rock answers ground
    whatever the floor says. Rock beside the point is not held against it, or every canyon flight
    below the rim would be not known."""

    def __init__(self, record: AirspaceRecord, height: "HeightMap | None" = None,
                 margin_side: float = MARGIN_SIDE_M, margin_up: float = MARGIN_UP_M,
                 two_pass: bool = True):
        self.record = record
        self.height = height
        self.margin_side = float(margin_side)
        self.margin_up = float(margin_up)
        # two_pass=False takes one pass's floor: phase 2 measures what the two-pass rule costs
        # where the mesh already drops every sample inside the rock.
        trusted = np.asarray(record.floor2 if two_pass else record.floor, dtype=np.float64)
        n, cell = record.n, record.cell
        r = int(math.ceil(self.margin_side / cell)) if self.margin_side > 0 else 0
        pad = np.pad(trusted, r, constant_values=np.nan)
        highest = np.full_like(trusted, np.nan)
        for di in range(-r, r + 1):
            for dj in range(-r, r + 1):
                # a neighbour counts when any of it lies within the margin of the cell's centre
                if math.hypot(max(abs(di) - 0.5, 0.0), max(abs(dj) - 0.5, 0.0)) * cell > self.margin_side:
                    continue
                highest = np.fmax(highest, pad[r + di:r + di + n, r + dj:r + dj + n])
        self.need = np.where(np.isfinite(trusted), highest + self.margin_up, np.nan)

    def points(self, x, z, alt) -> np.ndarray:
        """FREE, NOT_KNOWN or GROUND for each point."""
        x = np.asarray(x, dtype=np.float64)
        z = np.asarray(z, dtype=np.float64)
        alt = np.asarray(alt, dtype=np.float64)
        rec = self.record
        i = np.floor((z - rec.origin) / rec.cell).astype(np.int64)
        j = np.floor((x - rec.origin) / rec.cell).astype(np.int64)
        on = (i >= 0) & (i < rec.n) & (j >= 0) & (j < rec.n)
        need = np.where(on, self.need[np.where(on, i, 0), np.where(on, j, 0)], np.nan)
        with np.errstate(invalid="ignore"):
            free = np.isfinite(need) & (alt >= need)
        out = np.where(free, FREE, NOT_KNOWN)
        if self.height is not None:
            out = np.where(self.height.inside(x, z, alt, tol=0.0), GROUND, out)
        return out

    def path(self, x, z, alt) -> "tuple[int, int | None]":
        """The worst answer over a path's points, and the index of its first point that is not
        free (None when every point is free)."""
        codes = self.points(x, z, alt)
        if not len(codes):
            return NOT_KNOWN, None
        bad = np.flatnonzero(codes != FREE)
        return int(codes.max()), (int(bad[0]) if len(bad) else None)


# Bands of a flown floor's height over the highest ground in its cell. Below 0 the cell
# holds rock higher than the floor somewhere inside it: the cell is coarser than the ground.
AGL_BANDS = ((-math.inf, 0.0), (0.0, 100.0), (100.0, 300.0), (300.0, 1000.0), (1000.0, math.inf))


def mesh_check(record: AirspaceRecord, height: HeightMap, arena_radius_m: float = 10000.0) -> dict:
    """Design 017 Phase 1's checks against the mesh: coverage by height band, flown floors
    under the ground, and crash samples against the ground under them."""
    gmax, gmin, _ = height.per_cell(record.cell, record.origin, record.n)
    flown = np.isfinite(record.floor)
    trusted = np.isfinite(record.floor2)
    ii, jj = np.mgrid[:record.n, :record.n]
    cx = record.origin + (jj + 0.5) * record.cell
    cz = record.origin + (ii + 0.5) * record.cell
    in_arena = np.hypot(cx, cz) <= arena_radius_m
    cell_km2 = record.cell ** 2 / 1e6
    agl = record.floor - gmax
    bands = []
    for lo, hi in AGL_BANDS:
        sel = flown & (agl >= lo) & (agl < hi)
        bands.append({"lo": lo, "hi": hi, "cells": int(sel.sum()), "trusted": int((sel & trusted).sum())})
    with np.errstate(invalid="ignore"):
        under_all = flown & (record.floor < gmin - INSIDE_GROUND_M) & ~record.overhang
        rock_above = flown & (record.floor < gmax - INSIDE_GROUND_M)
        rock_above_trusted = trusted & (record.floor2 < gmax - INSIDE_GROUND_M)
    excess = (gmax - record.floor)[rock_above]
    crashes = record.crashes
    crash_over = (crashes[:, 3] - height.ground(crashes[:, 1], crashes[:, 2])) if len(crashes) else np.zeros(0)
    return {
        "arena_km2": float(in_arena.sum() * cell_km2),
        "flown_km2": float((flown & in_arena).sum() * cell_km2),
        "trusted_km2": float((trusted & in_arena).sum() * cell_km2),
        "flown_cells": int(flown.sum()),
        "trusted_cells": int(trusted.sum()),
        "bands": bands,
        "floor_under_all_ground": int(under_all.sum()),
        "rock_above_floor": int(rock_above.sum()),
        "rock_above_trusted_floor": int(rock_above_trusted.sum()),
        "rock_above_floor_m": _quantiles(excess),
        "crashes": int(len(crashes)),
        "crash_over_ground_m": _quantiles(crash_over),
        "crashes_within_100m": int((np.abs(crash_over) <= 100.0).sum()),
    }


def _quantiles(values) -> dict:
    v = np.asarray(values, dtype=np.float64)
    v = v[np.isfinite(v)]
    if not len(v):
        return {"n": 0}
    p10, p50, p90 = np.percentile(v, [10, 50, 90])
    return {"n": int(len(v)), "p10": float(p10), "median": float(p50), "p90": float(p90)}
