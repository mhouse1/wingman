"""Failures that a guarded handler swallows, made visible at INFO. CR-018-17.

Per-tick reflexes (stall prevention, terrain detection, the emergency climb, the
afterburner evade) run inside ``except Exception`` so that one failure cannot
take the main loop down. Logging those at DEBUG only meant that a reflex which
raised on every tick looked, in an INFO session, exactly like a reflex that was
working: the log said nothing while the reflex was dead.

``log_suppressed`` keeps the handler non-fatal and makes it loud once: a
WARNING with the traceback on the first failure of each name, again on every
``WARN_EVERY``-th failure, and DEBUG in between. The per-name counts go into the
session summary.
"""

import logging
import threading

# At the 1.5 s tick a handler that fails every tick warns about every 2.5 min.
WARN_EVERY = 100

_counts: "dict[str, int]" = {}
_lock = threading.Lock()


def log_suppressed(log: logging.Logger, name: str, exc: BaseException) -> None:
    """Record a swallowed failure of ``name`` and log it at the right level.

    Call from inside the ``except`` block that swallows ``exc``.
    """
    with _lock:
        n = _counts.get(name, 0) + 1
        _counts[name] = n
    if n == 1 or n % WARN_EVERY == 0:
        log.warning("%s failed (%d this session; handler continues)", name, n,
                    exc_info=exc)
    else:
        log.debug("%s failed (%d this session)", name, n, exc_info=exc)


def suppressed_counts() -> "dict[str, int]":
    """Failures per name so far this session, for the session summary."""
    with _lock:
        return dict(_counts)


def reset_suppressed_counts() -> None:
    """Clear the counts. For tests."""
    with _lock:
        _counts.clear()
