"""Heap census — the discriminating measurement Performance 008 asks for.

Performance 008 established that the long-session growth is **live allocation,
not fragmentation** (`mi_use` climbs ~950 MB/h while arena-retained memory stays
bounded at a few hundred MB). It then names one remaining fork, and insists it
be measured rather than guessed:

  * **Python-side retention** — a container holding frames, crops, or tensors.
  * **Native retention** — memory held by torch or OpenCV below the Python
    object graph, invisible to `gc`.

This module measures both at once and prints them side by side, so the fork
resolves from one session rather than two:

    HEAPCENSUS ... py_mb=412 d_py=+38 tm_mb=520 d_tm=+61 mi_use_mb=3639

**`tm_mb` (tracemalloc) is the lane to trust for payload.** Measured 2026-08-23:
64 MB of retained `ndarray` shows up as exactly 64 MB, because numpy registers
its data allocations with tracemalloc; `bytearray` likewise. It also carries a
traceback, so the by-site table names the file and line that is retaining.

**`py_mb` (the gc census) is blind to that same 64 MB, by design of CPython.**
A non-object-dtype ndarray cannot take part in a reference cycle, so numpy
leaves it untracked and `gc.get_objects()` never returns it — nor `bytes`, nor
`bytearray`. Taking the gc census at face value would report a flat heap while
gigabytes of frames accumulated, and would have sent this investigation to the
"native" branch for a leak that is squarely Python-side. So the census also
scans the *contents* of tracked containers and attributes any untracked payload
it finds there (see `_scan_payload`) — which is what makes the by-type table
name the container holding the frames rather than shrug.

  * `mi_use_mb` is the ground truth from `mallinfo2`, carried through from the
    ResourceSampler.

The reading that matters:

    tm_mb climbing with mi_use     -> Python-allocated, numpy included. The
                                      by-site table names the file and line.
    tm_mb FLAT while mi_use climbs -> genuinely native: torch/OpenCV C++ buffers
                                      that bypass the Python allocator. The
                                      Python tables will look innocent and are
                                      not where to keep looking.

**Off by default.** `gc.get_objects()` walks the entire heap and blocks the main
loop for as long as that takes; tracemalloc adds a per-allocation cost and holds
its own traces. This is a diagnostic to switch on for a leak-hunting session,
not something to carry in a normal run. `census_ms` in the output reports the
cost so it is never invisible.

Note that ADR 090's memory guard suppresses the very curve this measures — a
leak-hunting session must raise `memory_guard.soft_limit_mb` as Performance 008
warns, or the session ends before the signal is clear.
"""

import gc
import logging
import sys
import time
import tracemalloc
import warnings

logger = logging.getLogger(__name__)

# Depth of the captured traceback. 1 names the allocating line, which is what
# the by-site table needs; deeper traces multiply tracemalloc's own retention,
# and this runs on a process that is already the subject of a leak hunt.
_DEFAULT_DEPTH = 2
_DEFAULT_INTERVAL_S = 600.0
_DEFAULT_TOP_N = 12

# Types whose payload lives outside the Python object header. sys.getsizeof
# reports only the header for these, understating an image or a tensor by
# orders of magnitude, so ask the object instead.
_NBYTES_TYPES = ("ndarray", "Tensor", "Parameter")


def _sizeof(obj) -> int:
    """Best-effort byte size, never raising.

    numpy/torch payloads are asked for their real extent; everything else falls
    back to the object header. Shallow by design — summing transitively would
    double-count every shared buffer on the heap.
    """
    try:
        name = type(obj).__name__
        if name in _NBYTES_TYPES:
            nbytes = getattr(obj, "nbytes", None)
            if isinstance(nbytes, int):
                return nbytes
            # torch tensors predate .nbytes on some versions
            nel, esz = getattr(obj, "nelement", None), getattr(obj, "element_size", None)
            if callable(nel) and callable(esz):
                return int(nel()) * int(esz())
        return sys.getsizeof(obj)
    except Exception:
        return 0


