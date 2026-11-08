"""Design 020 Phase 4 (shadow): where the aircraft is on the map, and whether its path meets the ground.

Each battle tick, given the MINIMAP crop and the compass's north bearing:

1. **Fix.** The crop, turned north-up, is matched against the game's own minimap
   texture for the map being played (normalised cross-correlation with the rim,
   the own icon and the view cone masked out, sub-pixel peak). The texture spans
   ±10 km of the arena centre, +x east, +z north (Design 020, "Phase 0 results").
2. **Path.** Ground speed and track come from this fix and the last one; altitude
   is the HUD reading carried forward at its rate. The path is projected straight
   ahead and `track_spread_deg` either side for `horizon_s`.
3. **Time to impact** against the height map built from the game's collision
   meshes. The HUD altitude is the mesh height itself, signed (Design 020,
   settled 2026-11-07). Logged twice: with no clearance (`ttc_y`), and with the
   path required to clear the ground by `clearance_margin_m` (`ttc_m`). Over 39
   live crashes a margin of 150 m warned before 85% at 6.6% of ordinary ticks,
   against 67% at 4.6% with none.

Shadow only: nothing steers on this. It reads files on disk and the screen,
nothing else. Without data for the map it answers nothing, and wingman flies as
before. The data (texture and height map) is built offline by the tools in
`test_screenshots/terrain/tools/` and is not shipped.

Standard library, numpy and OpenCV only.
"""

from __future__ import annotations

import logging
import math
import os
import random
from dataclasses import dataclass

import cv2
import numpy as np

from .config_schema import schema_default

logger = logging.getLogger(__name__)

_TEXTURE_SPAN_M = 20000.0       # the minimap texture covers ±10 km
_REFERENCE_CROP_PX = 320        # the MINIMAP crop's width at 1920 by 1200


def _d(key: str):
    return schema_default(f"terrain_service.{key}")


@dataclass(frozen=True)
class Fix:
    t: float
    x: float
    z: float
    score: float
    second: float


@dataclass(frozen=True)
class TerrainReading:
    fix: "Fix | None"
    reason: "str | None" = None          # why there is no fix
    ground: "float | None" = None        # height-map top under the fix, mesh metres
    ttc_y: "float | None" = None         # seconds to impact if HUD = mesh height
    ttc_m: "float | None" = None         # seconds until the path is within the clearance margin
    speed: "float | None" = None         # ground speed from the last two fixes, m/s
    # Design 020 way_out: each candidate's seconds to impact (None = clear for the way-out
    # horizon), and the best of them. Filled when the straight path meets the ground within
    # decide_s, or the 2 s warning fires; action() flies it in the `map` arm (ADR 174 rev 1).
    way_out: "dict | None" = None
    way_out_best: "str | None" = None
    # The altitude is 30 m or more below the ground under the fix: a fix off at a cliff
    # or a stale altitude, not a real state. Such a reading warns of nothing.
    inside_ground: bool = False

    def log_text(self, map_name: "str | None") -> str:
        if self.fix is None:
            return f"map={map_name} fix=none({self.reason})"
        f = self.fix

        def ttc(v):
            return "clear" if v is None else f"{v:.1f}s"
        speed = "n/a" if self.speed is None else f"{self.speed:.0f}"
        text = (f"map={map_name} fix={f.x:.0f},{f.z:.0f} score={f.score:.2f}/{f.second:.2f} "
                f"ground={self.ground:.0f} speed={speed} ttc_y={ttc(self.ttc_y)} ttc_m={ttc(self.ttc_m)}")
        if self.way_out:
            outs = " ".join(f"{k}={ttc(v)}" for k, v in self.way_out.items())
            text += f" way_out={self.way_out_best} [{outs}]"
        if self.inside_ground:
            text += " inside_ground"
        return text


