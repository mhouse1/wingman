"""Runtime performance tracking: per-crop OCR timing and incoming→flare reaction latency."""

import json
import logging
import threading
import time
from pathlib import Path

logger = logging.getLogger(__name__)

_CROPS = ("incoming", "respawn", "health", "ammo_flares", "ammo_missiles")

# (label, lo_inclusive, hi_exclusive)  — hi=None means unbounded upper
_OCR_BUCKETS = [
    ("<0.10s",     0.00, 0.10),
    ("0.10-0.24s", 0.10, 0.25),
    ("0.25-0.49s", 0.25, 0.50),
    (">=0.50s",    0.50, None),
]

_REACTION_BUCKETS = [
    ("<0.25s",     0.00, 0.25),
    ("0.25-0.49s", 0.25, 0.50),
    ("0.50-0.99s", 0.50, 1.00),
    (">=1.00s",    1.00, None),
]


def _compute_stats(samples: list) -> dict:
    if not samples:
        return {}
    n = len(samples)
    mean = sum(samples) / n
    s = sorted(samples)

    def pct(p: float) -> float:
        return s[min(int(p / 100 * n), n - 1)]

    return {
        "n": n,
        "mean": round(mean, 4),
        "p50": round(pct(50), 4),
        "p95": round(pct(95), 4),
        "p99": round(pct(99), 4),
        "max": round(s[-1], 4),
    }


def _bucket_pcts(samples: list, buckets: list) -> list:
    n = len(samples)
    if n == 0:
        return [0] * len(buckets)
    result = []
    for _, lo, hi in buckets:
        count = sum(1 for v in samples if v >= lo and (hi is None or v < hi))
        result.append(round(100 * count / n))
    return result


def _load_run_file(path: Path) -> "dict | None":
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning("PerformanceTracker: failed to load %s: %s", path, e)
        return None


def _aggregate_folder(folder: Path) -> "dict | None":
    """Weighted-mean aggregation across all run_*.json files in folder.

    Returns None when folder is missing or has no valid files.
    Percentiles come from the most-recently-dated file (by filename sort order,
    which matches start_ts ordering since run_YYYYMMDD_HHMMSS is lexicographic).
    """
    if not folder.exists():
        return None
    files = sorted(folder.glob("run_*.json"))
    run_data = [d for f in files if (d := _load_run_file(f)) is not None]
    if not run_data:
        return None

    most_recent = run_data[-1]

    agg_crops: dict = {}
    for crop in _CROPS:
        total_n = sum(
            d["ocr_crops"][crop]["n"]
            for d in run_data
            if crop in d.get("ocr_crops", {})
        )
        if total_n == 0:
            continue
        weighted_mean = sum(
            d["ocr_crops"][crop]["mean"] * d["ocr_crops"][crop]["n"]
            for d in run_data
            if crop in d.get("ocr_crops", {})
        ) / total_n
        recent_crop = most_recent.get("ocr_crops", {}).get(crop, {})
        agg_crops[crop] = {
            "n": total_n,
            "mean": round(weighted_mean, 4),
            "p50": recent_crop.get("p50", 0.0),
            "p95": recent_crop.get("p95", 0.0),
            "p99": recent_crop.get("p99", 0.0),
        }

    reaction_sessions = [d for d in run_data if d.get("reaction", {}).get("n", 0) > 0]
    reaction_total_n = sum(d["reaction"]["n"] for d in reaction_sessions)
    reaction_mean = 0.0
    if reaction_total_n > 0:
        reaction_mean = (
            sum(d["reaction"]["mean"] * d["reaction"]["n"] for d in reaction_sessions)
            / reaction_total_n
        )
    recent_reaction = most_recent.get("reaction", {})
    agg_reaction = {
        "n": reaction_total_n,
        "mean": round(reaction_mean, 4),
        "p95": recent_reaction.get("p95", 0.0),
    }

    return {
        "session_count": len(run_data),
        "total_n": agg_crops.get("incoming", {}).get("n", 0),
        "version": most_recent.get("version", "?"),
        "ocr_crops": agg_crops,
        "reaction": agg_reaction,
    }


