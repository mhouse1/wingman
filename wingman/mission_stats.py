"""Mission-level statistics tracking: per-mission outcomes and session aggregates."""

import json
import logging
import threading
import time
from pathlib import Path

logger = logging.getLogger(__name__)

# GAME_BATTLE_EJECT is a mid-mission excursion (missiles empty -> dive), not a
# mission boundary. Omitting it made every eject read as a mission end and the
# return from it as a new mission start: the 2026-07-30 16:27 session reported
# "7 missions, avg 1m30s" for 3 real rounds (avg ~4m57s), and undercounted
# manual takeovers 1-of-4 because _in_mission was already False when the
# EJECT -> MANUAL transitions arrived.
_BATTLE_STATES = {"GAME_BATTLE", "GAME_BATTLE_MANUAL", "GAME_BATTLE_EJECT"}

# ADR 070 V5 engagement accounting.
# Alerts inside one volley arrive ~1.3-1.7 s apart (2026-08-11/12 sessions) and
# are ONE engagement; separate volleys are minutes apart, so the grouping
# threshold sits well clear of both.
_VOLLEY_GROUP_S = 3.0
# An evade is selected on the next behavior-tree tick, ~1.3-1.5 s after the
# alert that triggered it; allow slack without reaching the next volley.
_EVADE_ATTRIBUTION_S = 3.0
# A missile that is going to kill you does it well inside this window. Longer
# and unrelated deaths get attributed to the engagement.
_ENGAGEMENT_WINDOW_S = 10.0

# ADR 076: a death this soon after a post-respawn mission restart is a
# spawn-crash candidate — the anomaly the spawn-attitude guard exists to fix.
# Stamped off the existing restart_last_mission event (the post-respawn
# restart path), so no new event names enter the replay/capture streams.
_SPAWN_CRASH_WINDOW_S = 10.0