# Payload types whose bytes live outside the object header AND which CPython
# does not gc-track (they cannot form reference cycles). They are invisible to
# gc.get_objects(); the only way to attribute them is through the tracked
# container that holds them.
_PAYLOAD_TYPES = frozenset({
    "ndarray", "Tensor", "Parameter", "bytes", "bytearray", "memoryview", "str",
})


def _iter_contents(obj):
    """Yield the elements a tracked container holds, or nothing.

    Deliberately shallow and total: any container that objects to being walked
    (mutated under us, hostile __iter__) yields nothing rather than raising.
    """
    try:
        if isinstance(obj, dict):
            return list(obj.values())
        if isinstance(obj, (list, tuple, set, frozenset)):
            return list(obj)
        d = getattr(obj, "__dict__", None)
        if isinstance(d, dict):
            return list(d.values())
    except Exception:
        pass
    return ()


def _qualname(obj) -> str:
    try:
        t = type(obj)
        mod = getattr(t, "__module__", "") or ""
        # Keep numpy.ndarray and torch.Tensor distinguishable from lookalikes,
        # but do not let builtins carry a noisy "builtins." prefix.
        return t.__name__ if mod in ("builtins", "") else f"{mod.split('.')[0]}.{t.__name__}"
    except Exception:
        return "<unknown>"