class PerformanceTracker:
    """Tracks per-crop OCR timing and reaction latency. Thread-safe.

    Wiring:
      record_ocr_crop()     — background OCR thread, after each future.result()
      record_reaction()     — main thread, before flare burst on incoming detection
      on_enter_game_lobby() — main thread, on every GAME_LOBBY state transition
      on_session_end()      — main thread, after clean ThreadPoolExecutor shutdown
    """

    def __init__(self, config: dict, version: str = "?"):
        perf_cfg = config.get("performance", {})
        hist_cfg = perf_cfg.get("round_histogram", {})
        reg_cfg  = perf_cfg.get("regression", {})

        self._enabled: bool    = hist_cfg.get("enabled", True)
        self._output_dir       = Path(hist_cfg.get("output_dir", "docs/performance"))
        self._min_sessions: int  = reg_cfg.get("min_sessions", 5)
        self._min_cycles: int    = reg_cfg.get("min_cycles", 1000)
        self._threshold_pct: float = reg_cfg.get("threshold_pct", 20)
        self._version          = version

        self._lock = threading.Lock()
        self._session_start    = time.time()
        self._rounds           = 0

        # Per-round buffers — cleared after each on_enter_game_lobby() emission
        self._round_crops: dict   = {c: [] for c in _CROPS}
        self._round_reaction: list = []

        # Session-level buffers — accumulate until on_session_end()
        self._session_crops: dict   = {c: [] for c in _CROPS}
        self._session_reaction: list = []

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def record_ocr_crop(self, crop_name: str, seconds: float) -> None:
        """Record one OCR timing sample. Called from background OCR thread."""
        if not self._enabled or crop_name not in self._round_crops:
            return
        with self._lock:
            self._round_crops[crop_name].append(seconds)
            self._session_crops[crop_name].append(seconds)

    def record_reaction(self, seconds: float) -> None:
        """Record one incoming→flare reaction latency. Called from main thread."""
        if not self._enabled:
            return
        if not self._lock.acquire(timeout=0.1):
            logger.warning("PerformanceTracker: lock timeout in record_reaction — skipping")
            return
        try:
            self._round_reaction.append(seconds)
            self._session_reaction.append(seconds)
        finally:
            self._lock.release()

    def on_enter_game_lobby(self) -> None:
        """Emit round histogram when entering GAME_LOBBY (buffer-check: skips if no round data)."""
        if not self._enabled:
            return
        if not self._lock.acquire(timeout=0.1):
            logger.warning("PerformanceTracker: lock timeout in on_enter_game_lobby — skipping")
            return
        try:
            has_data = (
                any(self._round_crops[c] for c in _CROPS)
                or bool(self._round_reaction)
            )
            if not has_data:
                return
            self._rounds += 1
            round_num = self._rounds
            crops_snap    = {k: list(v) for k, v in self._round_crops.items()}
            reaction_snap = list(self._round_reaction)
            for k in self._round_crops:
                self._round_crops[k].clear()
            self._round_reaction.clear()
        finally:
            self._lock.release()

        self._log_round_histogram(round_num, crops_snap, reaction_snap)

    def on_session_end(self) -> None:
        """Write run JSON and emit cross-session comparison. Called after clean shutdown."""
        if not self._enabled:
            return
        with self._lock:
            crops_snap    = {k: list(v) for k, v in self._session_crops.items()}
            reaction_snap = list(self._session_reaction)
            rounds        = self._rounds

        end_ts = time.time()
        self._write_run_file(crops_snap, reaction_snap, rounds, end_ts)
        self._emit_comparison(crops_snap, reaction_snap)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _log_round_histogram(self, round_num: int, crops: dict, reaction: list) -> None:
        n_cycles = len(crops.get("incoming", []))
        header   = f"[ROUND {round_num} — OCR crop timings | {n_cycles} cycles]"
        col_hdr  = f"  {'crop':<14}  {'<0.10s':>6}  {'0.10-0.24s':>10}  {'0.25-0.49s':>10}  {'>=0.50s':>7}  {'mean':>6}  {'p95':>6}"
        lines    = [header, col_hdr]

        for crop in _CROPS:
            samples = crops.get(crop, [])
            if not samples:
                continue
            pcts  = _bucket_pcts(samples, _OCR_BUCKETS)
            stats = _compute_stats(samples)
            lines.append(
                f"  {crop:<14}  {pcts[0]:>5}%  {pcts[1]:>9}%  {pcts[2]:>9}%  {pcts[3]:>6}%"
                f"  {stats['mean']:.2f}s  {stats['p95']:.2f}s"
            )

        if reaction:
            pcts  = _bucket_pcts(reaction, _REACTION_BUCKETS)
            stats = _compute_stats(reaction)
            lines.append("")
            lines.append(f"[ROUND {round_num} — Reaction latency | {len(reaction)} events]")
            lines.append(
                f"  <0.25s {pcts[0]}%   0.25-0.49s {pcts[1]}%"
                f"   0.50-0.99s {pcts[2]}%   >=1.00s {pcts[3]}%"
            )
            lines.append(f"  mean {stats['mean']:.2f}s   max {stats['max']:.2f}s")

        logger.info("\n".join(lines))

    def _write_run_file(
        self, crops: dict, reaction: list, rounds: int, end_ts: float
    ) -> None:
        run_id  = time.strftime("%Y%m%d_%H%M%S", time.localtime(self._session_start))
        out_dir = self._output_dir / "current"
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            logger.warning("PerformanceTracker: could not create %s: %s", out_dir, e)
            return

        ocr_out: dict = {}
        for crop in _CROPS:
            stats = _compute_stats(crops.get(crop, []))
            if stats:
                # Drop 'max' from OCR crops — not in schema
                ocr_out[crop] = {k: v for k, v in stats.items() if k != "max"}

        r_stats = _compute_stats(reaction)
        reaction_out = r_stats if r_stats else {
            "n": 0, "mean": 0.0, "p50": 0.0, "p95": 0.0, "p99": 0.0, "max": 0.0,
        }

        data = {
            "version":   self._version,
            "run_id":    run_id,
            "start_ts":  round(self._session_start, 3),
            "end_ts":    round(end_ts, 3),
            "rounds":    rounds,
            "ocr_crops": ocr_out,
            "reaction":  reaction_out,
        }

        out_path = out_dir / f"run_{run_id}.json"
        try:
            out_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
            logger.info("PerformanceTracker: session data written to %s", out_path)
        except Exception as e:
            logger.warning("PerformanceTracker: failed to write %s: %s", out_path, e)

    def _emit_comparison(self, session_crops: dict, session_reaction: list) -> None:
        current_agg = _aggregate_folder(self._output_dir / "current")
        if current_agg is None:
            return

        session_n      = len(session_crops.get("incoming", []))
        period_sessions = current_agg["session_count"]
        period_n       = current_agg["total_n"]

        # Block 1: this session vs current-period aggregate
        lines = [
            f"[SESSION vs CURRENT PERIOD"
            f" | this session: {self._rounds} rounds {session_n} cycles"
            f" | period: {period_sessions} sessions {period_n} cycles]",
            f"  {'crop':<14}  {'this session':>22}  {'period mean':>11}  delta",
        ]
        for crop in _CROPS:
            s_stats = _compute_stats(session_crops.get(crop, []))
            p_stats = current_agg["ocr_crops"].get(crop)
            if not s_stats or not p_stats:
                continue
            delta = (s_stats["mean"] - p_stats["mean"]) / max(p_stats["mean"], 0.001) * 100
            arrow = "↑" if delta > 0 else ""
            flag  = "  ⚠️ OUTLIER SESSION" if abs(delta) >= self._threshold_pct else ""
            lines.append(
                f"  {crop:<14}  mean {s_stats['mean']:.2f}s p95 {s_stats['p95']:.2f}s"
                f"    {p_stats['mean']:.2f}s      {delta:+.0f}% {arrow}{flag}"
            )

        r_s = _compute_stats(session_reaction)
        r_p = current_agg["reaction"]
        if r_s and r_p.get("n", 0) > 0:
            delta = (r_s["mean"] - r_p["mean"]) / max(r_p["mean"], 0.001) * 100
            arrow = "↑" if delta > 0 else ""
            flag  = "  ⚠️ OUTLIER SESSION" if abs(delta) >= self._threshold_pct else ""
            lines.append(
                f"  {'reaction':<14}  mean {r_s['mean']:.2f}s p95 {r_s['p95']:.2f}s"
                f"    {r_p['mean']:.2f}s      {delta:+.0f}% {arrow}{flag}"
                f"  ({r_s['n']} events this session)"
            )

        logger.info("\n".join(lines))

        # Threshold guard for Block 2
        if period_sessions < self._min_sessions or period_n < self._min_cycles:
            logger.info(
                "[PERIOD COMPARISON] accumulating baseline"
                " (N=%d sessions, %d cycles — need %d sessions and %d cycles)",
                period_sessions, period_n, self._min_sessions, self._min_cycles,
            )
            return

        release_agg = _aggregate_folder(self._output_dir / "release")
        if release_agg is None:
            logger.info(
                "[PERIOD COMPARISON] no release/ baseline — run 'make wrelease' to create one"
            )
            return

        # Block 2: current-period aggregate vs release baseline
        r_ver = release_agg["version"]
        lines2 = [
            f"[PERIOD vs RELEASE v{r_ver}"
            f" | current: {period_sessions} sessions {period_n} cycles"
            f" | release: {release_agg['session_count']} sessions {release_agg['total_n']} cycles]",
            f"  {'crop':<14}  {'current mean':>12}  {'release mean':>12}  delta",
        ]
        for crop in _CROPS:
            c_stats = current_agg["ocr_crops"].get(crop)
            r_stats = release_agg["ocr_crops"].get(crop)
            if not c_stats or not r_stats:
                continue
            delta  = (c_stats["mean"] - r_stats["mean"]) / max(r_stats["mean"], 0.001) * 100
            arrow  = "↑" if delta > 0 else ""
            flag   = "  ⚠️ REGRESSION" if abs(delta) >= self._threshold_pct else ""
            prefix = "⚠️  " if flag else "   "
            lines2.append(
                f"  {prefix}{crop:<14}  {c_stats['mean']:.2f}s"
                f"            {r_stats['mean']:.2f}s"
                f"        {delta:+.0f}% {arrow}{flag}"
            )

        c_r = current_agg["reaction"]
        r_r = release_agg["reaction"]
        if c_r.get("n", 0) > 0 and r_r.get("n", 0) > 0:
            delta  = (c_r["mean"] - r_r["mean"]) / max(r_r["mean"], 0.001) * 100
            arrow  = "↑" if delta > 0 else ""
            flag   = "  ⚠️ REGRESSION" if abs(delta) >= self._threshold_pct else ""
            prefix = "⚠️  " if flag else "   "
            lines2.append(
                f"  {prefix}{'reaction':<14}  {c_r['mean']:.2f}s"
                f"            {r_r['mean']:.2f}s"
                f"        {delta:+.0f}% {arrow}{flag}"
                f"  ({c_r['n']} events in period)"
            )

        logger.info("\n".join(lines2))
