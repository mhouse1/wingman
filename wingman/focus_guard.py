"""Suppress key and mouse injection when the game does not have focus (ADR 098).

Wingman types into whatever window is focused. The pre-existing capture-staleness
check cannot catch an alt-tab, because mss keeps grabbing the monitor fine while
another window holds focus — frames arrive, and wingman keeps typing. On
2026-08-28 the operator's message reached them as "tryi auganw": the stray `i`
and `w` were NOSE_UP_KEY and WINGSWEEP_KEY, injected into their editor.

Identity is the game's *Wine session*, never a window title. See ADR 098 for the
three traps that establishes: the game window is titled "Wine Desktop", any
window may carry "Metalstorm" in its title, and the managed window is owned by
explorer.exe rather than the game binary.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import time

logger = logging.getLogger(__name__)

GAME_PROCESS_NAME = "Metalstorm.exe"

FOCUS_GAME = "game"
FOCUS_OTHER = "other"
FOCUS_UNKNOWN = "unknown"

_WID = re.compile(r"(0x[0-9a-fA-F]+)")
_XFOCUS = re.compile(r"focus:\s+window (0x[0-9a-fA-F]+)")


def _ppid_of(pid: int) -> "int | None":
    """Parent pid from /proc/<pid>/stat. Never raises."""
    try:
        with open(f"/proc/{pid}/stat") as fh:
            # comm may contain spaces and parentheses; fields after the final
            # ')' are positional.
            return int(fh.read().rsplit(")", 1)[1].split()[1])
    except (OSError, ValueError, IndexError):
        return None


def find_game_pids(process_name: str = GAME_PROCESS_NAME) -> "set[int]":
    """PIDs whose `comm` matches. Never raises. Mirrors ADR 094's scan."""
    needle = process_name[:15]          # kernel truncates comm to 15 chars
    pids: set[int] = set()
    try:
        entries = os.listdir("/proc")
    except OSError:
        return pids
    for entry in entries:
        if not entry.isdigit():
            continue
        try:
            with open(f"/proc/{entry}/comm") as fh:
                if fh.read().strip().startswith(needle):
                    pids.add(int(entry))
        except (OSError, ValueError):
            continue
    return pids


def game_session_pids(process_name: str = GAME_PROCESS_NAME) -> "set[int]":
    """Every PID in the game's Wine session, not just the game binary.

    ADR 098 D2. The WM-managed window is owned by explorer.exe, a sibling of the
    game under the Proton launcher, so matching the binary alone suppresses
    every keypress while the game is fully focused.
    """
    game = find_game_pids(process_name)
    if not game:
        return set()
    roots = {pp for pp in (_ppid_of(g) for g in game) if pp and pp > 1}
    if not roots:
        return set(game)
    session = set(game)
    try:
        entries = [int(e) for e in os.listdir("/proc") if e.isdigit()]
    except OSError:
        return session
    for pid in entries:
        cur, depth = pid, 0
        # Depth cap: a racing or malformed /proc must not loop forever.
        while cur and cur > 1 and depth < 12:
            if cur in roots:
                session.add(pid)
                break
            cur = _ppid_of(cur)
            depth += 1
    return session


def config_for_display(cfg, injection_display):
    """focus_guard config aimed at the display injection actually targets.

    ADR 099 moves capture and injection to a nested display while the operator's
    DISPLAY stays put. The guard resolves its own display from
    `cfg["display"] or os.environ["DISPLAY"]`, so left alone it interrogates the
    operator's screen, finds no game window there, concludes "not the game" and
    suppresses every key and click - the guard silently disabling the very thing
    it exists to protect. Observed 2026-08-29: 10 suppressed clicks and a
    154 s stall in GAME_WAITING before matchmaking fell back.

    An explicit `focus_guard.display` in config still wins, so the operator can
    always pin it.
    """
    out = dict(cfg or {})
    if injection_display and not out.get("display"):
        out["display"] = injection_display
    return out


