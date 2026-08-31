"""Close MetalStorm at the end of a finish-round-then-exit (ADR 094).

Signals the process rather than driving the in-game Exit-to-Desktop dialog.
`make launch-game` already SIGTERMs the client before every relaunch, so this is
a constantly exercised path; the round has completed and progression lives on
the backend, so nothing is unsaved. Driving the menu would mean clicking an
*uncalibrated* button — only that modal's Cancel is calibrated
(`STALL_EXIT_TO_DESKTOP`), with Exit roughly 130px beside it, and an
uncalibrated click at a guessed offset is how Anomaly 001 stranded a session for
110 minutes.

Processes are found through `/proc`, not `pkill -f`. That is not fastidiousness:
the Makefile splits its own pattern through a shell variable specifically so
`pkill -f Metalstorm.exe` cannot match and kill the recipe's own shell. A
`/proc` lookup cannot match wingman itself and needs no such trick.

Never raises. A stop command that can itself fail to stop is worse than one that
occasionally leaves a window open, so every failure is logged and swallowed —
the caller's exit proceeds regardless (ADR 094 V7).
"""

import logging
import os
import signal
import time

logger = logging.getLogger(__name__)

_PROC = "/proc"
_DEFAULT_GRACE_S = 5.0        # matches launch-game's settle before relaunch


def find_game_pids(process_name: str = "Metalstorm.exe") -> "list[int]":
    """PIDs whose `comm` matches, by /proc scan. Never raises."""
    pids = []
    try:
        entries = os.listdir(_PROC)
    except OSError as e:
        logger.warning("Game shutdown: cannot read %s: %s", _PROC, e)
        return pids
    # comm is truncated to 15 characters by the kernel, so match on the prefix
    # rather than the full name — "Metalstorm.exe" fits, but a longer future
    # binary name would not, and silently matching nothing is the bad failure.
    needle = process_name[:15]
    for entry in entries:
        if not entry.isdigit():
            continue
        try:
            with open(f"{_PROC}/{entry}/comm", "r", encoding="utf-8") as fh:
                if fh.read().strip() == needle:
                    pids.append(int(entry))
        except OSError:
            continue          # the process exited between listdir and open
    return pids


def find_nested_display_pids(display: str) -> "list[int]":
    """PIDs of the Xwayland server backing `display`. Never raises.

    ADR 099. The match is deliberately EXACT on argv[1], not a substring of the
    command line: the operator's own session is served by `Xwayland :0 ...`, and
    a loose match that caught it would take their entire desktop down. `:3` is
    also a substring of `:30`, so even a display-number match must be whole.
    """
    pids = []
    if not display:
        return pids
    try:
        entries = os.listdir(_PROC)
    except OSError as e:
        logger.warning("Nested display: cannot read %s: %s", _PROC, e)
        return pids
    for entry in entries:
        if not entry.isdigit():
            continue
        try:
            with open(f"{_PROC}/{entry}/comm", "r", encoding="utf-8") as fh:
                if fh.read().strip() != "Xwayland":
                    continue
            with open(f"{_PROC}/{entry}/cmdline", "rb") as fh:
                argv = fh.read().split(b"\0")
        except OSError:
            continue          # exited between listdir and open
        if len(argv) >= 2 and argv[1].decode("utf-8", "replace") == display:
            pids.append(int(entry))
    return pids


