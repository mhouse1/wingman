"""Mission-level statistics tracking: per-mission outcomes and session aggregates."""

import json
import logging
import time
from pathlib import Path

logger = logging.getLogger(__name__)

_BATTLE_STATES = {"GAME_BATTLE", "GAME_BATTLE_MANUAL"}


def _fmt_duration(seconds: float) -> str:
    s = int(seconds)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    if h:
        return f"{h}h {m:02d}m {sec:02d}s"
    if m:
        return f"{m}m {sec:02d}s"
    return f"{sec}s"


class MissionStatsTracker:
    """Counts per-mission and session-level gameplay events.

    Thread-safety: on_event is called from the main loop thread only. on_fsm_transition
    may be called from the background OCR thread (analyzer fires click_to_detected from
    _run_click_to_in_background); the outcome fallback uses trigger_name to avoid the
    ordering race rather than relying on _pending_outcome being set first.

    Wiring:
      on_event()          — call for named events: respawn_detected, missiles_empty,
                            click_to_detected, flare_burst_deployed, flare_reload
      on_fsm_transition() — chain after existing replay/capture FSM callback
      finalize()          — call in finally block; writes JSON and returns session dict
      print_summary()     — call after finalize(); logs formatted console summary
    """

    def __init__(self, version: str = "?", output_dir: str = "docs/performance"):
        self._version = version
        self._output_dir = Path(output_dir)
        self._session_start = time.time()

        # Startup guard: ignore GAME_BATTLE entries until FSM has left GAME_UNKNOWN.
        self._startup_done = False

        # Current mission state.
        self._in_mission = False
        self._mission_start_ts: float = 0.0
        self._current: dict = {}

        # Accumulated per-mission records.
        self._missions: list[dict] = []

        # Session totals.
        self._total_respawns = 0
        self._total_flare_bursts = 0
        self._total_flare_reloads = 0
        self._total_manual_takeovers = 0

        # Pending outcome hint set by named events before the FSM transition fires.
        self._pending_outcome: str | None = None

        self._summary: dict | None = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def on_event(self, event_name: str, ts: float) -> None:
        """Record a named gameplay event."""
        if event_name == "respawn_detected":
            self._total_respawns += 1
            if self._in_mission:
                self._current["respawn_count"] += 1

        elif event_name == "flare_burst_deployed":
            self._total_flare_bursts += 1
            if self._in_mission:
                self._current["flare_burst_count"] += 1

        elif event_name == "flare_reload":
            self._total_flare_reloads += 1
            if self._in_mission:
                self._current["flare_reload_count"] += 1

        elif event_name == "missiles_empty":
            if self._in_mission:
                self._current["no_missiles_abort"] = True
                self._pending_outcome = "missiles_empty"

        elif event_name == "click_to_detected":
            if self._in_mission:
                self._pending_outcome = "click_to"

    def on_fsm_transition(
        self, trigger_name: str, prev_state: str, next_state: str, ts: float
    ) -> None:
        """Process an FSM state transition."""
        # Startup guard: capture whether classification was already done before this
        # transition, then update the flag. A GAME_UNKNOWN → GAME_BATTLE transition
        # clears the guard but must not count as a mission start (bot launched mid-game).
        was_startup_done = self._startup_done
        if not self._startup_done and next_state != "GAME_UNKNOWN":
            self._startup_done = True

        if next_state == "GAME_BATTLE_MANUAL" and self._in_mission:
            self._total_manual_takeovers += 1
            self._current["manual_takeover_count"] += 1

        # Mission start: entering GAME_BATTLE (auto) or GAME_BATTLE_MANUAL from a non-battle state.
        if next_state in _BATTLE_STATES and prev_state not in _BATTLE_STATES:
            if was_startup_done:
                self._start_mission(ts)

        # Mission end: leaving battle for a non-battle state.
        elif prev_state in _BATTLE_STATES and next_state not in _BATTLE_STATES:
            if self._in_mission:
                outcome = self._pending_outcome
                if outcome is None:
                    if trigger_name == "click_to_detected":
                        outcome = "click_to"
                    elif next_state in ("GAME_LOBBY", "GAME_WAITING"):
                        outcome = "lobby_exit"
                    else:
                        outcome = "unknown"
                self._end_mission(ts, outcome)

    def finalize(self, run_id: str | None = None) -> dict:
        """Close any open mission, build session dict, write JSON. Returns session dict."""
        if self._in_mission:
            self._end_mission(time.time(), "unknown")

        session_duration = time.time() - self._session_start
        missions_started = len(self._missions)

        counts = {"click_to": 0, "missiles_empty": 0, "lobby_exit": 0, "unknown": 0}
        durations = []
        for m in self._missions:
            outcome = m.get("outcome", "unknown")
            counts[outcome] += 1
            if m.get("duration_s") is not None:
                durations.append(m["duration_s"])

        avg_duration = (sum(durations) / len(durations)) if durations else None

        self._summary = {
            "wingman_version": self._version,
            "run_id": run_id or time.strftime("%Y%m%d_%H%M%S", time.localtime(self._session_start)),
            "session_start_ts": round(self._session_start, 3),
            "session_duration_s": round(session_duration, 1),
            "missions_started": missions_started,
            "missions_click_to": counts["click_to"],
            "missions_missiles_empty": counts["missiles_empty"],
            "missions_lobby_exit": counts["lobby_exit"],
            "missions_unknown_outcome": counts["unknown"],
            "total_respawns": self._total_respawns,
            "total_flare_bursts": self._total_flare_bursts,
            "total_flare_reloads": self._total_flare_reloads,
            "total_manual_takeovers": self._total_manual_takeovers,
            "avg_mission_duration_s": round(avg_duration, 1) if avg_duration is not None else None,
            "missions": self._missions,
        }

        self._write_json(self._summary)
        return self._summary

    def print_summary(self) -> None:
        """Log a formatted session summary. Call after finalize()."""
        if self._summary is None:
            logger.warning("MissionStatsTracker: print_summary called before finalize()")
            return

        s = self._summary
        n = s["missions_started"]
        dur = _fmt_duration(s["session_duration_s"])
        avg = _fmt_duration(s["avg_mission_duration_s"]) if s["avg_mission_duration_s"] else "n/a"

        def pct(count: int) -> str:
            if n == 0:
                return "  n/a"
            return f"{count / n * 100:4.0f}%"

        bursts_per = (
            f"  ({s['total_flare_bursts'] / n:.1f} per mission)" if n > 0 else ""
        )

        path_str = s.get("stats_path", "")
        path_line = f"\nStats saved to    : {path_str}" if path_str else ""

        lines = [
            "━" * 52,
            "  Wingman Session Summary",
            "━" * 52,
            f"Session duration  : {dur}",
            f"Missions started  : {n}",
            f"  Click-to finish : {s['missions_click_to']:<4} ({pct(s['missions_click_to'])})",
            f"  Missiles empty  : {s['missions_missiles_empty']:<4} ({pct(s['missions_missiles_empty'])})",
            f"  Lobby exit      : {s['missions_lobby_exit']:<4} ({pct(s['missions_lobby_exit'])})",
            f"  Unknown outcome : {s['missions_unknown_outcome']:<4} ({pct(s['missions_unknown_outcome'])})",
            f"Avg mission time  : {avg}",
            f"Total respawns    : {s['total_respawns']}",
            f"Total flare bursts: {s['total_flare_bursts']}{bursts_per}",
            f"Flare reloads     : {s['total_flare_reloads']}",
            f"Manual takeovers  : {s['total_manual_takeovers']}",
        ]
        if path_line:
            lines.append(path_line.strip())
        lines.append("━" * 52)

        logger.info("\n".join(lines))

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _start_mission(self, ts: float) -> None:
        self._in_mission = True
        self._mission_start_ts = ts
        self._pending_outcome = None
        self._current = {
            "index": len(self._missions),
            "start_ts": round(ts, 3),
            "duration_s": None,
            "respawn_count": 0,
            "flare_burst_count": 0,
            "flare_reload_count": 0,
            "no_missiles_abort": False,
            "manual_takeover_count": 0,
            "outcome": "unknown",
        }

    def _end_mission(self, ts: float, outcome: str) -> None:
        self._current["duration_s"] = round(ts - self._mission_start_ts, 1)
        self._current["outcome"] = outcome
        self._missions.append(self._current)
        self._current = {}
        self._in_mission = False
        self._pending_outcome = None

    def _write_json(self, data: dict) -> None:
        run_id = data["run_id"]
        out_dir = self._output_dir / "current"
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            logger.warning("MissionStatsTracker: could not create %s: %s", out_dir, e)
            return

        out_path = out_dir / f"run_{run_id}_stats.json"
        try:
            data["stats_path"] = str(out_path)
            out_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
            logger.info("MissionStatsTracker: session stats written to %s", out_path)
        except Exception as e:
            logger.warning("MissionStatsTracker: failed to write %s: %s", out_path, e)
