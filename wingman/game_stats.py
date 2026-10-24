"""The game's own figures for the match just played (Research 017 step 1).

MetalStorm saves the average frame rate, the share of bad frames and the ping
of its last match in its PlayerPrefs, which Wine keeps in the prefix's
``user.reg``, under ``[Software\\Starform\\Metalstorm]``:
``air-combat-settings--default_lastMatchAvFps_h<hash>``,
``..._lastMatchPercentBadFrames_h<hash>`` and ``..._lastMatchAvgPing_h<hash>``,
each an 8-byte double written as ``hex(4):``. Research 017 asks whether a vision
model sharing the Intel chip slows the game; these are the game's own averages,
one set a match, where the on-screen counter is a glance.

wineserver writes ``user.reg`` from memory when it saves. On 2026-10-19 account
1's last match wrote its figures at 13:32:45 and the file was saved at 13:33:09,
before the game closed. So the watcher stats the file every ``_INTERVAL_S``,
parses it only when its mtime moved, and logs ``GAME STATS:`` when the figures
change: once a match, some seconds after it ends. The first reading of a
session is the previous session's last match, and is logged as such.

Read only: the section also holds the login and private chat, and nothing but
the three figures is taken from it. Never raises. Standard library only.
"""

import logging
import os
import re
import struct
import time
from typing import NamedTuple

logger = logging.getLogger(__name__)

# Set by the run targets (Makefile) to the prefix the game runs in.
PREFIX_ENV = "WINGMAN_WINE_PREFIX"
_SECTION = "[Software\\\\Starform\\\\Metalstorm]"
_KEYS = (("avg_fps", "lastMatchAvFps"),
         ("bad_frames", "lastMatchPercentBadFrames"),
         ("ping_ms", "lastMatchAvgPing"))
# A stat a quarter-minute: the file changes at most every few tens of seconds.
_INTERVAL_S = 15.0


class LastMatchStats(NamedTuple):
    avg_fps: float
    bad_frames: float      # as the game stores it (0.010 seen; units unknown)
    ping_ms: float


def parse_last_match_stats(text: str) -> "LastMatchStats | None":
    """The three figures from a ``user.reg``'s text; None when any is missing."""
    start = text.find(_SECTION)
    if start < 0:
        return None
    end = text.find("\n[", start + len(_SECTION))
    section = text[start:end if end >= 0 else len(text)]
    section = re.sub(r"\\\n\s*", "", section)       # join continued values
    values = {}
    for field, key in _KEYS:
        m = re.search(r'^"air-combat-settings--default_' + key
                      + r'(?:_h\d+)?"=hex(?:\(\d+\))?:([0-9a-fA-F,]+)$',
                      section, re.MULTILINE)
        if m is None:
            return None
        raw = bytes(int(b, 16) for b in m.group(1).split(",") if b)
        if len(raw) != 8:
            return None
        values[field] = struct.unpack("<d", raw)[0]
    return LastMatchStats(**values)


class GameStatsWatcher:
    """Logs the game's figures for each match as they reach ``user.reg``."""

    def __init__(self, prefix: "str | None", interval_s: float = _INTERVAL_S,
                 clock=time.monotonic) -> None:
        self._path = os.path.join(prefix, "user.reg") if prefix else None
        self._interval_s = float(interval_s)
        self._clock = clock
        self._next_due = 0.0
        self._mtime = None
        self._last: "LastMatchStats | None" = None
        self._warned = False

    @classmethod
    def from_env(cls) -> "GameStatsWatcher":
        return cls(os.environ.get(PREFIX_ENV) or None)

    @property
    def enabled(self) -> bool:
        return self._path is not None

    @property
    def last(self) -> "LastMatchStats | None":
        return self._last

    def maybe_poll(self) -> "LastMatchStats | None":
        """The figures when they changed since the last reading, else None."""
        if self._path is None:
            return None
        now = self._clock()
        if now < self._next_due:
            return None
        self._next_due = now + self._interval_s
        try:
            mtime = os.stat(self._path).st_mtime
            if mtime == self._mtime:
                return None
            self._mtime = mtime
            with open(self._path, "r", encoding="utf-8", errors="replace") as fh:
                stats = parse_last_match_stats(fh.read())
        except Exception as exc:
            if not self._warned:
                self._warned = True
                logger.warning("GAME STATS: cannot read %s (%s); not logged this session",
                               self._path, exc)
            return None
        if stats is None or stats == self._last:
            return None
        which = "before this session" if self._last is None else "last match"
        self._last = stats
        logger.info("GAME STATS: %s avg_fps=%.1f bad_frames=%.4f ping_ms=%.0f "
                    "(the game's own figures, Research 017)",
                    which, stats.avg_fps, stats.bad_frames, stats.ping_ms)
        return stats