class _MapData:
    """One map's minimap texture (grey, float32) and height map (top layer)."""

    def __init__(self, minimap_path: str, height_path: str):
        img = cv2.imread(minimap_path, cv2.IMREAD_GRAYSCALE)
        if img is None:
            raise FileNotFoundError(minimap_path)
        self.texture = img.astype(np.float32)
        hm = np.load(height_path)
        self.top = hm["top"].astype(np.float32)
        self.cell = float(hm["cell"])
        self.origin = float(hm["origin"])
        self._scaled: dict = {}

    def scaled(self, mpp: float, pad: int, blur_px: float = 0.0) -> np.ndarray:
        """The texture at `mpp` metres a pixel, blurred by `blur_px`, padded by `pad` so a fix can
        reach the edge."""
        key = (round(mpp, 3), pad, round(blur_px, 2))
        img = self._scaled.get(key)
        if img is None:
            side = max(1, int(round(_TEXTURE_SPAN_M / mpp)))
            small = cv2.resize(self.texture, (side, side), interpolation=cv2.INTER_AREA)
            if blur_px > 0:
                small = cv2.GaussianBlur(small, (0, 0), blur_px)
            img = cv2.copyMakeBorder(small, pad, pad, pad, pad, cv2.BORDER_CONSTANT,
                                     value=float(small.mean()))
            self._scaled[key] = img
        return img

    def ground(self, x: float, z: float) -> float:
        n = self.top.shape[0]
        i = int((z - self.origin) / self.cell)
        j = int((x - self.origin) / self.cell)
        if 0 <= i < n and 0 <= j < n:
            return float(self.top[i, j])
        return float(self.top.min())    # off the map: the water plane


