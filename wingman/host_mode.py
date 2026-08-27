"""Report which foundry host mode wingman is running under (foundry HLDD 001).

VEDA and Ptolemy can stand their lab services down — Jenkins, Redmine, and on
Ptolemy also Frigate and Jellyfin — to give the desktop the machine back. That
state is **TRIAL**; the normal state with everything up is **R&D mode**.

It matters to wingman because co-tenant load is invisible in today's results and
very visible in tomorrow's: a session at load average 9.4 still finished 8/8
missions on the 1.5 s tick (2026-08-26), but the frame-bounded design in
HLDD 008 has no such slack. Knowing which mode a session ran under is the
difference between a comparable measurement and an anecdote.

foundry publishes this as a **supported interface**, not something to scrape:

    rd-mode status --json

Contract rules this module honours, taken from foundry HLDD 001:

  * ``mode`` is derived from the running containers on every call, so it cannot
    go stale. The state file is explicitly *not* an API and is never read here.
  * All five values are handled: ``rd``, ``trial``, ``mixed``, ``none``,
    ``unknown``.
  * **``unknown`` is not ``trial``.** A caller outside the ``docker`` group gets
    a permission error while systemd still reports the daemon active; read
    naively every stack looks down and the host looks stood-down when it is not.
    That case is reported as ``unknown`` with ``docker_reachable: false``, and
    treating it as TRIAL would reintroduce the exact bug the field prevents.
  * Exit status is 0 whenever JSON was produced — read ``mode``, not ``$?``.
  * ``schema`` is checked, and a version this code does not know is refused
    politely rather than parsed hopefully.

Never raises, and never blocks startup for more than ``timeout_s``: a reporting
convenience must not be able to stop wingman flying.
"""

import json
import logging
import shutil
import subprocess

logger = logging.getLogger(__name__)

SCHEMA = 1
_CMD = "rd-mode"
_TIMEOUT_S = 5.0

# TRIAL's own icon is red — it is a lockout from the lab's point of view. From
# wingman's point of view it is the opposite: the services are down and the
# machine is ours. Green here is deliberate, not a mismatch.
_GREEN = "\033[92m"
_YELLOW = "\033[93m"
_RESET = "\033[0m"

_BANNER_WIDTH = 68


def query(timeout_s: float = _TIMEOUT_S) -> "dict | None":
    """Return the parsed `rd-mode status --json` payload, or None.

    None means "no answer" — the tool is absent, timed out, or produced
    something this code will not interpret. It never means "not in TRIAL".
    """
    exe = shutil.which(_CMD)
    if exe is None:
        logger.debug("host mode: %s not on PATH — not a foundry-managed host", _CMD)
        return None
    try:
        proc = subprocess.run([exe, "status", "--json"], capture_output=True,
                              text=True, timeout=timeout_s)
    except subprocess.TimeoutExpired:
        logger.warning("host mode: %s timed out after %.0fs — continuing", _CMD, timeout_s)
        return None
    except OSError as e:
        logger.debug("host mode: %s could not run: %s", _CMD, e)
        return None

    # Exit status is 0 whenever the JSON was produced; the payload is the answer.
    try:
        data = json.loads(proc.stdout)
    except (ValueError, TypeError):
        logger.debug("host mode: no JSON on stdout (rc=%s)", proc.returncode)
        return None
    if not isinstance(data, dict):
        return None

    schema = data.get("schema")
    if schema != SCHEMA:
        logger.warning(
            "host mode: rd-mode reports schema %r, this build understands %d — "
            "not interpreting it (foundry HLDD 001)", schema, SCHEMA)
        return None
    return data


def _line(text: str = "") -> str:
    return "║ " + text.ljust(_BANNER_WIDTH - 4) + " ║"


def banner(status: dict) -> "list[str]":
    """The TRIAL banner, as lines. Empty for any other mode."""
    if status.get("mode") != "trial":
        return []
    down = [s.get("label", "?") for s in status.get("stacks") or []
            if s.get("state") == "down"]
    top = "╔" + "═" * (_BANNER_WIDTH - 2) + "╗"
    bottom = "╚" + "═" * (_BANNER_WIDTH - 2) + "╝"
    lines = [
        top,
        _line(),
        _line("  T R I A L   M O D E   E N G A G E D"),
        _line(),
        _line("  Lab services are stood down. This machine is yours."),
    ]
    if down:
        lines.append(_line(f"  Down: {', '.join(down)}"))
    if status.get("swap_used_mb"):
        lines.append(_line(f"  Swap still in use: {status['swap_used_mb']} MB"))
    since = status.get("since")
    if since:
        lines.append(_line(f"  Since: {since}"))
    lines += [
        _line(),
        _line("  TRIAL latches — nothing restores it. Re-enable R&D mode"),
        _line("  when you are done, or Jenkins stays down (foundry ADR 021)."),
        _line(),
        bottom,
    ]
    return lines


def log_host_mode(timeout_s: float = _TIMEOUT_S) -> "str | None":
    """Announce the host mode at startup. Returns the mode, or None.

    TRIAL gets a banner because it latches and is easy to forget; every other
    mode gets one line, because a banner nobody needs is a banner nobody reads.
    """
    try:
        status = query(timeout_s=timeout_s)
        if status is None:
            return None
        mode = status.get("mode")

        if mode == "trial":
            for ln in banner(status):
                logger.info("%s%s%s", _GREEN, ln, _RESET)
            return mode

        if mode == "rd":
            logger.info("Host mode: R&D — lab services up. Run TRIAL before a "
                        "measurement session (foundry ADR 021).")
        elif mode == "mixed":
            logger.warning("%sHost mode: MIXED — a stack was changed outside "
                           "rd-mode. Session conditions are not reproducible.%s",
                           _YELLOW, _RESET)
        elif mode == "unknown":
            logger.warning(
                "%sHost mode: UNKNOWN — could not determine it%s%s. Not assuming "
                "TRIAL (foundry HLDD 001).%s", _YELLOW,
                "" if status.get("docker_reachable", True) else
                " (docker unreachable from this user; is it in the docker group?)",
                "", _RESET)
        elif mode == "none":
            logger.info("Host mode: none — no stacks configured on this host.")
        else:
            logger.debug("host mode: unrecognised value %r", mode)

        if status.get("transam_active"):
            logger.warning("%sTRANSAM is active — this host has no desktop "
                           "session (foundry ADR 013).%s", _YELLOW, _RESET)
        return mode
    except Exception as e:      # a reporting convenience must never break startup
        logger.debug("host mode: reporting failed: %s", e)
        return None
