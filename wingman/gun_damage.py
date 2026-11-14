"""The gun's hits, from the game's damage text: Design 021 Phase 1 (shadow).

On a hit the game writes "70 DMG" in white just left of the crosshair, and the
number adds up over one target (2026-11-13: 30, 30, then 70 on the kill). This
module reads it and writes one `GUNHIT:` line per burst: what the burst gained.
The reading runs on the range reader's worker (`TargetRangeReader`), which
already has an EasyOCR reader; a second reader would cost about 300 MB.

Grey, not a white mask: on the six 2026-11-13 frames with damage text the grey
crop read 30, 30, 70, 70, 80 and 200, all right, and the white mask read 74 and
2200 for two of them (clouds behind the text are white too).
"""

import logging
import re
from itertools import pairwise
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import cv2
import numpy as np

from .suppressed import log_suppressed

logger = logging.getLogger(__name__)

# The text's box as fractions of the frame: x 690-900, y 580-632 at 1920x1200.
# Its right edge sits near x 878; "200 DMG" starts near x 750.
DAMAGE_BOX_PCT = (690 / 1920, 580 / 1200, 900 / 1920, 632 / 1200)
_OCR_HEIGHT = 64
ALLOWLIST = "0123456789DMG "
# The number directly before "DMG". The grey crop reads stray digits around it
# ("3 80 DMG", "1   200 DMG 6"), so only the number touching DMG counts.
_DMG_RE = re.compile(r"(\d{1,4})\s*D\s*M+\s*G")
# How long after a burst its damage is still read and credited to it, s.
SETTLE_S = 2.0
# A missile launched this long before a burst opened, or during it and its
# settling time, can add to the same text: such a burst is logged "mixed".
MISSILE_BEFORE_S = 3.0
# Rev 6 (2026-11-14): a rise of this much or less is a gun hit. In today's
# bursts the rises were 9 to 55 (ten), then 75 (one), then 94 to 142 (nineteen,
# 98 twelve times: missiles), then 300 and more (misreads). A burst near a
# missile launch keeps its gun share this way.
GUN_STEP_MAX = 60
_BANDS = ((0.0, 1.0), (1.0, 1.2), (1.2, 2.0))
_CROP_DIR = Path("test_screenshots/unknown_anomalies")
_CROPS_PER_SESSION = 30
_JUNK_CROPS_PER_SESSION = 30


def damage_crop(frame: np.ndarray) -> np.ndarray:
    h, w = frame.shape[:2]
    x0, y0, x1, y1 = DAMAGE_BOX_PCT
    return frame[round(y0 * h):round(y1 * h), round(x0 * w):round(x1 * w)]


def ocr_input(crop: np.ndarray) -> np.ndarray:
    grey = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY) if crop.ndim == 3 else crop
    f = _OCR_HEIGHT / max(1, grey.shape[0])
    return cv2.resize(grey, None, fx=f, fy=f, interpolation=cv2.INTER_CUBIC)


def parse_damage(text: str) -> "int | None":
    m = _DMG_RE.search(text or "")
    return int(m.group(1)) if m else None


# Inside the 210 px wide crop (1920 px frame) the text is right-aligned: the
# number ends at column 113-114 and "DMG" starts at 127-128 on every crop seen
# (8 operator frames and 10 live crops, 2026-11-13).
_NUM_END_COL = 114
_NUM_END_TOL = 6
_WHITE_GAP_COLS = 8


def number_start(crop: np.ndarray) -> "int | None":
    """The column where the number starts: the start of the run of white glyph
    columns that ends where the number ends. None when no run ends there (a
    cloud behind the text joins everything into one run)."""
    scale = crop.shape[1] / 210.0
    hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
    white = cv2.inRange(hsv, (0, 0, 200), (180, 50, 255))
    cols = np.nonzero((white > 0).sum(axis=0) >= 2)[0]
    if cols.size == 0:
        return None
    breaks = np.nonzero(np.diff(cols) - 1 > _WHITE_GAP_COLS * scale)[0]
    starts = np.concatenate(([0], breaks + 1))
    ends = np.concatenate((breaks, [cols.size - 1]))
    for a, b in zip(starts, ends, strict=True):
        if abs(cols[b] - _NUM_END_COL * scale) <= _NUM_END_TOL * scale:
            return int(cols[a])
    return None