class TerrainService:
    """Shadow position fix and time to impact from the minimap (Design 020 Phase 4)."""

    def __init__(self, cfg: "dict | None" = None):
        cfg = cfg or {}

        def get(key):
            return cfg.get(key, _d(key))
        self.enabled = bool(get("enabled"))
        self._maps = dict(cfg.get("maps") or {})      # a MapOf: no single default
        self._mpp_ref = float(get("mpp_at_320px"))
        self._min_score = float(get("min_score"))
        self._min_gap = float(get("min_gap"))
        self._horizon = float(get("horizon_s"))
        self._spread = float(get("track_spread_deg"))
        self._max_fix_gap = float(get("max_fix_gap_s"))
        self._window_m = float(get("search_window_m"))
        self._blur_m = float(get("match_blur_m"))
        self._clearance = float(get("clearance_margin_m"))
        # ADR 174: a warning is ttc_y at or under warn_s; in the `map` arm, drawn per
        # round with probability trial_share, it starts the emergency climb.
        self._warn_s = float(get("warn_s"))
        self._way_out_h = float(get("way_out_horizon_s"))
        self._decide_s = float(get("decide_s"))
        self.turn_max_s = float(get("turn_max_s"))
        self.trial_share = float(get("trial_share"))
        self._rng = random.Random()
        self.arm: "str | None" = None
        self.rounds = 0
        self._loaded: dict = {}          # map name -> _MapData, or None when it failed
        self._last_fix: "Fix | None" = None
        self._last_map: "str | None" = None
        self._masks: dict = {}

    def has_map(self, map_name: "str | None") -> bool:
        return map_name is not None and map_name in self._maps

    def new_round(self) -> "str | None":
        """ADR 174: draw this round's arm, `map` or `control`; None while the trial is off."""
        if self.trial_share <= 0.0:
            self.arm = None
            return None
        self.arm = "map" if self._rng.random() < self.trial_share else "control"
        self.rounds += 1
        return self.arm

    def warns(self, reading: "TerrainReading | None") -> bool:
        """ADR 174: the path meets the ground within warn_s, with no margin."""
        return (reading is not None and not reading.inside_ground and reading.ttc_y is not None
                and reading.ttc_y <= self._warn_s)

    def flies(self) -> bool:
        """ADR 174: whether a warning starts the emergency climb this round."""
        return self.arm == "map"

    def _data(self, map_name: str) -> "_MapData | None":
        if map_name not in self._loaded:
            entry = self._maps.get(map_name) or {}
            try:
                self._loaded[map_name] = _MapData(str(entry.get("minimap")), str(entry.get("height")))
                logger.info("TERRAIN: loaded %s (%s)", map_name, os.path.dirname(str(entry.get("height"))))
            except Exception as exc:
                logger.warning("TERRAIN: no data for %s (%s); answering nothing on this map",
                               map_name, exc)
                self._loaded[map_name] = None
        return self._loaded[map_name]

    def _mask(self, h: int, w: int):
        key = (h, w)
        m = self._masks.get(key)
        if m is None:
            cy, cx = (h - 1) / 2.0, (w - 1) / 2.0
            radius = min(h, w) / 2.0
            yy, xx = np.mgrid[:h, :w]
            r = np.hypot(xx - cx, yy - cy)
            ang = np.degrees(np.arctan2(xx - cx, -(yy - cy)))
            # inside the rim, outside the own icon, and not the view cone ahead
            m = ((r < 0.80 * radius) & (r > 0.12 * radius) & ~(np.abs(ang) < 22)).astype(np.float32)
            self._masks[key] = m
        return m

    def fix(self, map_name: str, crop: np.ndarray, north_bearing: float, now: float) -> "Fix | None":
        """Where the minimap's centre is on the map, or None when the match is weak or ambiguous."""
        data = self._data(map_name)
        if data is None or crop is None or crop.size == 0:
            return None
        g = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY).astype(np.float32) if crop.ndim == 3 else crop.astype(np.float32)
        h, w = g.shape
        cy, cx = (h - 1) / 2.0, (w - 1) / 2.0
        radius = min(h, w) / 2.0
        half = int(0.80 * radius) + 1
        if int(cy) - half < 0 or int(cx) - half < 0:
            return None
        turn = cv2.getRotationMatrix2D((cx, cy), north_bearing, 1.0)     # N to the top
        gr = cv2.warpAffine(g, turn, (w, h))
        mr = cv2.warpAffine(self._mask(h, w), turn, (w, h), flags=cv2.INTER_NEAREST)
        rows = slice(int(cy) - half, int(cy) + half)
        cols = slice(int(cx) - half, int(cx) + half)
        tpl, msk = gr[rows, cols], mr[rows, cols]
        if float(msk.sum()) < 100 or float(tpl[msk > 0].std()) < 1.0:
            return None
        mpp = self._mpp_ref * _REFERENCE_CROP_PX / float(w)
        # Design 020: blur both sides by about 100 m. Stone Forest's minimap draws trees as
        # speckle its texture lacks: unblurred 0 of 55 recorded crops fixed, blurred 50 of 55
        # (99% of consecutive fixes agreeing); Crimson Canyon's margin rose 0.49 to 0.51.
        blur_px = self._blur_m / mpp
        if blur_px > 0:
            tpl = cv2.GaussianBlur(tpl, (0, 0), blur_px)
        padded = data.scaled(mpp, half, blur_px)
        last = self._last_fix
        if last is not None and 0 < now - last.t <= self._max_fix_gap:
            # Near the last fix first: the aircraft cannot have gone far, and a
            # window is a fraction of the cost of the whole map.
            found = self._search(padded, tpl, msk, mpp, half, (last.x, last.z), self._window_m)
            if found is not None:
                return Fix(now, *found)
        found = self._search(padded, tpl, msk, mpp, half, None, None)
        return None if found is None else Fix(now, *found)

    def _search(self, padded, tpl, msk, mpp, half, near, radius_m):
        """(x, z, score, second) of the best match, in the window around `near` or everywhere."""
        r0 = c0 = 0
        img = padded
        if near is not None:
            n = padded.shape[0] - 2 * half                    # result positions = unpadded pixels
            u = int(round((near[0] + _TEXTURE_SPAN_M / 2) / mpp))
            v = int(round((_TEXTURE_SPAN_M / 2 - near[1]) / mpp))
            rad = int(round(radius_m / mpp))
            r0, r1 = max(0, v - rad), min(n - 1, v + rad)
            c0, c1 = max(0, u - rad), min(n - 1, u + rad)
            if r1 <= r0 or c1 <= c0:
                return None
            img = padded[r0:r1 + 2 * half + 1, c0:c1 + 2 * half + 1]
        res = cv2.matchTemplate(img, tpl, cv2.TM_CCOEFF_NORMED, mask=msk)
        res = np.nan_to_num(res, nan=-1.0, posinf=-1.0, neginf=-1.0)
        _, score, _, (px, py) = cv2.minMaxLoc(res)
        excl = max(1, int(round(1000.0 / mpp)))              # the best match more than 1 km away
        other = res.copy()
        other[max(0, py - excl):py + excl + 1, max(0, px - excl):px + excl + 1] = -1.0
        second = float(other.max()) if other.size else -1.0
        if score < self._min_score or score - second < self._min_gap:
            return None
        fx, fy = _subpixel(res, px, py)
        # padding is `half`, so the peak's index is the template centre's texture pixel
        fx, fy = fx + c0, fy + r0
        return (fx * mpp - _TEXTURE_SPAN_M / 2, _TEXTURE_SPAN_M / 2 - fy * mpp, float(score), second)

    def observe(self, map_name: "str | None", crop, north_bearing: "float | None",
                altitude: "float | None", altitude_rate: "float | None", now: float) -> "TerrainReading | None":
        """This tick's reading, or None when the service has nothing to say (off, or no data for the map)."""
        if not self.enabled or not self.has_map(map_name):
            return None
        if map_name != self._last_map:
            self._last_map, self._last_fix = map_name, None
        if self._data(map_name) is None:
            return None
        if north_bearing is None:
            return TerrainReading(None, "no-compass")
        fix = self.fix(map_name, crop, north_bearing, now)
        if fix is None:
            return TerrainReading(None, "weak-match")
        prev, self._last_fix = self._last_fix, fix
        data = self._data(map_name)
        ground = data.ground(fix.x, fix.z)
        if prev is None or not 0.2 <= fix.t - prev.t <= self._max_fix_gap or altitude is None:
            return TerrainReading(fix, ground=ground)
        dt = fix.t - prev.t
        vx, vz = (fix.x - prev.x) / dt, (fix.z - prev.z) / dt
        speed = math.hypot(vx, vz)
        if not 30.0 <= speed <= 700.0:
            return TerrainReading(fix, ground=ground, speed=speed)
        rate = float(altitude_rate or 0.0)
        ttc_y = self.time_to_impact(data, fix.x, fix.z, vx, vz, float(altitude), rate)
        ttc_m = self.time_to_impact(data, fix.x, fix.z, vx, vz, float(altitude) - self._clearance, rate)
        if float(altitude) < ground - 30.0:
            return TerrainReading(fix, ground=ground, speed=speed, ttc_y=ttc_y, ttc_m=ttc_m,
                                  inside_ground=True)
        # Design 020: decide while there is room to. The way out is worked out whenever the
        # straight path meets the ground within decide_s (offline, 107 crashes: deciding at
        # 5 s found a clear way out for 62, at 2 s for 12), or the 2 s warning's spread does.
        hold = fly_candidate(data, fix.x, fix.z, vx, vz, float(altitude), rate, 0.0, 0.0, self._way_out_h)
        outs = best = None
        if (hold is not None and hold <= self._decide_s) or ttc_y is not None:
            outs, best = self.way_out(data, fix.x, fix.z, vx, vz, float(altitude), rate)
        return TerrainReading(fix, ground=ground, speed=speed, ttc_y=ttc_y, ttc_m=ttc_m,
                              way_out=outs, way_out_best=best)

    def action(self, reading: "TerrainReading | None") -> "str | None":
        """ADR 174 revised: what to fly this tick in the `map` arm — "climb", "left", "right", or None.

        The best candidate when the straight path meets the ground within decide_s and a better
        one exists; the climb when nothing beats holding course and the 2 s warning fires; nothing
        when the straight path is clear (the warning's 15 deg spread alone does not fly), or the
        reading is inside the ground."""
        if not self.flies() or reading is None or reading.inside_ground or not reading.way_out:
            return None
        outs = reading.way_out
        if outs.get("hold") is None:
            return None
        best = reading.way_out_best
        if best in ("left", "left+climb"):
            return "left"
        if best in ("right", "right+climb"):
            return "right"
        if best == "climb":
            return "climb"
        return "climb" if self.warns(reading) else None

    def way_out(self, data: _MapData, x: float, z: float, vx: float, vz: float,
                height: float, climb_rate: float) -> "tuple[dict, str]":
        """Design 020: each candidate manoeuvre's seconds to impact over the way-out horizon (None =
        clear), and the best: clear, or else the latest impact; ties to the one that turns least."""
        outs = {"hold": fly_candidate(data, x, z, vx, vz, height, climb_rate, 0.0, 0.0, self._way_out_h)}
        for name, turn, pull, level in WAY_OUT_CANDIDATES:
            outs[name] = fly_candidate(data, x, z, vx, vz, height, climb_rate, turn, pull, self._way_out_h,
                                       level_deg_s=level)
        def merit(name):
            v = outs[name]
            return float("inf") if v is None else v
        best = max(outs, key=merit)                  # dict order keeps the least-turning first on ties
        return outs, best

    def time_to_impact(self, data: _MapData, x: float, z: float, vx: float, vz: float,
                       height: float, climb_rate: float) -> "float | None":
        """First time within the horizon that the straight or turned path meets the ground; None if clear."""
        speed = math.hypot(vx, vz)
        track = math.atan2(vx, vz)
        best = None
        steps = int(round(self._horizon / 0.1))
        for off in (-self._spread, 0.0, self._spread):
            th = track + math.radians(off)
            sx, sz = speed * math.sin(th), speed * math.cos(th)
            for k in range(1, steps + 1):
                s = k * 0.1
                if data.ground(x + sx * s, z + sz * s) > height + climb_rate * s:
                    best = s if best is None else min(best, s)
                    break
        return best