def close_nested_display(display: str,
                         grace_s: float = _DEFAULT_GRACE_S,
                         clock=time.monotonic, sleep=time.sleep) -> dict:
    """Tear down the nested Xwayland serving `display` (ADR 099).

    Called only after the game itself is closed. The server exists solely to
    host the game, so leaving it behind strands an empty black "Xwayland on :N"
    window on the operator's desktop — the visible residue of a session that
    otherwise ended cleanly.

    Never raises: a stop path must never fail to stop.
    """
    result = {"found": 0, "terminated": [], "killed": [], "failed": [], "ok": True}
    try:
        pids = find_nested_display_pids(display)
        result["found"] = len(pids)
        if not pids:
            logger.info("Nested display: no Xwayland found for %s — nothing to close",
                        display)
            return result
        logger.info("Nested display: closing Xwayland for %s (pid(s): %s)",
                    display, ", ".join(str(p) for p in pids))
        for pid in pids:
            try:
                os.kill(pid, signal.SIGTERM)
                result["terminated"].append(pid)
            except ProcessLookupError:
                pass
            except OSError as e:
                logger.warning("Nested display: SIGTERM to %d failed: %s", pid, e)
                result["failed"].append(pid)
                result["ok"] = False
        deadline = clock() + max(0.0, grace_s)
        while clock() < deadline:
            if not any(_alive(p) for p in pids):
                break
            sleep(0.25)
        for pid in pids:
            if not _alive(pid):
                continue
            logger.warning("Nested display: %d survived %.1fs — SIGKILL", pid, grace_s)
            try:
                os.kill(pid, signal.SIGKILL)
                result["killed"].append(pid)
            except ProcessLookupError:
                pass
            except OSError as e:
                logger.warning("Nested display: SIGKILL to %d failed: %s", pid, e)
                result["failed"].append(pid)
                result["ok"] = False
        if [p for p in pids if _alive(p)]:
            result["ok"] = False
        else:
            logger.info("Nested display: %s closed", display)
    except Exception as e:
        logger.warning("Nested display: close failed (%s: %s) — exiting anyway",
                       type(e).__name__, e)
        result["ok"] = False
    return result


def _alive(pid: int) -> bool:
    return os.path.isdir(f"{_PROC}/{pid}")


def close_game(process_name: str = "Metalstorm.exe",
               grace_s: float = _DEFAULT_GRACE_S,
               clock=time.monotonic, sleep=time.sleep) -> dict:
    """SIGTERM the game, then SIGKILL whatever survives the grace window.

    Returns a summary dict for logging and tests. Never raises (ADR 094 V7).
    """
    result = {"found": 0, "terminated": [], "killed": [], "failed": [], "ok": True}
    try:
        pids = find_game_pids(process_name)
        result["found"] = len(pids)
        if not pids:
            logger.info("Game shutdown: no %s process found — nothing to close",
                        process_name)
            return result

        logger.info("Game shutdown: closing %s (%d process(es): %s)",
                    process_name, len(pids), ", ".join(str(p) for p in pids))
        for pid in pids:
            try:
                os.kill(pid, signal.SIGTERM)
                result["terminated"].append(pid)
            except ProcessLookupError:
                pass                      # already gone; that is success
            except OSError as e:
                logger.warning("Game shutdown: SIGTERM to %d failed: %s", pid, e)
                result["failed"].append(pid)
                result["ok"] = False

        # Wait out the grace window, returning early once everything is gone.
        deadline = clock() + max(0.0, grace_s)
        while clock() < deadline:
            if not any(_alive(p) for p in pids):
                break
            sleep(0.25)

        for pid in pids:
            if not _alive(pid):
                continue
            logger.warning("Game shutdown: %d survived %.1fs — SIGKILL", pid, grace_s)
            try:
                os.kill(pid, signal.SIGKILL)
                result["killed"].append(pid)
            except ProcessLookupError:
                pass
            except OSError as e:
                logger.warning("Game shutdown: SIGKILL to %d failed: %s", pid, e)
                result["failed"].append(pid)
                result["ok"] = False

        survivors = [p for p in pids if _alive(p)]
        if survivors:
            logger.warning("Game shutdown: %s still running after SIGKILL — "
                           "leaving them", survivors)
            result["ok"] = False
        else:
            logger.info("Game shutdown: %s closed", process_name)
    except Exception as e:                # a stop path must never fail to stop
        logger.warning("Game shutdown: failed (%s: %s) — exiting anyway",
                       type(e).__name__, e)
        result["ok"] = False
    return result