def read_damage(frame: np.ndarray, reader) -> "tuple[int | None, str, np.ndarray]":
    crop = damage_crop(frame)
    # Live, 2026-11-13 12:59 on CRIMSON: the canyon texture left of the number
    # read as a junk first digit ("188 DMG" as 2188, 3188, 4188, 6188; "94" as
    # 294), and the burst's gain came out 6,294. The read starts 6 px left of
    # the number instead; on the 18 crops it read every value right.
    start = number_start(crop)
    if start is not None:
        crop = crop[:, max(0, start - round(6 * crop.shape[1] / 210.0)):]
    res = reader.recognize(ocr_input(crop), detail=1, allowlist=ALLOWLIST)
    text = " ".join(r[1] for r in res) if res else ""
    return parse_damage(text), text, crop


@dataclass
class _Burst:
    opened: float               # monotonic
    km: "float | None"
    ring_px: "float | None"
    closed: "float | None" = None
    arm: "str | None" = None      # Phase 3: the round's arm, `ring` or `marker`
    aim: str = "marker"           # what the gun opened on: the ring or the marker
    reads: int = 0
    values: list = field(default_factory=list)


def wrapped(value: int, around: int) -> bool:
    """True when `value` reads as `around` with characters added in front of it
    or behind it: the misread every junk read so far has had (2026-11-13: 94 as
    294, 494, 794, 2494, 4794, 7094 and 941; 188 as 2188, 3188, 4188, 6188).
    A real rise that happens to end in the old digits (94 to 194) is taken for
    one too: a known under-count, rarer than the misread."""
    a, b = str(value), str(around)
    # Two digits at least: "100" ends in "0", and at 17:20:20 a single misread
    # "0" after eight readings of 100 took them all for junk.
    return len(b) >= 2 and a != b and (a.startswith(b) or a.endswith(b))


def burst_gain(values: "list[int]") -> "tuple[int, int, int | None, int | None]":
    """(gain, drops, first, last) over a burst's readings, in order.

    Only a value read twice in a row counts. The text stays on the screen for
    seconds and is read about three times a second, so a real value repeats; a
    misread is one reading (live 2026-11-13 13:48:17: 94, 94, 941, 94, 94
    credited 847 before this). Gain is the sum of the rises between the values
    that count: the text adds up over one target. A fall is a new target's text;
    it is a drop and the count starts again from it, so the new target's first
    hit inside the burst is not credited (a known under-count, shadow only)."""
    # A reading that wraps the one before it is that one (rev 4: 4794 was read
    # twice in a row at 14:23:53, so the rule below let it count); one the
    # next reading is wrapped by was the wrapped one all along.
    clean: "list[int]" = []
    for v in values:
        if clean and wrapped(v, clean[-1]):
            v = clean[-1]
        elif clean and wrapped(clean[-1], v):
            junk = clean[-1]
            clean = [v if c == junk else c for c in clean]
        clean.append(v)
    kept = [v for i, v in enumerate(clean) if i > 0 and clean[i - 1] == v]
    gain = drops = 0
    for a, b in pairwise(kept):
        if b > a:
            gain += b - a
        elif b < a:
            drops += 1
    return gain, drops, (kept[0] if kept else None), (kept[-1] if kept else None)