class FocusGuard:
    """Answers "does the game have focus?" for the injection path.

    Off unless explicitly enabled (ADR 098 D6). Never raises: every failure
    resolves to FOCUS_UNKNOWN, and D4 decides what that means.
    """

    def __init__(self, cfg: "dict | None" = None, clock=time.monotonic):
        cfg = cfg or {}
        self._enabled = bool(cfg.get("enabled", False))
        self._ttl_s = float(cfg.get("ttl_s", 1.0))
        self._session_ttl_s = float(cfg.get("session_ttl_s", 5.0))
        # "inject" (default) or "suppress" — see ADR 098 D4.
        self._on_unknown = str(cfg.get("on_unknown", "inject")).lower()
        self._display = cfg.get("display") or os.environ.get("DISPLAY", ":0")
        self._process_name = str(cfg.get("process_name", GAME_PROCESS_NAME))
        self._clock = clock

        self._focus_at = None
        self._focus = FOCUS_UNKNOWN
        self._session_at = None
        self._session: set[int] = set()
        self.suppressed_total = 0
        self.unknown_total = 0
        self._last_other = None

    @property
    def enabled(self) -> bool:
        return self._enabled

    def _run(self, argv) -> "str | None":
        try:
            out = subprocess.run(argv, env=dict(os.environ, DISPLAY=self._display),
                                 capture_output=True, text=True, timeout=2.0)
            return out.stdout if out.returncode == 0 else None
        except Exception:                    # noqa: BLE001 - guard must not raise
            return None

    def _prop(self, wid: str, name: str) -> "str | None":
        out = self._run(["xprop", "-id", wid, name])
        if not out or "=" not in out:
            return None
        return out.split("=", 1)[1].strip() or None

    def _owner_pid(self, wid: str) -> "int | None":
        raw = self._prop(wid, "_NET_WM_PID")
        return int(raw) if raw and raw.isdigit() else None

    def _title(self, wid: str) -> "str | None":
        raw = self._prop(wid, "_NET_WM_NAME") or self._prop(wid, "WM_NAME")
        if raw and raw.startswith('"') and raw.endswith('"'):
            return raw[1:-1]
        return raw

    def _session_pids(self) -> "set[int]":
        now = self._clock()
        if self._session_at is None or (now - self._session_at) >= self._session_ttl_s:
            self._session = game_session_pids(self._process_name)
            self._session_at = now
        return self._session

    def _focused_windows(self) -> "list[str]":
        """Both signals. ADR 098 D3: either naming a session process is enough."""
        wids = []
        out = self._run(["xprop", "-root", "_NET_ACTIVE_WINDOW"])
        if out:
            m = _WID.search(out)
            if m and m.group(1) != "0x0":
                wids.append(m.group(1))
        out = self._run(["xdpyinfo"])
        if out:
            m = _XFOCUS.search(out)
            # "window 0x0" / PointerRoot means no X client holds focus.
            if m and m.group(1) != "0x0":
                wids.append(m.group(1))
        return wids

    def _probe(self) -> str:
        session = self._session_pids()
        if not session:
            return FOCUS_UNKNOWN            # game not running — nothing to protect
        wids = self._focused_windows()
        if not wids:
            return FOCUS_UNKNOWN
        for wid in wids:
            if self._owner_pid(wid) in session:
                return FOCUS_GAME
        self._last_other = self._title(wids[0])
        return FOCUS_OTHER

    def focus_state(self) -> str:
        """Cached focus verdict. ADR 098 D5: at most one probe per ttl_s."""
        if not self._enabled:
            return FOCUS_GAME
        now = self._clock()
        if self._focus_at is None or (now - self._focus_at) >= self._ttl_s:
            try:
                self._focus = self._probe()
            except Exception as e:           # noqa: BLE001 - guard must not raise
                logger.debug("FocusGuard: probe failed: %s", e)
                self._focus = FOCUS_UNKNOWN
            self._focus_at = now
        return self._focus

    def may_inject(self, what: str = "key") -> bool:
        """True when injection is allowed. Never raises."""
        if not self._enabled:
            return True
        state = self.focus_state()
        if state == FOCUS_GAME:
            return True
        if state == FOCUS_UNKNOWN:
            self.unknown_total += 1
            if self._on_unknown == "suppress":
                self.suppressed_total += 1
                logger.warning("FocusGuard: focus unresolved - suppressing %s "
                               "(on_unknown=suppress)", what)
                return False
            # Default: a guard that cannot tell must not silently kill the run.
            if self.unknown_total in (1, 10, 100) or self.unknown_total % 1000 == 0:
                logger.warning("FocusGuard: focus unresolved (%d so far) - "
                               "injecting anyway (ADR 098 D4)", self.unknown_total)
            return True
        self.suppressed_total += 1
        if self.suppressed_total in (1, 10, 100) or self.suppressed_total % 500 == 0:
            logger.warning("FocusGuard: game does not have focus (%r) - "
                           "suppressed %s injection (%d so far)",
                           self._last_other, what, self.suppressed_total)
        return False

    def summary(self) -> str:
        return (f"FocusGuard: suppressed={self.suppressed_total} "
                f"unknown={self.unknown_total} enabled={self._enabled}")