# ADR 082: and no sooner than this. The aircraft respawns AIRBORNE with
# forward speed, so it cannot reach terrain in under a second — a death
# stamped that fast is a second respawn_detected arriving on the heels of
# the restart, not a crash. The 2026-08-19 12-hour session produced 22 such
# events (median 0.2 s, max 2.5 s) and zero genuine ones. Sub-floor events
# are counted separately rather than dropped: they are respawn-flow churn
# (each one cancels a freshly restarted mission) and the only surviving
# evidence that it happens.
_SPAWN_CRASH_MIN_S = 3.0


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
        self._total_missile_evades = 0

        # ADR 070 V5: per-ENGAGEMENT survival, the measure a per-mission death
        # rate cannot give. Missions mix engagements the tactic touched with
        # ones it never saw (2026-08-12: 8 evades across 12 missions), so the
        # per-mission rate is dominated by deaths the evade had no part in.
        # One record per missile volley: did the aircraft die within
        # _engagement_window_s of the alert, and had an evade fired?
        self._engagements: list[dict] = []

        # ADR 076/082 spawn-crash instrument: deaths in
        # [_SPAWN_CRASH_MIN_S, _SPAWN_CRASH_WINDOW_S] after a post-respawn
        # restart. The before/after measure for the spawn-attitude guard.
        # Faster deaths are respawn re-detection churn, tracked separately.
        self._last_restart_ts: float | None = None
        self._spawn_crashes: list[float] = []       # seconds from restart to death
        self._immediate_redetects: list[float] = []  # sub-floor, same stamp

        # Pending outcome hint set by named events before the FSM transition fires.
        self._pending_outcome: str | None = None

        self._summary: dict | None = None

        # on_event runs on the main loop; on_fsm_transition can arrive from the
        # analyzer's background click-to thread. Both mutate _current/_in_mission,
        # so an unlocked check-then-act could KeyError the main loop when a
        # mission ends between the check and the increment. Bodies are
        # sub-microsecond dict ops; per repo rules the main-loop side acquires
        # with a timeout instead of blocking indefinitely.
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def on_event(self, event_name: str, ts: float) -> None:
        """Record a named gameplay event. Called from the main loop."""
        if not self._lock.acquire(timeout=1.0):
            logger.warning("MissionStatsTracker: lock timeout — dropping event %s", event_name)
            return
        try:
            self._on_event_locked(event_name, ts)
        finally:
            self._lock.release()

    def _on_event_locked(self, event_name: str, ts: float) -> None:
        if event_name == "respawn_detected":
            self._total_respawns += 1
            if self._in_mission:
                self._current["respawn_count"] += 1
            self._attribute_death(ts)
            # ADR 076/082: death shortly after a restart = spawn crash, but
            # only above the physical floor; faster ones are re-detection
            # churn. One candidate per life — the stamp is consumed in every
            # branch.
            if self._last_restart_ts is not None:
                since_restart = ts - self._last_restart_ts
                if since_restart <= _SPAWN_CRASH_WINDOW_S:
                    if since_restart >= _SPAWN_CRASH_MIN_S:
                        self._spawn_crashes.append(round(since_restart, 1))
                    else:
                        self._immediate_redetects.append(round(since_restart, 1))
                self._last_restart_ts = None

        elif event_name == "restart_last_mission":
            self._last_restart_ts = ts

        elif event_name == "flare_burst_deployed":
            self._total_flare_bursts += 1
            if self._in_mission:
                self._current["flare_burst_count"] += 1
            self._open_engagement(ts)

        elif event_name == "missile_evade":
            self._total_missile_evades += 1
            if self._in_mission:
                self._current["missile_evade_count"] += 1
            self._mark_engagement_evaded(ts)

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
        """Process an FSM state transition. May arrive from a background thread,
        which can block briefly (lock holds are sub-microsecond)."""
        with self._lock:
            self._on_fsm_transition_locked(trigger_name, prev_state, next_state, ts)

    def _on_fsm_transition_locked(
        self, trigger_name: str, prev_state: str, next_state: str, ts: float
    ) -> None:
        # Startup guard: capture whether classification was already done before this
        # transition, then update the flag. A GAME_UNKNOWN → GAME_BATTLE transition
        # clears the guard but must not count as a mission start (bot launched mid-game).
        was_startup_done = self._startup_done
        if not self._startup_done and next_state != "GAME_UNKNOWN":
            self._startup_done = True

        # Mission start: entering GAME_BATTLE (auto) or GAME_BATTLE_MANUAL from a non-battle state.
        if next_state in _BATTLE_STATES and prev_state not in _BATTLE_STATES:
            if was_startup_done:
                self._start_mission(ts)

        # Mission end: leaving battle for a non-battle state.
        elif prev_state in _BATTLE_STATES and next_state not in _BATTLE_STATES:
            if self._in_mission:
                # The trigger that actually ENDS the mission is authoritative;
                # _pending_outcome only fills in when the transition itself says
                # nothing. Since GAME_BATTLE_EJECT became an in-mission state,
                # "missiles_empty" is a MID-mission signal that survives to the
                # real end of the round, and checking it first shadowed the
                # terminal click_to: the 2026-07-30 18:51 session booked all 10
                # missions as "missiles empty (100%)" despite 10 logged
                # CLICK TO CONTINUE finishes.
                if trigger_name == "click_to_detected":
                    outcome = "click_to"
                elif self._pending_outcome is not None:
                    outcome = self._pending_outcome
                elif next_state in ("GAME_LOBBY", "GAME_WAITING"):
                    outcome = "lobby_exit"
                else:
                    outcome = "unknown"
                self._end_mission(ts, outcome)

        # Counted AFTER the start/end handling: a takeover can itself be the
        # transition that opens a mission (e.g. GAME_BATTLE_EJECT ->
        # GAME_BATTLE_MANUAL on the first tick after startup), and checking
        # _in_mission before _start_mission ran dropped 3 of 4 takeovers in the
        # 2026-07-30 16:27 session.
        if next_state == "GAME_BATTLE_MANUAL" and prev_state != "GAME_BATTLE_MANUAL":
            if self._in_mission:
                self._total_manual_takeovers += 1
                self._current["manual_takeover_count"] += 1

    def finalize(self, run_id: str | None = None, extra: "dict | None" = None) -> dict:
        """Close any open mission, build session dict, write JSON. Returns session dict.

        extra: optional additional top-level sections to embed in the summary
        (e.g. the ADR 062 shadow-detector agreement block). Keys must not
        collide with the built-in summary fields.
        """
        got = self._lock.acquire(timeout=5.0)
        if not got:
            # Shutdown must still produce stats; a 5s-held lock here means
            # something is wedged anyway. Do NOT release a lock we never
            # acquired — locked() being True could be another thread's hold.
            logger.warning("MissionStatsTracker: lock timeout in finalize — proceeding unlocked")
        try:
            return self._finalize_locked(run_id, extra)
        finally:
            if got:
                self._lock.release()

    def _finalize_locked(self, run_id: str | None = None, extra: "dict | None" = None) -> dict:
        if self._in_mission:
            # Shutdown mid-mission: honour any outcome hint already recorded
            # (e.g. missiles_empty fired but the round never reached its end
            # screen) instead of discarding it as "unknown".
            self._end_mission(time.time(), self._pending_outcome or "unknown")

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
            "total_missile_evades": self._total_missile_evades,
            "spawn_crashes": {
                "window_s": _SPAWN_CRASH_WINDOW_S,
                "min_s": _SPAWN_CRASH_MIN_S,
                "count": len(self._spawn_crashes),
                "died_after_s": self._spawn_crashes,
                # ADR 082: sub-floor events — respawn re-detection churn,
                # not crashes. Reported so the class stays visible.
                "immediate_redetects": len(self._immediate_redetects),
                "redetect_after_s": self._immediate_redetects,
            },
            "missile_engagements": self._engagement_summary(),
            "avg_mission_duration_s": round(avg_duration, 1) if avg_duration is not None else None,
            "missions": self._missions,
        }
        if extra:
            for key, value in extra.items():
                if key not in self._summary:
                    self._summary[key] = value

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
            f"Missile evades    : {s.get('total_missile_evades', 0)}",
            f"Manual takeovers  : {s['total_manual_takeovers']}",
        ]

        sc = s.get("spawn_crashes") or {}
        if sc:
            lines.append(
                f"Spawn crashes     : {sc['count']}  "
                f"(death {sc.get('min_s', 0):.0f}-{sc['window_s']:.0f}s after restart)"
            )
            if sc.get("immediate_redetects"):
                lines.append(
                    f"  redetect churn  : {sc['immediate_redetects']}  "
                    f"(respawn re-fired under {sc.get('min_s', 0):.0f}s — not crashes)"
                )

        eng = s.get("missile_engagements") or {}
        if eng.get("engagements"):
            def surv(rate, total, died):
                if rate is None:
                    return "n/a"
                return f"{rate * 100:3.0f}%  ({total - died}/{total})"
            lines += [
                f"Missile engagements: {eng['engagements']}  "
                f"(survived {eng['window_s']:.0f}s after alert)",
                f"  with evade      : "
                f"{surv(eng['evaded_survival'], eng['evaded_total'], eng['evaded_died'])}",
                f"  without evade   : "
                f"{surv(eng['not_evaded_survival'], eng['not_evaded_total'], eng['not_evaded_died'])}",
            ]
        if path_line:
            lines.append(path_line.strip())
        lines.append("━" * 52)

        logger.info("\n".join(lines))

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    # -- ADR 070 V5: per-engagement survival -------------------------------

    def _open_engagement(self, ts: float) -> None:
        """Start an engagement on an incoming alert, or extend the current volley.

        The flare burst fires on the detection edge, so this is the earliest
        signal an engagement exists — before it is known whether an evade will
        follow.
        """
        if self._engagements:
            last = self._engagements[-1]
            if ts - last["last_alert_ts"] <= _VOLLEY_GROUP_S:
                last["last_alert_ts"] = ts
                last["alerts"] += 1
                return
        self._engagements.append({
            "ts": round(ts, 3),
            "last_alert_ts": ts,
            "alerts": 1,
            "evaded": False,
            "died_after_s": None,
        })

    def _mark_engagement_evaded(self, ts: float) -> None:
        if not self._engagements:
            return
        last = self._engagements[-1]
        if ts - last["last_alert_ts"] <= _EVADE_ATTRIBUTION_S:
            last["evaded"] = True

    def _attribute_death(self, ts: float) -> None:
        """Attribute a death to the most recent engagement still in window."""
        if not self._engagements:
            return
        last = self._engagements[-1]
        if last["died_after_s"] is None and (ts - last["ts"]) <= _ENGAGEMENT_WINDOW_S:
            last["died_after_s"] = round(ts - last["ts"], 1)

    def _engagement_summary(self) -> dict:
        """Survival rate split by whether an evade fired — the V5 measure."""
        buckets = {"evaded": [0, 0], "not_evaded": [0, 0]}  # [total, died]
        for e in self._engagements:
            key = "evaded" if e["evaded"] else "not_evaded"
            buckets[key][0] += 1
            if e["died_after_s"] is not None:
                buckets[key][1] += 1

        def rate(total: int, died: int):
            return round((total - died) / total, 3) if total else None

        return {
            "window_s": _ENGAGEMENT_WINDOW_S,
            "engagements": len(self._engagements),
            "evaded_total": buckets["evaded"][0],
            "evaded_died": buckets["evaded"][1],
            "evaded_survival": rate(*buckets["evaded"]),
            "not_evaded_total": buckets["not_evaded"][0],
            "not_evaded_died": buckets["not_evaded"][1],
            "not_evaded_survival": rate(*buckets["not_evaded"]),
            "detail": [
                {k: v for k, v in e.items() if k != "last_alert_ts"}
                for e in self._engagements
            ],
        }

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
            "missile_evade_count": 0,
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