def gun_gain(values: "list[int]") -> int:
    """The gun's share of a burst's gain: the rises of GUN_STEP_MAX or less
    between the values `burst_gain` counts. A missile hit (about 94 to 142) and
    a misread (300 and more) are left out, so a burst near a missile launch
    still says what the gun did (rev 6)."""
    clean: "list[int]" = []
    for v in values:
        if clean and wrapped(v, clean[-1]):
            v = clean[-1]
        elif clean and wrapped(clean[-1], v):
            junk = clean[-1]
            clean = [v if c == junk else c for c in clean]
        clean.append(v)
    kept = [v for i, v in enumerate(clean) if i > 0 and clean[i - 1] == v]
    return sum(b - a for a, b in pairwise(kept) if 0 < b - a <= GUN_STEP_MAX)


class GunDamageLog:
    """Burst bookkeeping. The pursuit calls `burst_open` and `burst_close` with
    the gun; the worker calls `damage` with each reading; `tick` writes a
    burst's line once its settling time has passed.

    Gain is the sum of the rises between readings of the burst: the text adds up
    over one target, so a rise is damage done. A fall is a new target's text (or
    a misread); it is counted as a drop and the count starts again from it, so
    the new target's first hit inside the burst is not credited (a known
    under-count, shadow only)."""

    def __init__(self) -> None:
        # The pursuit thread opens and closes bursts; the range reader's worker
        # adds readings; cleanup flushes. Held only for bookkeeping, never
        # around OCR or file writes.
        self._lock = threading.Lock()
        self._burst: "_Burst | None" = None
        self._missile_ts: "float | None" = None
        self._crops = 0
        self._junk_crops = 0
        self.bursts = [0] * (len(_BANDS) + 1)       # by band, last = no range
        self.hit_bursts = [0] * (len(_BANDS) + 1)
        self.gun_s = [0.0] * (len(_BANDS) + 1)
        self.damage_total = [0] * (len(_BANDS) + 1)
        self.mixed = 0
        # Phase 3, by the round's arm: clean bursts, bursts that gained, gun
        # seconds, damage.
        self.by_arm: "dict[str, list]" = {}
        # Rev 6, by the round's arm, over every burst, mixed or not: bursts,
        # bursts with a gun-sized rise, gun seconds, gun damage.
        self.gun_by_arm: "dict[str, list]" = {}

    def missile_fired(self, now: "float | None" = None) -> None:
        if not self._lock.acquire(timeout=0.5):
            return
        try:
            self._missile_ts = time.monotonic() if now is None else now
        finally:
            self._lock.release()

    def reading_wanted(self, now: "float | None" = None) -> bool:
        """True while a burst is open or settling: only then is the text read."""
        if not self._lock.acquire(timeout=0.5):
            return False
        try:
            b = self._burst
            if b is None:
                return False
            now = time.monotonic() if now is None else now
            return b.closed is None or now - b.closed <= SETTLE_S
        finally:
            self._lock.release()

    def burst_open(self, km: "float | None", ring_px: "float | None",
                   now: "float | None" = None, *, arm: "str | None" = None,
                   aim: str = "marker") -> None:
        if not self._lock.acquire(timeout=0.5):
            return
        try:
            now = time.monotonic() if now is None else now
            if self._burst is not None:
                self._finish(self._burst)
            self._burst = _Burst(opened=now, km=km, ring_px=ring_px, arm=arm, aim=aim)
        finally:
            self._lock.release()

    def burst_close(self, now: "float | None" = None) -> None:
        if not self._lock.acquire(timeout=0.5):
            return
        try:
            b = self._burst
            if b is not None and b.closed is None:
                b.closed = time.monotonic() if now is None else now
        finally:
            self._lock.release()

    def damage(self, value: "int | None", crop) -> None:
        if not self._lock.acquire(timeout=0.5):
            return
        try:
            b = self._burst
            if b is None:
                return
            b.reads += 1
            if value is None:
                return
            junk = bool(b.values) and (wrapped(value, b.values[-1])
                                       or wrapped(b.values[-1], value))
            b.values.append(value)
        finally:
            self._lock.release()
        self._save(crop, value, junk)

    def tick(self, now: "float | None" = None) -> None:
        if not self._lock.acquire(timeout=0.5):
            return
        try:
            b = self._burst
            if b is None or b.closed is None:
                return
            now = time.monotonic() if now is None else now
            if now - b.closed > SETTLE_S:
                self._finish(b)
                self._burst = None
        finally:
            self._lock.release()

    def flush(self) -> None:
        if not self._lock.acquire(timeout=0.5):
            return
        try:
            if self._burst is not None:
                if self._burst.closed is None:
                    self._burst.closed = time.monotonic()
                self._finish(self._burst)
                self._burst = None
        finally:
            self._lock.release()

    def _finish(self, b: _Burst) -> None:
        closed = b.closed if b.closed is not None else time.monotonic()
        held = max(0.0, closed - b.opened)
        gain, drops, first, last = burst_gain(b.values)
        gun = gun_gain(b.values)
        tally = self.gun_by_arm.setdefault(b.arm or "-", [0, 0, 0.0, 0])
        tally[0] += 1
        tally[1] += gun > 0
        tally[2] += held
        tally[3] += gun
        mixed = (self._missile_ts is not None
                 and b.opened - MISSILE_BEFORE_S <= self._missile_ts <= closed + SETTLE_S)
        band = next((i for i, (lo, hi) in enumerate(_BANDS)
                     if b.km is not None and lo <= b.km < hi), len(_BANDS))
        if mixed:
            self.mixed += 1
        else:
            self.bursts[band] += 1
            self.gun_s[band] += held
            self.damage_total[band] += gain
            self.hit_bursts[band] += gain > 0
            if b.arm is not None:
                tally = self.by_arm.setdefault(b.arm, [0, 0, 0.0, 0])
                tally[0] += 1
                tally[1] += gain > 0
                tally[2] += held
                tally[3] += gain
        logger.debug(
            "GUNHIT: burst=%.1fs range=%s ring_px=%s reads=%d first=%s last=%s gain=%d "
            "gun=%d drops=%d%s arm=%s aim=%s values=%s",
            held, "none" if b.km is None else "%.2fkm" % b.km,
            "none" if b.ring_px is None else "%.0f" % b.ring_px, b.reads,
            "-" if first is None else first, "-" if last is None else last,
            gain, gun, drops, " mixed" if mixed else "", b.arm or "-", b.aim, b.values[:30])

    def _save(self, crop, value: int, junk: bool = False) -> None:
        """A crop for checking by eye. Readings that wrap their neighbour have
        their own budget: by 14:23 the plain budget was spent before the first
        such reading of the session."""
        if crop is None:
            return
        if junk:
            if self._junk_crops >= _JUNK_CROPS_PER_SESSION:
                return
            self._junk_crops += 1
        else:
            if self._crops >= _CROPS_PER_SESSION:
                return
            self._crops += 1
        try:
            _CROP_DIR.mkdir(parents=True, exist_ok=True)
            cv2.imwrite(str(_CROP_DIR / ("gundmg_%s%s_%d.png" % (
                "junk_" if junk else "", time.strftime("%Y%m%d_%H%M%S"), value))), crop)
        except Exception as exc:
            log_suppressed(logger, "gun_damage_save", exc)

    def summary(self) -> str:
        names = ["%.1f-%.1fkm" % b for b in _BANDS] + ["no range"]
        parts = []
        for n, k, hb, s, d in zip(names, self.bursts, self.hit_bursts, self.gun_s, self.damage_total, strict=True):
            if k:
                parts.append("%s %d bursts, %d gained, %.1fs, %d dmg (%.0f/s)"
                             % (n, k, hb, s, d, d / s if s > 0 else 0.0))
        arms = "".join("; %s arm %d bursts, %d gained, %.1fs, %d dmg" % (a, *t)
                       for a, t in sorted(self.by_arm.items()))
        arms += "".join("; gun share, %s arm: %d bursts, %d with gun hits, %.1fs, %d dmg" % (a, *t)
                        for a, t in sorted(self.gun_by_arm.items()))
        return ("; ".join(parts or ["no bursts"]) + "; %d mixed with a missile" % self.mixed
                + arms)
