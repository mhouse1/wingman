"""The tracked enemy's range, read off its nameplate (operator, 2026-11-13:
"machine gun activation should only be allowed when the target distance is
less than 2km away").

The game draws every enemy contact a red nameplate in three rows: the pilot's
name, the range ("1.3km", "0.95km", "10.4km") and the aircraft type. The
tracker already finds that nameplate (`TargetTracker`, the `cluster` of its
observation); this module finds the range row inside it and reads it.

How, and why this way (measured 2026-11-13 on the operator's frame
tests/fixtures/gun_range_f106_1p3km.png and 12 archived frames):

- EasyOCR's full detect-and-read on the 300 x 170 px nameplate window took
  3.6 to 6.8 s per call on VEDA's CPU with a session running, and read
  "1.3km" as "13km". Too slow for a 0.2 s steering cycle and wrong besides.
- The red mask splits each nameplate into three clean bands of rows (name,
  range, type: y 445-465, 476-494 and 509-529 on the operator's frame). The
  range is the band nearest the cluster's centre. Recognition alone on that
  band, upscaled and masked to red-on-white (`_ocr_input`), read all 12
  curated ranges in tests/fixtures/gun_range/ in about 50 ms each.
- A reading is accepted only when it starts "<digits>.<digits>km". Anything
  else (the name band picked by mistake, an icon over the digits, a dropped
  point) is no reading, and no reading means the gun stays off.

OCR runs on one long-lived worker thread: EasyOCR readers are per thread
(~300 MB each, `analyzer._get_thread_ocr_reader`), and some calls still took
1 to 2.6 s on a loaded CPU, which must not stall the steering.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass, field, replace
from pathlib import Path

import cv2
import numpy as np

from .gun_damage import read_damage
from .suppressed import log_suppressed

logger = logging.getLogger(__name__)

# The nameplate red, as the range digits draw it. The tracker's own mask
# raises the value floor to keep the afterburner out, which thins these
# glyphs; the window here is already on the nameplate, so a looser mask is
# safe. Measured on the operator's frame: these give three bands per label.
_RED_LO = (0, 100, 200)
_RED_HI = (8, 255, 255)
_RED_WRAP_LO = (170, 100, 200)
_RED_WRAP_HI = (180, 255, 255)
# Half the window searched around the cluster centre: the tracker's glyph
# window (300 x 170 px), which holds one whole nameplate.
_HALF_W, _HALF_H = 150, 85
# A text band: at least this many rows with at least _BAND_MIN_PX red pixels.
_BAND_MIN_ROWS = 8
_BAND_MIN_PX = 2
# Recognition input height, px, and the widest input passed on.
_OCR_HEIGHT = 64
_OCR_WIDTH = 400
_ALLOWLIST = "0123456789.km"
_RANGE_RE = re.compile(r"^(\d{1,2}\.\d{1,2})km")


def parse_range_km(text: "str | None") -> "float | None":
    """The range in km from a nameplate's range row, or None.

    The game always prints a point ("0.95km", "1.3km", "10.4km"), so text
    without one is a misread, not a whole number of km: "13km" was the
    operator's 1.3 km target. Trailing junk after "km" is ignored; leading
    junk is not accepted."""
    if not text:
        return None
    m = _RANGE_RE.match(text.replace(" ", ""))
    return float(m.group(1)) if m else None


def range_bands(mask: np.ndarray) -> "list[tuple[int, int]]":
    """(first row, end row) of each text band in a nameplate window's mask,
    top to bottom."""
    rows = (mask > 0).sum(axis=1)
    bands: "list[tuple[int, int]]" = []
    start = None
    for i, v in enumerate(list(rows) + [0]):
        if v >= _BAND_MIN_PX and start is None:
            start = i
        elif v < _BAND_MIN_PX and start is not None:
            if i - start >= _BAND_MIN_ROWS:
                bands.append((start, i))
            start = None
    return bands


def range_row_crops(frame: np.ndarray, cluster: "tuple[float, float]",
                    max_rows: int = 2) -> "list[np.ndarray]":
    """Crops of the text bands nearest the nameplate cluster's centre,
    nearest first, each cut to its red glyphs' width plus a small margin.
    The first is the range row on a plain three-row label; the second is
    tried when a fourth band (an icon, a marker) shifted the centre."""
    h, w = frame.shape[:2]
    cx, cy = int(round(cluster[0])), int(round(cluster[1]))
    x1, y1 = max(0, cx - _HALF_W), max(0, cy - _HALF_H)
    x2, y2 = min(w, cx + _HALF_W), min(h, cy + _HALF_H)
    if x2 - x1 < 20 or y2 - y1 < 20:
        return []
    window = frame[y1:y2, x1:x2]
    hsv = cv2.cvtColor(window, cv2.COLOR_BGR2HSV)
    mask = cv2.inRange(hsv, _RED_LO, _RED_HI) | cv2.inRange(hsv, _RED_WRAP_LO, _RED_WRAP_HI)
    bands = sorted(range_bands(mask), key=lambda b: abs((b[0] + b[1]) / 2.0 - (cy - y1)))
    crops = []
    for a, b in bands[:max_rows]:
        span = text_span(mask[a:b])
        if span is None:
            continue
        ya, yb = max(0, y1 + a - 4), min(h, y1 + b + 4)
        xa, xb = max(0, x1 + span[0] - 6), min(w, x1 + span[1] + 7)
        crops.append(frame[ya:yb, xa:xb])
    return crops


# Columns with no red wider than this split a row into groups (text_span).
# Measured 2026-11-13: inside the range text the widest gap is 4 to 6 px (11
# curated rows); between a contact's jet icon and the text it was 13 to 105 px
# (4 captured junk-digit rows: tests/fixtures/gun_range/rowcrop_icon_*).
_TEXT_GAP_PX = 10


def text_span(row_mask: np.ndarray) -> "tuple[int, int] | None":
    """First and last column of the widest group of red columns in one text
    row's mask, groups split by gaps over _TEXT_GAP_PX. The captured junk
    reads ("44.5km" for 4.5, "46.5km" for 6.5) all had a small red jet icon
    left of the range, which OCR read as a digit; the text is the wider group."""
    cols = np.nonzero((row_mask > 0).sum(axis=0))[0]
    if cols.size == 0:
        return None
    breaks = np.nonzero(np.diff(cols) - 1 > _TEXT_GAP_PX)[0]
    starts = np.concatenate(([0], breaks + 1))
    ends = np.concatenate((breaks, [cols.size - 1]))
    i = int(np.argmax(cols[ends] - cols[starts]))
    return int(cols[starts[i]]), int(cols[ends[i]])


def _ocr_input(crop: np.ndarray) -> np.ndarray:
    """The crop scaled to _OCR_HEIGHT, as dark red glyphs on white.

    Measured 2026-11-13 on the 12 curated readings in tests/fixtures/gun_range/
    (ranges 0.95 to 14.0 km): this read 12 of 12. Plain grey read 6 (the
    operator's 1.3 km as "04kkkkkkk", 14.0 as "4kk4749"), grey padded white 7,
    red minus green 11. The mask here is looser than the band mask, to keep the
    glyphs' anti-aliased edges after the upscale."""
    scale = _OCR_HEIGHT / float(crop.shape[0])
    img = cv2.resize(crop, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
    img = img[:, :_OCR_WIDTH]
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    mask = (cv2.inRange(hsv, (0, 80, 170), (10, 255, 255))
            | cv2.inRange(hsv, (168, 80, 170), (180, 255, 255)))
    return 255 - mask


@dataclass(frozen=True)
class RangeReading:
    km: "float | None"          # None: no "<n>.<n>km" read
    text: str                   # what the OCR returned, for the log
    cluster: "tuple[float, float]"
    ts: float                   # monotonic time of the picture
    cost_ms: float
    # The colour crop OCR read, for the anomaly capture; not compared.
    crop: "np.ndarray | None" = field(default=None, repr=False, compare=False)


# Junk reads' crops land here (`_save_crop`), at most this many per session.
_CROP_DIR = Path("test_screenshots/unknown_anomalies")
_CROPS_PER_SESSION = 20
_MISS_CROPS_PER_SESSION = 15

# How far apart two readings of one nameplate may be (TargetRangeReader.note).
# Two jets closing head-on at about 1,500 KPH each close 0.83 km/s; 1.0 km/s
# and 0.3 km for a one-digit misread of the tenths are named guesses.
_JUMP_RATE_KMPS = 1.0
_JUMP_SLACK_KM = 0.3
_JUMP_WINDOW_S = 1.5
# Which jumps are rejected. Holding back a reading keeps the one before it, so
# only a jump whose rejection keeps the gun no more allowed than the reading
# would is held back:
# - a DROP of any size: keeping the higher reading keeps the gun off. Live at
#   03:26:00.884 "4.7km" read as "1.7km" between 4.7 and 4.5 and opened the gun
#   at about 4.6 km for 0.1 s.
# - a RISE only to this far out. Replayed over both live runs (1,122 readings)
#   every junk read was a real range with a digit in front (81.2, 21.2, 71.5,
#   44.8, 43.6, 16.1, 13.6), so 10 km or more. Holding back every rise also
#   held back a 3.0 km read against a kept 1.4, which kept the gun allowed.
_JUMP_MIN_KM = 10.0
# Nameplate centres this close are taken as the same target (as in `latest`).
_SAME_PLATE_PX = 200.0


def _same_plate(a: RangeReading, b: RangeReading) -> bool:
    return (abs(a.cluster[0] - b.cluster[0]) <= _SAME_PLATE_PX
            and abs(a.cluster[1] - b.cluster[1]) <= _SAME_PLATE_PX)


def _consistent(a: RangeReading, b: RangeReading) -> bool:
    return abs(a.km - b.km) <= _JUMP_SLACK_KM + _JUMP_RATE_KMPS * abs(a.ts - b.ts)


class TargetRangeReader:
    """Reads the tracked nameplate's range, on its own worker thread.

    The pursuit `submit`s each picture with the nameplate it is tracking and
    asks `latest` for the newest reading of that nameplate. One worker for
    the session (its EasyOCR reader is per thread); `stop` ends it."""

    def __init__(self, reader_factory=None) -> None:
        # The reader is made on the worker thread, on first use. Tests pass a
        # factory; the default is the analyzer's per-thread EasyOCR reader,
        # imported late so the controller does not load EasyOCR (CR-018-15).
        self._reader_factory = reader_factory
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._pending = None            # (frame, cluster, ts)
        # Design 021 Phase 1: the damage text is read on this worker too, by
        # its EasyOCR reader, into `damage_log` (a gun_damage.GunDamageLog).
        self._damage_pending = None     # frame
        self.damage_log = None
        self._latest: "RangeReading | None" = None
        self._rejected: "RangeReading | None" = None
        self._thread: "threading.Thread | None" = None
        self._last_submitted_ts: "float | None" = None
        self.reads = 0
        self.misses = 0
        self.jumps = 0
        self.repeats = 0
        self.strips = 0
        # Instrumentation (2026-11-13): the colour crop behind each junk read,
        # to find what puts a digit in front of the range. Capped per session.
        self._crops_saved = 0
        self._miss_crops_saved = 0

    # -- synchronous -------------------------------------------------------

    def read(self, frame: np.ndarray, cluster: "tuple[float, float]",
             ts: "float | None" = None, reader=None) -> RangeReading:
        """Read the range of the nameplate at `cluster` (absolute px) here,
        on the calling thread."""
        if reader is None:
            reader = self._make_reader()
        ts = time.monotonic() if ts is None else ts
        started = time.perf_counter()
        text = ""
        km = None
        used = None
        tried = []
        for crop in range_row_crops(frame, cluster):
            res = reader.recognize(_ocr_input(crop), detail=1, allowlist=_ALLOWLIST)
            text = res[0][1] if res else ""
            km = parse_range_km(text)
            used = crop
            tried.append(crop)
            if km is not None:
                break
        if km is None and len(tried) > 1:
            # A miss's capture shows every row tried, top to bottom in the
            # order tried: the last alone could not say whether the range row
            # was tried at all (04:31, rows of names and types).
            wmax = max(c.shape[1] for c in tried)
            used = np.vstack([cv2.copyMakeBorder(c, 0, 4, 0, wmax - c.shape[1],
                                                 cv2.BORDER_CONSTANT, value=(40, 40, 40))
                              for c in tried])
        return RangeReading(km=km, text=text, cluster=(float(cluster[0]), float(cluster[1])),
                            ts=ts, cost_ms=(time.perf_counter() - started) * 1000.0,
                            crop=used)

    def _make_reader(self):
        if self._reader_factory is not None:
            return self._reader_factory()
        from .analyzer import _get_thread_ocr_reader
        return _get_thread_ocr_reader()

    # -- worker ------------------------------------------------------------

    def submit(self, frame: np.ndarray, cluster: "tuple[float, float]",
               ts: "float | None" = None) -> None:
        """Hand the worker this picture and return at once; a picture it has
        not started yet is replaced. Starts the worker on first use."""
        if self._thread is None or not self._thread.is_alive():
            self._start()
        ts = time.monotonic() if ts is None else ts
        # A repeated picture keeps its first time (hold_model.PictureClock), so
        # the same time again is the same picture: reading it again only costs
        # CPU, and at 03:47:57 three reads of one picture's "31.2km" (a junk
        # digit in front of a range near 7 km) "agreed" and were kept (`note`).
        if ts == self._last_submitted_ts:
            self.repeats += 1
            return
        self._last_submitted_ts = ts
        with self._lock:
            self._pending = (frame, cluster, ts)
        self._wake.set()

    def submit_damage(self, frame: np.ndarray) -> None:
        """Hand the worker this picture for a damage-text read (Design 021);
        a picture it has not started yet is replaced."""
        if self.damage_log is None:
            return
        if self._thread is None or not self._thread.is_alive():
            self._start()
        with self._lock:
            self._damage_pending = frame
        self._wake.set()

    def latest(self, cluster: "tuple[float, float] | None", max_age_s: float,
               max_shift_px: float = 200.0, now: "float | None" = None) -> "RangeReading | None":
        """The newest reading that found a range (misses are not kept), if it
        is at most `max_age_s` old and was read
        on a nameplate within `max_shift_px` of `cluster` (so a reading of
        the previous target does not open the gun on the next), else None."""
        r = self._latest
        if r is None or cluster is None:
            return None
        now = time.monotonic() if now is None else now
        if now - r.ts > max_age_s:
            return None
        if (abs(r.cluster[0] - cluster[0]) > max_shift_px
                or abs(r.cluster[1] - cluster[1]) > max_shift_px):
            return None
        return r

    def _start(self) -> None:
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="TargetRange")
        self._thread.start()

    def stop(self, timeout: float = 0.5) -> None:
        self._stop.set()
        self._wake.set()
        t = self._thread
        if t is not None and t.is_alive():
            t.join(timeout)
        if self.reads or self.misses or self.jumps:
            logger.info("GUNRANGE: %d reads (%d with a junk first digit dropped), "
                        "%d without a range, %d impossible jumps, "
                        "%d repeated pictures not read",
                        self.reads, self.strips, self.misses, self.jumps, self.repeats)

    def _run(self) -> None:
        reader = None
        while not self._stop.is_set():
            if not self._wake.wait(timeout=1.0):
                continue
            self._wake.clear()
            if self._stop.is_set():
                break
            with self._lock:
                job, self._pending = self._pending, None
                dmg_frame, self._damage_pending = self._damage_pending, None
            if job is None and dmg_frame is None:
                continue
            try:
                if reader is None:
                    reader = self._make_reader()
                    if reader is None:
                        logger.warning("GUNRANGE: no OCR reader; the range gate keeps the gun off")
                        self._stop.wait(timeout=30.0)
                        continue
            except Exception as exc:
                log_suppressed(logger, "target_range_reader", exc)
                continue
            if dmg_frame is not None and self.damage_log is not None:
                try:
                    value, _text, crop = read_damage(dmg_frame, reader)
                    self.damage_log.damage(value, crop)
                except Exception as exc:
                    log_suppressed(logger, "gun_damage_read", exc)
            if job is None:
                continue
            frame, cluster, ts = job
            try:
                r = self.read(frame, cluster, ts, reader=reader)
            except Exception as exc:
                log_suppressed(logger, "target_range_read", exc)
                continue
            verdict = self.note(r)
            logger.debug("GUNRANGE: km=%s text=%r at=(%.0f,%.0f) pic=%.3f cost=%.0fms%s",
                         "-" if r.km is None else "%.2f" % r.km, r.text,
                         r.cluster[0], r.cluster[1], r.ts, r.cost_ms,
                         " jump: kept %.2f" % self._latest.km if verdict == "jump"
                         else " strip: read as %.2f" % self._latest.km if verdict == "strip"
                         else "")
            if (verdict in ("jump", "strip") or (r.km is not None and r.km >= _JUMP_MIN_KM)):
                self._save_crop(r)
            elif verdict == "miss":
                # 04:10-04:14 on CRIMSON 105 of 242 reads missed (other maps
                # 14-17%), as long junk ("845084kk.06"): red scenery in the
                # crop is the guess these pictures are to test.
                self._save_crop(r, miss=True)

    def _save_crop(self, r: RangeReading, miss: bool = False) -> None:
        if r.crop is None:
            return
        if miss:
            if self._miss_crops_saved >= _MISS_CROPS_PER_SESSION:
                return
            self._miss_crops_saved += 1
        else:
            if self._crops_saved >= _CROPS_PER_SESSION:
                return
            self._crops_saved += 1
        try:
            _CROP_DIR.mkdir(parents=True, exist_ok=True)
            name = "gunrange_%s%s_%s.png" % ("miss_" if miss else "",
                                             time.strftime("%Y%m%d_%H%M%S"),
                                           re.sub(r"[^0-9a-z.]", "_", r.text)[:20])
            cv2.imwrite(str(_CROP_DIR / name), r.crop)
        except Exception as exc:
            log_suppressed(logger, "target_range_save_crop", exc)

    def note(self, r: RangeReading) -> str:
        """Keep `r` as the latest reading or not; returns "miss", "kept" or "jump".

        A miss does not replace a good reading: live on 2026-11-13 at 03:04:15
        one unreadable picture ("770") 0.17 s after a 1.8 km read let go of the
        gun, and the next good read was 1.5 km. About one read in seven missed.

        Nor does an impossible jump: at 03:24:11 "1.2km" read with a junk digit
        in front as "81.2km", then "21.2km", between good 1.2 km reads 1.0 s
        apart, and let go of the gun for 0.9 s. And at 03:26:00 "4.7km" read
        as "1.7km" and opened it at about 4.6 km. A reading below the kept
        one, or of _JUMP_MIN_KM or more, of the same nameplate
        within _JUMP_WINDOW_S of the kept one, must lie within _JUMP_SLACK_KM
        plus _JUMP_RATE_KMPS per second of it, unless the reading before it
        was rejected and the two agree: then the kept one was the wrong one
        (a junk read on a new target must not lock the real ones out)."""
        if r.km is None:
            self.misses += 1
            return "miss"
        prev = self._latest
        if (prev is not None and (r.km >= _JUMP_MIN_KM or r.km < prev.km)
                and _same_plate(prev, r)
                and 0.0 <= r.ts - prev.ts <= _JUMP_WINDOW_S
                and not _consistent(prev, r)):
            # The junk digit is systematic: at 04:10:32 a 6.2 km range read
            # "76.0km" and, from the next picture, "75.7km"; the two agreed and
            # were kept. When the text without its first character is a range
            # that fits the kept one, that is the reading.
            if r.km >= _JUMP_MIN_KM:
                stripped = parse_range_km(r.text.replace(" ", "")[1:])
                if stripped is not None:
                    s = replace(r, km=stripped)
                    if _consistent(prev, s):
                        self._latest = s
                        self._rejected = None
                        self.reads += 1
                        self.strips += 1
                        return "strip"
            rej = self._rejected
            # Agreement must come from another picture: a re-read of the same
            # one repeats its OCR error (03:47:57, see `submit`).
            if (rej is None or rej.ts == r.ts or not _same_plate(rej, r)
                    or not _consistent(rej, r)):
                self._rejected = r
                self.jumps += 1
                return "jump"
        self._latest = r
        self._rejected = None
        self.reads += 1
        return "kept"