# Design 020 way_out: candidate manoeuvres as (turn deg/s, signed; pull deg/s of path angle;
# level deg/s, raising a dive toward level). Rates measured 2026-11-08: wingman's heading
# changed faster than 20 deg/s on 15% of ticks (p90 29); flown terrain turns came round at
# about 40 deg/s level; pull-outs reach about 22 deg/s (Research 020). A turn is flown as a
# bank and pull with the nose held up, so it also levels a dive: the 06:31:08 turn took the
# nose from -32 to +8 deg in about 3 s. Order breaks ties: the one that turns least first.
WAY_OUT_CANDIDATES = (
    ("climb", 0.0, 22.0, 0.0),
    ("left", -25.0, 0.0, 13.0),
    ("right", 25.0, 0.0, 13.0),
    ("left+climb", -18.0, 15.0, 0.0),
    ("right+climb", 18.0, 15.0, 0.0),
)


def fly_candidate(data: "_MapData", x: float, z: float, vx: float, vz: float, height: float,
                  climb_rate: float, turn_deg_s: float, pull_deg_s: float, horizon_s: float,
                  reaction_s: float = 0.5, max_path_deg: float = 50.0, step_s: float = 0.1,
                  level_deg_s: float = 0.0) -> "float | None":
    """Seconds until a manoeuvre, started after `reaction_s` on the current path, meets the ground;
    None if it stays clear for `horizon_s`. Speed is held. The pull raises the path angle to at
    most `max_path_deg`; `level_deg_s` raises a dive toward level and no further."""
    vh = math.hypot(vx, vz)
    speed = math.hypot(vh, climb_rate)
    path = math.atan2(climb_rate, vh)
    heading = math.atan2(vx, vz)
    for k in range(1, int(round(horizon_s / step_s)) + 1):
        s = k * step_s
        if s > reaction_s:
            heading += math.radians(turn_deg_s) * step_s
            if pull_deg_s:
                path = min(path + math.radians(pull_deg_s) * step_s, math.radians(max_path_deg))
            elif level_deg_s and path < 0:
                path = min(path + math.radians(level_deg_s) * step_s, 0.0)
        horiz = speed * math.cos(path) * step_s
        x += horiz * math.sin(heading)
        z += horiz * math.cos(heading)
        height += speed * math.sin(path) * step_s
        if data.ground(x, z) > height:
            return round(s, 1)
    return None


def _subpixel(res: np.ndarray, x: int, y: int) -> "tuple[float, float]":
    """The correlation peak refined by a parabola through it and its neighbours, per axis."""
    def offset(a, b, c):
        denom = a - 2.0 * b + c
        return 0.0 if denom >= 0 else 0.5 * (a - c) / denom
    dx = offset(res[y, x - 1], res[y, x], res[y, x + 1]) if 0 < x < res.shape[1] - 1 else 0.0
    dy = offset(res[y - 1, x], res[y, x], res[y + 1, x]) if 0 < y < res.shape[0] - 1 else 0.0
    return x + float(dx), y + float(dy)