class HeapCensus:
    """Self-throttled heap census. Call `maybe_census()` every tick.

    Mirrors ResourceSampler's contract deliberately: self-throttled, returns the
    emitted line or None, and never raises into the main loop.
    """

    def __init__(self, cfg: "dict | None" = None, clock=time.time):
        cfg = cfg or {}
        self._enabled = bool(cfg.get("enabled", False))
        self._interval = float(cfg.get("interval_s", _DEFAULT_INTERVAL_S))
        self._top_n = int(cfg.get("top_n", _DEFAULT_TOP_N))
        self._use_tracemalloc = bool(cfg.get("tracemalloc", True))
        self._depth = int(cfg.get("tracemalloc_depth", _DEFAULT_DEPTH))
        self._clock = clock

        self._start = clock()
        self._last = 0.0
        self._prev_snapshot = None
        self._prev_by_type: dict = {}
        self._prev_py_mb = None
        self._prev_tm_mb = None
        self._started_tracemalloc = False

        if self._enabled and self._use_tracemalloc:
            try:
                if not tracemalloc.is_tracing():
                    tracemalloc.start(self._depth)
                    self._started_tracemalloc = True
            except Exception as e:
                logger.warning("HeapCensus: tracemalloc unavailable: %s", e)
                self._use_tracemalloc = False

    @property
    def enabled(self) -> bool:
        return self._enabled

    def maybe_census(self, now: "float | None" = None, mi_use_mb=None) -> "str | None":
        """Emit a census if the interval has elapsed. Returns the block, or None."""
        if not self._enabled:
            return None
        now = self._clock() if now is None else now
        if self._last and (now - self._last) < self._interval:
            return None
        self._last = now
        try:
            return self._census(now, mi_use_mb)
        except Exception as e:  # a diagnostic must never break the loop
            logger.warning("HeapCensus: census failed: %s", e)
            return None

    def stop(self) -> None:
        """Stop tracing if we were the one who started it."""
        try:
            if self._started_tracemalloc and tracemalloc.is_tracing():
                tracemalloc.stop()
                self._started_tracemalloc = False
        except Exception:
            pass

    def _census(self, now: float, mi_use_mb) -> str:
        t0 = time.perf_counter()

        # Introspecting arbitrary heap objects fires other libraries' lazy
        # deprecation shims — torch/typing_extensions raise FutureWarning from
        # inside `getattr(obj, "__dict__")` on objects this code merely looked
        # at. Those warnings say nothing about wingman and would land in the log
        # of a leak-hunting session as pure noise. The suppression is global for
        # its duration (Python has no per-thread filters), so it is scoped as
        # tightly as possible around the walk and nothing else.
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            return self._census_locked(now, mi_use_mb, t0)

    def _census_locked(self, now: float, mi_use_mb, t0: float) -> str:

        by_type: dict = {}
        objs = gc.get_objects()
        total_bytes = n_objects = 0
        # Payloads are reached through their holder, so the same array seen from
        # two containers must be counted once. Ids only — never the objects, or
        # the census would itself pin the heap it is measuring.
        seen_payload: set = set()
        try:
            for obj in objs:
                name = _qualname(obj)
                size = _sizeof(obj)
                cnt, tot = by_type.get(name, (0, 0))
                by_type[name] = (cnt + 1, tot + size)
                total_bytes += size
                n_objects += 1

                for el in _iter_contents(obj):
                    try:
                        if type(el).__name__ not in _PAYLOAD_TYPES:
                            continue
                        if gc.is_tracked(el):
                            continue  # it gets its own turn in this loop
                        if id(el) in seen_payload:
                            continue
                        seen_payload.add(id(el))
                    except Exception:
                        continue
                    p_name, p_size = _qualname(el), _sizeof(el)
                    p_cnt, p_tot = by_type.get(p_name, (0, 0))
                    by_type[p_name] = (p_cnt + 1, p_tot + p_size)
                    total_bytes += p_size
                    n_objects += 1
        finally:
            # Do not leave the census itself holding a reference to every object
            # on the heap while the rest of this method runs.
            del objs

        py_mb = total_bytes / 1048576.0
        d_py = "n/a" if self._prev_py_mb is None else f"{py_mb - self._prev_py_mb:+.0f}"

        tm_mb = None
        top_sites: list = []
        if self._use_tracemalloc and tracemalloc.is_tracing():
            current, _peak = tracemalloc.get_traced_memory()
            tm_mb = current / 1048576.0
            snapshot = tracemalloc.take_snapshot()
            if self._prev_snapshot is not None:
                for stat in snapshot.compare_to(self._prev_snapshot, "lineno")[:self._top_n]:
                    top_sites.append((stat.size_diff / 1048576.0,
                                      stat.size / 1048576.0,
                                      stat.count_diff,
                                      str(stat.traceback[0]) if stat.traceback else "?"))
            self._prev_snapshot = snapshot
        d_tm = "n/a" if (tm_mb is None or self._prev_tm_mb is None) else f"{tm_mb - self._prev_tm_mb:+.0f}"

        census_ms = (time.perf_counter() - t0) * 1000.0

        lines = [
            f"HEAPCENSUS elapsed={int(now - self._start)}s "
            f"py_mb={py_mb:.0f} d_py={d_py} objects={n_objects} "
            f"tm_mb={'n/a' if tm_mb is None else f'{tm_mb:.0f}'} d_tm={d_tm} "
            f"mi_use_mb={'n/a' if mi_use_mb is None else mi_use_mb} "
            f"census_ms={census_ms:.0f}"
        ]

        ranked = sorted(by_type.items(), key=lambda kv: -kv[1][1])[:self._top_n]
        lines.append(f"  by-type (top {len(ranked)} of {len(by_type)} types, by bytes):")
        for name, (cnt, tot) in ranked:
            p_cnt, p_tot = self._prev_by_type.get(name, (None, None))
            d_cnt = "" if p_cnt is None else f" ({cnt - p_cnt:+d})"
            d_tot = "" if p_tot is None else f" ({(tot - p_tot) / 1048576.0:+.1f}MB)"
            lines.append(f"    {name:<28} {tot / 1048576.0:8.1f}MB{d_tot:>12}   "
                         f"n={cnt}{d_cnt}")
        self._prev_by_type = by_type

        if top_sites:
            lines.append(f"  by-site (tracemalloc, top {len(top_sites)} by growth since last census):")
            for d_mb, cur_mb, d_cnt, where in top_sites:
                lines.append(f"    {d_mb:+8.1f}MB  (now {cur_mb:7.1f}MB, {d_cnt:+d} blocks)  {where}")
        elif self._use_tracemalloc:
            lines.append("  by-site: first census — deltas begin at the next one")

        self._prev_py_mb, self._prev_tm_mb = py_mb, tm_mb
        block = "\n".join(lines)
        logger.info(block)
        return block
