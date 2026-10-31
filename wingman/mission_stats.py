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

# The objectives flown through, as the session summary lists them: the kind's
# key in `ObjectiveTally.session_counts()` and its label. Kept as plain strings
# so this module stays free of the pursuit's imports; a test pins the keys.
_OBJECTIVE_ROWS = (
    ("air_superiority", "air superiority"),
    ("resupply", "resupply"),
    ("priority_target", "priority target"),
)

# ADR 163 / ADR 165: the evade trial's arms, in the order the summary prints them.
_TRIAL_ARMS = ("evade", "control")


def objectives_flown_lines(objectives: dict) -> "list[str]":
    """The objectives block: the total, then a row for each kind, at zero too.
    The session summary prints it, and the pursuit's tally prints it in green
    each time it counts one (`ObjectiveTally`, operator 2026-10-10)."""
    flown = [(label, int(objectives.get(kind, 0))) for kind, label in _OBJECTIVE_ROWS]
    return [f"Objectives flown  : {sum(count for _label, count in flown)}  "
            f"(flown through in pursuit; Design 015)"
            ] + [f"  {label:<15} : {count}" for label, count in flown]


def round_performance_lines(perf: dict) -> "list[str]":
    """The session's totals from the end-of-round PERFORMANCE panel, with the
    rounds they cover; the per-round figures only once a round was read."""
    read = int(perf.get("rounds_read", 0))
    rounds = read + int(perf.get("rounds_unread", 0))
    lines = [f"Round performance : {read} of {rounds} rounds read  "
             f"(the end-of-round PERFORMANCE panel)"]
    taken_over = int(perf.get("rounds_taken_over", 0))
    if taken_over:
        lines.append(f"  taken over      : {taken_over}  "
                     f"(a manual takeover; left out of these figures)")
    if read:
        k, d, a = (int(perf.get(key, 0)) for key in ("kills", "deaths", "assists"))
        score = int(perf.get("score", 0))
        lines += [
            f"  score           : {score}  ({score / read:.1f} per round read)",
            f"  K / D / A       : {k} / {d} / {a}  "
            f"({k / read:.1f} / {d / read:.1f} / {a / read:.1f} per round read)",
        ]
    return lines


def squadron_ranking_lines(readings: list) -> "list[str]":
    """ADR 166: the squadron's last reading of the session, and how many there
    were; nothing when none was read."""
    if not readings:
        return []
    last = readings[-1]
    return [f"Squadron ranking  : rank {int(last['rank'])}, trophies "
            f"{int(last['trophies']):,}  (last of {len(readings)} reading(s); ADR 166)"]


