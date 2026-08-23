"""Periodic self-resource sampling for long-session leak diagnosis (Performance 008).

Emits one greppable `RESOURCE` line per interval carrying everything the
2026-08-20 leak investigation had to reconstruct by hand from an external
host sampler:

    RESOURCE elapsed=8112s rss_mb=2841 swap_mb=1203 threads=31 fds=147
             gc=(412,29,7) ocr_med_5m=0.47 ocr_p95_5m=2.71 n_ocr=1652
             game_rss_mb=6210 game_swap_mb=3401 sys_swap_mb=12371

`grep RESOURCE wingman.log` then answers, without any external correlation:

  * wingman RSS flat while `game_rss_mb` climbs  → the game leaks
  * `rss_mb` climbing                            → wingman leaks
  * `threads` climbing                           → daemon threads not reaped
  * `fds` climbing                               → leaked X Display connections
                                                   (_linux_key_event opens one
                                                   per key event)
  * `ocr_med_5m` rising alongside any of the above → the degradation mechanism

Design rules:
  * Never raises. A diagnostic must not be able to break the main loop; every
    probe is individually guarded and degrades to `n/a`.
  * Stdlib only (/proc, gc, threading) — no psutil dependency.
  * Cheap: a handful of small file reads, a few ms, at a 5-minute cadence.
    Called from the tick loop but self-throttled, so the per-tick cost when
    not due is one float comparison.
"""

import ctypes
import gc
import logging
import os
import threading
import time

logger = logging.getLogger(__name__)

_PROC = "/proc"



class _MallInfo2(ctypes.Structure):
    """glibc `struct mallinfo2` — all fields size_t since glibc 2.33."""
    _fields_ = [(n, ctypes.c_size_t) for n in (
        "arena", "ordblks", "smblks", "hblks", "hblkhd", "usmblks",
        "fsmblks", "uordblks", "fordblks", "keepcost")]


def _load_mallinfo2():
    """Return a callable giving glibc allocator stats, or None."""
    try:
        libc = ctypes.CDLL("libc.so.6")
        fn = libc.mallinfo2
        fn.restype = _MallInfo2
        fn.argtypes = []
        fn()                      # probe once; a wrong signature fails here
        return fn
    except Exception:
        return None


_mallinfo2 = _load_mallinfo2()


def _read_malloc_stats() -> dict:
    """Live-vs-retained split from the allocator itself, in MB.

    Answers the question RSS and `anon` cannot: whether heap growth is memory
    the program is USING or memory glibc has freed and kept.

      * ``uordblks`` climbing  -> live allocations; something retains objects
      * ``fordblks`` climbing  -> freed but held in the arena; fragmentation

    Performance 008 needs this because two hypotheses have already been tested
    and refuted (the arena cap, then EasyOCR reader churn) without ever
    distinguishing these two cases. Note the figures cover the main arena and
    mmap'd blocks as glibc accounts them, so they will not sum exactly to RSS.

    Returns {} where mallinfo2 is unavailable rather than failing the sample.
    """
    if _mallinfo2 is None:
        return {}
    try:
        m = _mallinfo2()
        return {
            "mi_use": int(m.uordblks / 1048576),    # live
            "mi_free": int(m.fordblks / 1048576),   # freed, retained in arena
            "mi_mmap": int(m.hblkhd / 1048576),     # mmap'd regions
        }
    except Exception:
        return {}


def _read_smaps_rollup(pid: str = "self") -> dict:
    """Anonymous / file-backed / shared split for one process, in kB.

    RSS alone cannot answer "whose leak". Wingman receives capture buffers
    through the PipeWire pipeline the game feeds, and mapped pages count toward
    VmRSS just as heap does — so a growing RSS is consistent BOTH with wingman
    retaining its own allocations and with capture buffers accumulating. The
    split separates them:

      * ``anon`` climbing   → wingman's own heap (allocator or retention)
      * ``file``/``shmem`` climbing → mapped buffers, i.e. the capture path

    Returns {} on kernels without smaps_rollup rather than failing the sample.
    """
    out = {}
    try:
        with open(f"{_PROC}/{pid}/smaps_rollup", "r", encoding="utf-8",
                  errors="replace") as fh:
            for line in fh:
                if line.startswith(("Anonymous:", "Rss:", "Shmem:",
                                    "Private_Dirty:", "Shared_Clean:",
                                    "Shared_Dirty:")):
                    key, _, rest = line.partition(":")
                    parts = rest.split()
                    if parts:
                        out[key] = int(parts[0])
    except (OSError, ValueError):
        pass
    return out


def _read_proc_status(pid: str = "self") -> dict:
    """Parse the VmRSS/VmSwap/VmHWM/Threads fields out of /proc/<pid>/status."""
    out = {}
    try:
        with open(f"{_PROC}/{pid}/status", "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if line.startswith(("VmRSS:", "VmSwap:", "VmHWM:", "Threads:")):
                    key, _, rest = line.partition(":")
                    parts = rest.split()
                    if parts:
                        out[key] = int(parts[0])
    except (OSError, ValueError):
        pass
    return out


# Growth below this rate is indistinguishable from ordinary drift and must not
# be called a leak.
_LEAK_RATE_MB_PER_H = 50.0
# A verdict needs enough elapsed time for a rate to mean anything; the
# 2026-08-20 curve stayed flat for its first hour before compounding.
_MIN_VERDICT_HOURS = 1.0
# Rates are measured from a post-warm-up ANCHOR, never from t=0. Wingman
# allocates ~3.9 GB in its first five minutes loading 13 thread-local EasyOCR
# readers (measured 2026-08-20 23:09: rss 681 -> 4598 MB, threads 2 -> 22).
# Dividing that one-off across an 8-hour session yields ~490 MB/h of phantom
# growth — enough to report a WINGMAN-SIDE leak on a session with no leak at
# all. The t=0 baseline is still shown as the absolute span; only the rate and
# the verdict use the anchor.
_WARMUP_S = 600.0


def _kb_to_mb(kb) -> "int | None":
    return int(kb / 1024) if isinstance(kb, int) else None


class ResourceSampler:
    """Self-throttled resource sampler. Call `maybe_sample()` every tick."""

    def __init__(self, cfg: "dict | None" = None, perf_tracker=None,
                 game_process_name: str = "Metalstorm.exe", clock=time.time):
        cfg = cfg or {}
        self._enabled = bool(cfg.get("enabled", True))
        self._interval_s = max(10.0, float(cfg.get("interval_s", 300.0)))
        self._game_name = str(cfg.get("game_process_name", game_process_name))
        self._clock = clock
        self._perf = perf_tracker
        self._session_start = clock()
        # Emit the first sample immediately so every session has a t=0 baseline
        # to measure growth against — the 2026-08-20 analysis needed exactly
        # this and had to infer it from the first log hour.
        self._next_due = self._session_start
        self._ocr_offsets: "dict | None" = None
        self._game_pids: "list[str]" = []
        self._game_scan_due = 0.0
        # First and latest observation of each tracked quantity, so the
        # session-end summary can state growth and rate without the reader
        # re-deriving them from the line series.
        self._first: "dict | None" = None
        # Post-warm-up rate anchor — see _WARMUP_S.
        self._anchor: "dict | None" = None
        self._warmup_s = max(0.0, float(cfg.get("warmup_s", _WARMUP_S)))
        self._last: dict = {}
        self._samples = 0
        self._pool_depth_fn = None
        # ADR 090 memory guard. The leak is unfixed (Performance 008) and
        # degrades perception continuously: OCR p95 crosses the 1.5s tick
        # budget at ~hour 3 and the median at ~hour 6, while RSS reached
        # 13.2 GB at 6.9h — past the footprint that preceded the compositor
        # OOM in the foundry cross-reference. Until the allocation is found,
        # bound the damage.
        _guard = cfg.get("memory_guard", {}) or {}
        self._guard_enabled = bool(_guard.get("enabled", True))
        self._guard_soft_mb = float(_guard.get("soft_limit_mb", 6000))
        self._guard_hard_mb = float(_guard.get("hard_limit_mb", 10000))
        self._guard_armed = False       # soft limit crossed; await a safe point
        self._guard_hard = False        # hard limit crossed; stop now
        self._guard_peak_mb = 0.0

    def should_stop(self, at_safe_point: bool) -> bool:
        """True when the session should end to bound the Performance 008 leak.

        Two thresholds, deliberately different in urgency:

        * soft — stop at the next SAFE POINT. Restarting mid-mission abandons
          an aircraft in flight, so the guard arms and waits for the caller to
          say it is between missions.
        * hard — stop immediately. Past this the risk of an out-of-memory kill
          taking the desktop session with it outweighs one lost mission.
        """
        if not self._guard_enabled:
            return False
        return self._guard_hard or (self._guard_armed and at_safe_point)

    def guard_reason(self) -> str:
        if self._guard_hard:
            return f"hard limit {self._guard_hard_mb:.0f} MB"
        return f"soft limit {self._guard_soft_mb:.0f} MB at a safe point"

    def set_pool_depth_source(self, fn) -> None:
        """Wire a callable returning current OCR queue depth (FUTURE 001 item 5)."""
        self._pool_depth_fn = fn

    # -- process discovery --------------------------------------------------

    def _find_game_pids(self, now: float) -> "list[str]":
        """Locate game processes by comm, rescanned periodically (cheap).

        Summed RSS across a process tree double-counts shared pages; that is
        acceptable here because this figure is read as a GROWTH TREND, never
        as an absolute footprint.
        """
        if self._game_pids and now < self._game_scan_due:
            if all(os.path.isdir(f"{_PROC}/{p}") for p in self._game_pids):
                return self._game_pids
        self._game_scan_due = now + 60.0
        found = []
        try:
            for entry in os.listdir(_PROC):
                if not entry.isdigit():
                    continue
                try:
                    with open(f"{_PROC}/{entry}/comm", "r", errors="replace") as fh:
                        if fh.read().strip() == self._game_name:
                            found.append(entry)
                except OSError:
                    continue
        except OSError:
            pass
        self._game_pids = found
        return found

    # -- probes -------------------------------------------------------------

    def _system_swap_mb(self) -> "int | None":
        try:
            total = free = None
            with open(f"{_PROC}/meminfo", "r", errors="replace") as fh:
                for line in fh:
                    if line.startswith("SwapTotal:"):
                        total = int(line.split()[1])
                    elif line.startswith("SwapFree:"):
                        free = int(line.split()[1])
                    if total is not None and free is not None:
                        break
            if total is not None and free is not None:
                return _kb_to_mb(total - free)
        except (OSError, ValueError, IndexError):
            pass
        return None

    def _fd_count(self) -> "int | None":
        try:
            return len(os.listdir(f"{_PROC}/self/fd"))
        except OSError:
            return None

    def _ocr_window(self):
        """Per-crop OCR stats for samples recorded since the previous sample."""
        if self._perf is None or not hasattr(self._perf, "snapshot_since"):
            return None, None, 0
        try:
            stats, self._ocr_offsets = self._perf.snapshot_since(self._ocr_offsets)
        except Exception:
            return None, None, 0
        merged = []
        for values in (stats or {}).values():
            merged.extend(values)
        if not merged:
            return None, None, 0
        merged.sort()
        median = merged[len(merged) // 2]
        p95 = merged[min(len(merged) - 1, int(len(merged) * 0.95))]
        return median, p95, len(merged)

    # -- main entry point ---------------------------------------------------

    def maybe_sample(self, now: "float | None" = None) -> "str | None":
        """Emit a RESOURCE line if the interval has elapsed. Returns the line."""
        if not self._enabled:
            return None
        now = self._clock() if now is None else now
        if now < self._next_due:
            return None
        self._next_due = now + self._interval_s
        try:
            return self._sample(now)
        except Exception:
            logger.debug("ResourceSampler: sample failed", exc_info=True)
            return None

    def _pool_depth(self) -> "int | None":
        if self._pool_depth_fn is None:
            return None
        try:
            return int(self._pool_depth_fn())
        except Exception:
            return None

    def _sample(self, now: float) -> str:
        me = _read_proc_status("self")
        rss = _kb_to_mb(me.get("VmRSS"))
        peak = _kb_to_mb(me.get("VmHWM"))
        swap = _kb_to_mb(me.get("VmSwap"))
        fds = self._fd_count()
        # Whose pages are these? (see _read_smaps_rollup)
        roll = _read_smaps_rollup("self")
        anon = _kb_to_mb(roll.get("Anonymous"))
        shmem = _kb_to_mb(roll.get("Shmem"))
        # Performance 008: live vs retained, straight from the allocator.
        mi = _read_malloc_stats()
        mi_use, mi_free = mi.get("mi_use"), mi.get("mi_free")
        counts = gc.get_count()
        med, p95, n_ocr = self._ocr_window()

        game_rss = game_swap = None
        pids = self._find_game_pids(now)
        if pids:
            rss_sum = swap_sum = 0
            for pid in pids:
                st = _read_proc_status(pid)
                rss_sum += st.get("VmRSS", 0)
                swap_sum += st.get("VmSwap", 0)
            game_rss, game_swap = _kb_to_mb(rss_sum), _kb_to_mb(swap_sum)

        if self._guard_enabled and isinstance(rss, int):
            self._guard_peak_mb = max(self._guard_peak_mb, float(rss))
            if not self._guard_armed and rss >= self._guard_soft_mb:
                self._guard_armed = True
                logger.warning(
                    "MEMORY GUARD armed: rss %d MB >= soft limit %.0f MB — "
                    "will stop at the next safe point (ADR 090)",
                    rss, self._guard_soft_mb)
            if not self._guard_hard and rss >= self._guard_hard_mb:
                self._guard_hard = True
                logger.error(
                    "MEMORY GUARD hard limit: rss %d MB >= %.0f MB — stopping "
                    "regardless of state (ADR 090)", rss, self._guard_hard_mb)

        obs = {
            "t": now, "rss": rss, "swap": swap, "peak": peak, "fds": fds,
            "anon": anon, "shmem": shmem,
            "mi_use": mi_use, "mi_free": mi_free,
            "threads": threading.active_count(), "game_rss": game_rss,
            "game_swap": game_swap, "ocr_med": med,
            "sys_swap": self._system_swap_mb(),
        }
        if self._first is None:
            self._first = dict(obs)
        if self._anchor is None and (now - self._session_start) >= self._warmup_s:
            self._anchor = dict(obs)
        self._last = dict(obs)
        self._samples += 1

        def _fmt(value, spec=""):
            return "n/a" if value is None else format(value, spec)

        def _delta(key):
            """Growth since the session baseline — the number being watched."""
            a, b = (self._first or {}).get(key), obs.get(key)
            return f"{b - a:+d}" if isinstance(a, int) and isinstance(b, int) else "n/a"

        depth = self._pool_depth()
        line = (
            f"RESOURCE elapsed={int(now - self._session_start)}s "
            f"rss_mb={_fmt(rss)} d_rss={_delta('rss')} "
            f"anon_mb={_fmt(anon)} d_anon={_delta('anon')} "
            f"mi_use_mb={_fmt(mi_use)} d_mi_use={_delta('mi_use')} "
            f"mi_free_mb={_fmt(mi_free)} d_mi_free={_delta('mi_free')} "
            f"shmem_mb={_fmt(shmem)} d_shmem={_delta('shmem')} "
            f"peak_rss_mb={_fmt(peak)} "
            f"swap_mb={_fmt(swap)} threads={obs['threads']} d_threads={_delta('threads')} "
            f"fds={_fmt(fds)} d_fds={_delta('fds')} "
            f"gc=({counts[0]},{counts[1]},{counts[2]}) "
            f"ocr_med={_fmt(med, '.2f')} ocr_p95={_fmt(p95, '.2f')} n_ocr={n_ocr} "
            f"pool_depth={_fmt(depth)} "
            f"game_rss_mb={_fmt(game_rss)} d_game_rss={_delta('game_rss')} "
            f"game_swap_mb={_fmt(game_swap)} sys_swap_mb={_fmt(obs['sys_swap'])}"
        )
        logger.info(line)
        return line

    # -- session-end verdict ------------------------------------------------

    def _rate_mb_per_h(self, key: str) -> "float | None":
        """Growth rate measured from the post-warm-up anchor, not from t=0."""
        anchor = self._anchor
        if anchor is None:
            return None
        a, b = anchor.get(key), self._last.get(key)
        hours = (self._last.get("t", 0.0) - anchor.get("t", 0.0)) / 3600.0
        if not isinstance(a, int) or not isinstance(b, int) or hours <= 0:
            return None
        return (b - a) / hours

    def _measured_hours(self) -> float:
        """Hours of post-warm-up observation — the window a rate describes."""
        if self._anchor is None:
            return 0.0
        return max(0.0, (self._last.get("t", 0.0) - self._anchor.get("t", 0.0)) / 3600.0)

    def summarize(self, now: "float | None" = None) -> "str | None":
        """Emit the session-end growth summary and leak-attribution verdict.

        This is the payload of the whole feature: it turns a series of lines
        into the one sentence the investigation needs — which process grew,
        how fast, and whether the OCR pipeline degraded with it.
        """
        if not self._enabled or self._first is None or self._samples < 2:
            return None
        try:
            now = self._clock() if now is None else now
            hours = max(0.0, (now - self._session_start) / 3600.0)
            measured_h = self._measured_hours()
            self_rate = self._rate_mb_per_h("rss")
            game_rate = self._rate_mb_per_h("game_rss")

            def _span(key, unit=""):
                a, b = self._first.get(key), self._last.get(key)
                if not isinstance(a, int) or not isinstance(b, int):
                    return "n/a"
                return f"{a}->{b}{unit} ({b - a:+d})"

            ocr_a = self._first.get("ocr_med")
            ocr_b = self._last.get("ocr_med")
            if isinstance(ocr_a, float) and isinstance(ocr_b, float) and ocr_a > 0:
                ocr_txt = f"{ocr_a:.2f}->{ocr_b:.2f}s ({ocr_b / ocr_a:.1f}x)"
            else:
                ocr_txt = "n/a"

            verdict = self._verdict(measured_h, self_rate, game_rate)
            lines = [
                f"RESOURCE SUMMARY elapsed={hours:.1f}h samples={self._samples}"
                f" measured={measured_h:.1f}h (rates exclude {self._warmup_s / 60:.0f}min warm-up)",
                f"  malloc  live {_span('mi_use', 'MB')}"
                f"   retained-in-arena {_span('mi_free', 'MB')}\n"
                f"  wingman rss {_span('rss', 'MB')}"
                + (f"  rate={self_rate:+.0f} MB/h" if self_rate is not None else "")
                + f"  peak={self._last.get('peak', 'n/a')}MB",
                f"  game    rss {_span('game_rss', 'MB')}"
                + (f"  rate={game_rate:+.0f} MB/h" if game_rate is not None else ""),
                f"  threads {_span('threads')}   fds {_span('fds')}"
                f"   sys_swap {_span('sys_swap', 'MB')}",
                f"  ocr_med {ocr_txt}",
                f"  VERDICT: {verdict}",
            ]
            out = "\n".join(lines)
            logger.info(out)
            return out
        except Exception:
            logger.debug("ResourceSampler: summarize failed", exc_info=True)
            return None

    def _verdict(self, hours, self_rate, game_rate) -> str:
        """State only what the numbers support — this feeds a live investigation."""
        if self._anchor is None:
            return (f"still warming up (<{self._warmup_s / 60:.0f}min) — "
                    f"no rate anchor, no attribution")
        # Severity escape hatch: the short-window guard exists to stop a slow
        # drift being extrapolated from noise, NOT to sit on a catastrophe.
        # The 2026-08-20 23:04 session leaked 15.2 GB in 25 minutes
        # (+36,300 MB/h) and the guard still withheld a verdict because the
        # window was 0.4h. At this magnitude the window length cannot explain
        # the number, so say it immediately.
        severe = _LEAK_RATE_MB_PER_H * 20
        if hours < _MIN_VERDICT_HOURS:
            worst = max((r for r in (self_rate, game_rate) if r is not None),
                        default=None)
            if worst is None or worst < severe:
                return (f"measured window too short for a rate ({hours:.1f}h < "
                        f"{_MIN_VERDICT_HOURS:.0f}h post-warm-up) — no attribution")
            who = ("WINGMAN" if self_rate is not None and self_rate >= (game_rate or 0)
                   else "GAME")
            return (f"SEVERE {who}-SIDE growth ({worst:+.0f} MB/h over only "
                    f"{hours:.1f}h) — far above the {severe:.0f} MB/h severity "
                    f"bar; stop the session before the host exhausts memory")
        if self_rate is None and game_rate is None:
            return "no memory samples — cannot attribute"
        s_leak = self_rate is not None and self_rate > _LEAK_RATE_MB_PER_H
        g_leak = game_rate is not None and game_rate > _LEAK_RATE_MB_PER_H
        if s_leak and g_leak:
            who = "wingman" if (self_rate or 0) >= (game_rate or 0) else "game"
            return (f"BOTH processes growing above {_LEAK_RATE_MB_PER_H:.0f} MB/h "
                    f"({who} faster) — see Performance 008")
        if s_leak:
            return (f"WINGMAN-SIDE growth ({self_rate:+.0f} MB/h) while the game "
                    f"stayed flat — leak is in this process (Performance 008)")
        if g_leak:
            return (f"GAME-SIDE growth ({game_rate:+.0f} MB/h) while wingman "
                    f"stayed flat — wingman is a victim, not the cause")
        return (f"no leak observed this session (both under "
                f"{_LEAK_RATE_MB_PER_H:.0f} MB/h over {hours:.1f}h)")