def trial_round_lines(by_arm: dict) -> "list[str]":
    """ADR 165: each arm's rounds in the evade trial, with their PERFORMANCE
    readings per round read; nothing until a round has carried an arm."""
    if not any(int((by_arm.get(arm) or {}).get("rounds", 0)) for arm in _TRIAL_ARMS):
        return []
    lines = []
    for arm in _TRIAL_ARMS:
        figures = by_arm.get(arm) or {}
        rounds, read = int(figures.get("rounds", 0)), int(figures.get("rounds_read", 0))
        line = f"  {arm + ' arm':<15} : {read} of {rounds} rounds read"
        if read:
            k, d, a = (int(figures.get(key, 0)) for key in ("kills", "deaths", "assists"))
            line += (f": score {int(figures.get('score', 0)) / read:.1f}, K / D / A "
                     f"{k / read:.1f} / {d / read:.1f} / {a / read:.1f} per round read")
        lines.append(line + "  (ADR 165)")
    return lines


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
                            click_to_detected, flare_burst_deployed, flare_reload,
                            missile_evade, evade_trial_evade, evade_trial_control
      on_fsm_transition() — chain after existing replay/capture FSM callback
      round_awaits_reading() / record_round_performance() — the end-of-round
                            PERFORMANCE panel: whether a round waits for it, its reading
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
        # ADR 137: respawns where the aircraft was NOT in a deliberate eject
        # dive (ADR 069's "trade an empty airframe for a rearmed one") and
        # still had primary missiles — an unintended death with ordnance
        # still aboard, not by design. Tracked toward zero. NOT specifically
        # a terrain metric despite the original framing: ADR 137's seventh
        # live trial measured 36 telemetry-confirmed occurrences across two
        # sessions and found only 11% in a steep dive consistent with a
        # ground impact — 89% read level or climbing, consistent with
        # getting shot down rather than flying into anything.
        self._total_crashes_with_missiles = 0

        # ADR 143: per-occurrence classification of the above, computed at
        # the same RespawnHandler.tick_detect emit site from two existing
        # signals (a recent confirmed incoming-missile alert vs. a recent
        # hard climb emergency) rather than the unreliable single-tick
        # pre-crash altitude/rate sample. "unclassified" is an expected
        # bucket, not a bug — see ADR 143 Non-Goal 5.
        self._total_died_armed_enemy_fire = 0
        self._total_died_armed_terrain = 0
        # Both signals recent: a missile alert and a hard emergency. Neither
        # decides the cause, so it is counted apart from both (2026-10-03).
        self._total_died_armed_contested = 0
        self._total_died_armed_unclassified = 0

        # Operator directive: confirmed map-boundary crossings (the RETURN
        # TO BATTLE banner, BoundaryPerceptionHandler._on_rtb_confirmed)
        # where the aircraft still had primary missiles aboard — same "still
        # armed" shape as _total_crashes_with_missiles above, applied to
        # flying out of bounds instead of dying.
        self._total_rtb_with_missiles = 0

        # Operator, 2026-10-26: each round's score and kills/deaths/assists from
        # the end-of-round PERFORMANCE panel (wingman/round_performance.py),
        # totalled to compare long runs. A round whose panel could not be read
        # is counted apart, so the totals say how many rounds they cover.
        # ADR 166: each reading of the squadron leaderboard, in order.
        self._squadron_rankings: list = []
        self._rounds_read = 0
        self._rounds_unread = 0
        self._round_totals = {"score": 0, "kills": 0, "deaths": 0, "assists": 0}
        # Operator, 2026-10-29: a round with a manual takeover is not wingman's
        # flying, so it is left out of the totals and only counted here.
        self._rounds_taken_over = 0
        # Action Item 005 (AI-005-3): the missions whose panel has been read or
        # tried. Each round gets one try: a second click-through of the same end
        # screens (ADR 087's lobby scan starts one) used to book an unread round.
        self._performance_tried: set[int] = set()

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

        elif event_name == "crash_with_missiles":
            self._total_crashes_with_missiles += 1

        elif event_name == "died_armed_enemy_fire":
            self._total_died_armed_enemy_fire += 1

        elif event_name == "died_armed_terrain":
            self._total_died_armed_terrain += 1

        elif event_name == "died_armed_contested":
            self._total_died_armed_contested += 1

        elif event_name == "died_armed_unclassified":
            self._total_died_armed_unclassified += 1

        elif event_name == "return_to_battle_with_missiles":
            self._total_rtb_with_missiles += 1

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

        elif event_name in ("evade_trial_evade", "evade_trial_control"):
            arm = event_name.rsplit("_", 1)[1]
            self._mark_engagement_arm(ts, arm)
            # ADR 165: the arm is drawn once a round, so the round's record keeps
            # the first one it sees.
            if self._in_mission and self._current.get("trial_arm") is None:
                self._current["trial_arm"] = arm

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

    def _round_awaiting_reading(self) -> "dict | None":
        """The mission whose PERFORMANCE panel is up next: the last one, ended
        on the end screen, its panel not yet read or tried. Hold the lock."""
        last = self._missions[-1] if self._missions else None
        if (self._in_mission or last is None or last["outcome"] != "click_to"
                or last["index"] in self._performance_tried):
            return None
        return last

    def round_awaits_reading(self) -> bool:
        """True while a round that ended on the end screen waits for its
        PERFORMANCE panel to be read (AI-005-3). The click-through asks before
        it reads; a second click-through of the same end screens finds none."""
        with self._lock:
            return self._round_awaiting_reading() is not None

    def record_round_performance(self, reading) -> None:
        """One round's PERFORMANCE panel: a `RoundPerformance` (score, kills,
        deaths, assists), or None when it could not be read. Arrives from the
        end-of-round click-through thread, after the round's mission has ended,
        and is booked against that round, once: a reading no round waits for
        (a second click-through, or wingman started on the end screen) is not
        counted (AI-005-3)."""
        with self._lock:
            mission = self._round_awaiting_reading()
            if mission is None:
                logger.debug("MissionStatsTracker: a round performance reading no round "
                             "waits for, not counted")
                return
            self._performance_tried.add(mission["index"])
            if reading is not None:
                mission["performance"] = {"score": reading.score, "kills": reading.kills,
                                          "deaths": reading.deaths, "assists": reading.assists}
            # A round the operator took over keeps its reading on its record,
            # but is left out of every round figure (operator, 2026-10-29).
            if mission.get("manual_takeover_count", 0) > 0:
                self._rounds_taken_over += 1
                return
            if reading is None:
                self._rounds_unread += 1
                return
            self._rounds_read += 1
            for key, value in mission["performance"].items():
                self._round_totals[key] += value

    def record_squadron_ranking(self, reading, ts: float) -> None:
        """One reading of the squadron leaderboard (ADR 166): a
        `SquadronReading` (rank, trophies), at `ts`."""
        with self._lock:
            self._squadron_rankings.append(
                {"ts": round(ts, 3), "rank": int(reading.rank), "trophies": int(reading.trophies)})

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
            "total_crashes_with_missiles": self._total_crashes_with_missiles,
            "total_died_armed_enemy_fire": self._total_died_armed_enemy_fire,
            "total_died_armed_terrain": self._total_died_armed_terrain,
            "total_died_armed_contested": self._total_died_armed_contested,
            "total_died_armed_unclassified": self._total_died_armed_unclassified,
            "total_rtb_with_missiles": self._total_rtb_with_missiles,
            "round_performance": {
                "rounds_read": self._rounds_read,
                "rounds_unread": self._rounds_unread,
                "rounds_taken_over": self._rounds_taken_over,
                **self._round_totals,
            },
            "squadron_rankings": list(self._squadron_rankings),
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
            "evade_trial_rounds": self._trial_rounds(),
            "avg_mission_duration_s": round(avg_duration, 1) if avg_duration is not None else None,
            "missions": self._missions,
        }
        if extra:
            for key, value in extra.items():
                if key not in self._summary:
                    self._summary[key] = value

        self._write_json(self._summary)
        return self._summary

    def print_summary(self, objectives: "dict | None" = None) -> None:
        """Log a formatted session summary. Call after finalize().

        objectives: the session's count of objectives flown through, by kind
        (`ObjectiveTally.session_counts`). Printed as its own block; left out
        of the stats file. None prints no block."""
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
        ]
        lines += round_performance_lines(s.get("round_performance") or {})
        lines += trial_round_lines(s.get("evade_trial_rounds") or {})
        lines += squadron_ranking_lines(s.get("squadron_rankings") or [])
        lines += [
            f"Total respawns    : {s['total_respawns']}",
            f"Total flare bursts: {s['total_flare_bursts']}{bursts_per}",
            f"Flare reloads     : {s['total_flare_reloads']}",
            f"Missile evades    : {s.get('total_missile_evades', 0)}",
            f"Manual takeovers  : {s['total_manual_takeovers']}",
            f"Died armed        : {s.get('total_crashes_with_missiles', 0)}  "
            f"(missiles unused at death; ADR 143)",
            f"  enemy fire      : {s.get('total_died_armed_enemy_fire', 0)}",
            f"  terrain crash   : {s.get('total_died_armed_terrain', 0)}",
            f"  contested       : {s.get('total_died_armed_contested', 0)}  "
            f"(missile alert and dive emergency both recent)",
            f"  unclassified    : {s.get('total_died_armed_unclassified', 0)}",
            f"RTB w/ missiles   : {s.get('total_rtb_with_missiles', 0)}  "
            f"(confirmed map-boundary crossings with primary missiles"
            f" still aboard)",
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
            if eng.get("trial_evade_total") or eng.get("trial_control_total"):
                lines.append(
                    f"  pursuit trial   : evade arm "
                    f"{surv(eng['trial_evade_survival'], eng['trial_evade_total'], eng['trial_evade_died'])}"
                    f", control arm "
                    f"{surv(eng['trial_control_survival'], eng['trial_control_total'], eng['trial_control_died'])}"
                    f"  (ADR 163)")
        # Operator, 2026-10-09: "i only want it to print total counts during
        # wingman session summary." Shown at zero too: a session that flew
        # through nothing is what the line is read for.
        if objectives is not None:
            lines += objectives_flown_lines(objectives)
        # CR-018-17: a reflex that raised is otherwise invisible here.
        failures = s.get("suppressed_failures") or {}
        if failures:
            lines.append(
                "Handler failures  : " + ", ".join(
                    f"{name} {count}" for name, count in sorted(failures.items())))
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
            "trial_arm": None,
            "died_after_s": None,
        })

    def _mark_engagement_evaded(self, ts: float) -> None:
        if not self._engagements:
            return
        last = self._engagements[-1]
        if ts - last["last_alert_ts"] <= _EVADE_ATTRIBUTION_S:
            last["evaded"] = True

    def _mark_engagement_arm(self, ts: float, arm: str) -> None:
        """ADR 163: the pursuit trial's arm for the engagement this alert opened.

        The arm is what the engagement was assigned, so the trial is read by arm,
        whether or not the evade could start (a dive recovery, an eject)."""
        if not self._engagements:
            return
        last = self._engagements[-1]
        if ts - last["last_alert_ts"] <= _EVADE_ATTRIBUTION_S:
            last["trial_arm"] = arm

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
        trial = {"evade": [0, 0], "control": [0, 0]}         # ADR 163, by arm
        for e in self._engagements:
            key = "evaded" if e["evaded"] else "not_evaded"
            buckets[key][0] += 1
            if e["died_after_s"] is not None:
                buckets[key][1] += 1
            arm = e.get("trial_arm")
            if arm in trial:
                trial[arm][0] += 1
                if e["died_after_s"] is not None:
                    trial[arm][1] += 1

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
            "trial_evade_total": trial["evade"][0],
            "trial_evade_died": trial["evade"][1],
            "trial_evade_survival": rate(*trial["evade"]),
            "trial_control_total": trial["control"][0],
            "trial_control_died": trial["control"][1],
            "trial_control_survival": rate(*trial["control"]),
            "detail": [
                {k: v for k, v in e.items() if k != "last_alert_ts"}
                for e in self._engagements
            ],
        }

    def _trial_rounds(self) -> dict:
        """ADR 165: the rounds each arm of the evade trial was drawn for, and the
        sums of their PERFORMANCE readings. A round is a mission that ended on the
        end screen, the only kind a reading attaches to, and that wingman flew
        throughout: one with a manual takeover is left out (operator, 2026-10-29)."""
        by_arm = {}
        for arm in _TRIAL_ARMS:
            rounds = [m for m in self._missions
                      if m.get("trial_arm") == arm and m.get("outcome") == "click_to"
                      and not m.get("manual_takeover_count")]
            read = [m["performance"] for m in rounds if m.get("performance")]
            by_arm[arm] = {"rounds": len(rounds), "rounds_read": len(read),
                           **{key: sum(p[key] for p in read) for key in self._round_totals}}
        return by_arm

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
            "performance": None,
            "trial_arm": None,          # ADR 165: the evade trial's arm for this round
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
