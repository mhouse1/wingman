"""Per-concern tick-loop handlers (ADR 060 Phase 2).

Each handler owns the state that previously lived as `main()` loop-locals and
`nonlocal` closures, so concerns that must not interact can no longer reach
each other's variables. `main()` keeps setup, wiring, and the loop skeleton and
calls handlers in a fixed, documented order.

Contract shared by every handler:

- `on_state_change(new_state, prev_state)` — called once per FSM state change,
  before the tick body, so a handler can arm or clear its own state.
- `tick(...)` — called once per loop iteration. A handler returns True when the
  tick loop must `continue` (skip the remaining handlers this iteration),
  mirroring the `continue` statements the extracted blocks used.

Handlers never share mutable state. Where two concerns genuinely interact, the
interaction goes through an explicit argument, a return value, or a
`GameEvent` — never a variable both can write (ADR 060 Phase 2 rule 2).
"""

import logging
import threading
import time

from .analyzer import GameState

logger = logging.getLogger(__name__)


def update_waiting_fallback(
    analyzer,
    frame,
    elapsed_waiting: float,
    play_visible: bool,
    score: int,
    consecutive: int,
    *,
    enabled: bool,
    diff_threshold: float,
    score_threshold: int,
    consecutive_required: int,
    min_elapsed_s: float,
    logger=logger,
):
    """Update GAME_WAITING fallback confidence and report if promotion should fire.

    Pure function (moved verbatim from main._update_waiting_fallback): returns
    (score, consecutive, should_trigger, diff).
    """
    if not enabled or "CANCEL" not in analyzer.crops or elapsed_waiting < min_elapsed_s:
        return score, consecutive, False, None

    if play_visible:
        if score > 0 or consecutive > 0:
            logger.debug("GAME_WAITING fallback reset: PLAY/READY visible")
        return 0, 0, False, None

    diff = analyzer.compute_waiting_cancel_diff(frame)
    if diff is None:
        return score, consecutive, False, None

    score_step = 1
    if diff >= diff_threshold:
        score_step += 1

    new_score = score + score_step
    new_consecutive = consecutive + 1
    logger.debug(
        "GAME_WAITING fallback: play_not_visible=%s diff=%.3f score=%d consecutive=%d",
        True,
        diff,
        new_score,
        new_consecutive,
    )

    should_trigger = (
        new_score >= score_threshold
        and new_consecutive >= consecutive_required
    )
    return new_score, new_consecutive, should_trigger, diff


class AmmoEventsHandler:
    """Flares, missiles, and the padlock target-spread counter.

    Owns: `last_flare_reload_ts`, `no_missiles_zero_streak`, `battle_started_ts`,
    `missile_ignore_until`, `last_incoming_alert_ts`, and the padlock
    `missiles_fired_since_padlock` / `last_missile_count_for_padlock` pair.

    The post-respawn suppression window that the respawn flow used to set by
    writing a shared `missile_ignore_until` is now the explicit
    `suppress_after_respawn()` call (ADR 060 Phase 2 rule 2).

    Ordering note (preserved): incoming-flare deployment runs FIRST — it is
    higher priority than respawn and must run before the respawn block's
    `continue` — then low-flares, then no-missiles.
    """

    def __init__(self, analyzer, ctrl, mission_cfg, *, perf_tracker=None,
                 stats_tracker=None, emit_capture_event=None,
                 flare_reload_cooldown_s: float = 30.0):
        self._analyzer = analyzer
        self._ctrl = ctrl
        self._perf = perf_tracker
        self._stats = stats_tracker
        self._emit_capture_event = emit_capture_event or (lambda _name: None)
        self._flare_reload_cooldown_s = flare_reload_cooldown_s

        self._abort_grace_s = float(mission_cfg.get("no_missiles_abort_grace_s", 6.0))
        self._consecutive_required = int(
            mission_cfg.get("no_missiles_consecutive_required", 2))
        self._padlock_spread_missiles = int(mission_cfg.get("padlock_spread_missiles", 2))

        self._last_flare_reload_ts = 0.0
        self._zero_streak = 0
        self._battle_started_ts = 0.0
        self._ignore_until = 0.0
        self._last_incoming_alert_ts = 0.0
        self._fired_since_padlock = 0
        self._last_missile_count = None

    # -- state --------------------------------------------------------------

    def on_state_change(self, new_state, prev_state=None):
        if new_state == GameState.GAME_BATTLE:
            self._battle_started_ts = time.time()
            self._fired_since_padlock = 0
            self._last_missile_count = None
        self._zero_streak = 0

    def suppress_after_respawn(self, seconds: float = 10.0):
        """Ignore missile/incoming events for `seconds` — called by the respawn flow."""
        self._ignore_until = time.time() + seconds

    @property
    def battle_started_ts(self) -> float:
        return self._battle_started_ts

    # -- tick ---------------------------------------------------------------

    def tick_missile_count(self, missiles_snapshot, current_game_state) -> None:
        """Zero-streak reset and padlock target-spread bookkeeping."""
        if missiles_snapshot is not None and missiles_snapshot > 0:
            self._zero_streak = 0

        # Target-spread: after N cumulative missiles fired in GAME_BATTLE, press
        # padlock twice to switch target so missiles spread across enemy jets.
        if current_game_state != GameState.GAME_BATTLE or missiles_snapshot is None:
            return
        if self._last_missile_count is not None and missiles_snapshot < self._last_missile_count:
            self._fired_since_padlock += self._last_missile_count - missiles_snapshot
            if self._fired_since_padlock >= self._padlock_spread_missiles:
                logger.info("Controller: %d missiles fired — switching padlock target",
                            self._fired_since_padlock)
                self._ctrl.padlock_target_switch()
                self._fired_since_padlock = 0
        if missiles_snapshot > (self._last_missile_count or 0):
            # Missiles reloaded — reset so a pre-reload partial count isn't carried over
            self._fired_since_padlock = 0
        self._last_missile_count = missiles_snapshot

    def deploy_flares_on_new_incoming(self) -> bool:
        """Deploy flares in a burst when a new incoming OCR detection arrives."""
        incoming_detected, _, _ = self._analyzer.get_incoming_cache_result()
        incoming_ts = self._analyzer.get_incoming_cache_timestamp()

        if not (incoming_detected and incoming_ts > self._last_incoming_alert_ts):
            return False

        if time.time() < self._ignore_until:
            logger.debug("Missile alert suppressed — post-respawn grace period (%.1fs remaining)",
                         self._ignore_until - time.time())
            self._last_incoming_alert_ts = incoming_ts
            return False

        logger.info("\033[95m🚀 INCOMING MISSILE DETECTED - Deploying flares\033[0m")
        self._last_incoming_alert_ts = incoming_ts
        if self._perf is not None:
            try:
                self._perf.record_reaction(time.time() - incoming_ts)
            except Exception as e:
                logger.warning("PerformanceTracker: record_reaction failed: %s", e)
        if self._stats is not None:
            self._stats.on_event("flare_burst_deployed", time.time())

        def _flare_burst():
            for _ in range(3):
                self._ctrl.deploy_flares(hold_seconds=0.05, block=True, ignore_cancel=True)
                time.sleep(0.3)
            logger.info("\033[95m🚀 Flare burst complete\033[0m")

        threading.Thread(target=_flare_burst, daemon=True).start()
        return True

    def handle_low_flares(self) -> None:
        """Press SPECIAL_ABILITY to reload flares when count reaches 2."""
        self._analyzer.low_flares_event.clear()
        if self._analyzer.game_state != GameState.GAME_BATTLE:
            return
        elapsed = time.time() - self._last_flare_reload_ts
        if elapsed < self._flare_reload_cooldown_s:
            logger.debug("Low-flares event: reload suppressed by cooldown (%.1fs remaining)",
                         self._flare_reload_cooldown_s - elapsed)
            return
        self._ctrl.reload_flares()
        self._last_flare_reload_ts = time.time()
        if self._stats is not None:
            self._stats.on_event("flare_reload", self._last_flare_reload_ts)

    def handle_no_missiles(self) -> None:
        """End mission and eject when missile count reaches zero."""
        self._analyzer.no_missiles_event.clear()
        if not self._ctrl.is_mission_running():
            self._zero_streak = 0
            return
        if self._analyzer.game_state != GameState.GAME_BATTLE:
            # Ejecting is an auto-mode behavior. Without this gate, the narrow
            # window where a cancelled mission is still tearing down during a
            # manual takeover could inject NOSE_DOWN+AFTERBURNER into the
            # player's manual flight (and fire eject_started from a state
            # where it is invalid).
            self._zero_streak = 0
            return
        currently_respawning, _, _ = self._analyzer.get_respawn_cache_result()
        if currently_respawning:
            logger.debug("No-missiles suppressed — respawn screen active")
            self._zero_streak = 0
            return
        if (self._battle_started_ts > 0
                and (time.time() - self._battle_started_ts) < self._abort_grace_s):
            logger.debug(
                "No-missiles suppressed — post-mission-start grace (%.1fs remaining)",
                self._abort_grace_s - (time.time() - self._battle_started_ts),
            )
            self._zero_streak = 0
            return
        if time.time() < self._ignore_until:
            logger.debug("No-missiles suppressed — post-respawn grace (%.1fs remaining)",
                         self._ignore_until - time.time())
            self._zero_streak = 0
            return

        self._zero_streak += 1
        if self._zero_streak < self._consecutive_required:
            logger.debug(
                "No-missiles event awaiting confirmation (%d/%d)",
                self._zero_streak,
                self._consecutive_required,
            )
            return

        self._zero_streak = 0
        self._emit_capture_event("missiles_empty")
        self._analyzer.trigger_event("eject_started")
        self._ctrl.eject_and_dive(
            on_complete=lambda: (
                self._analyzer.trigger_event("eject_complete")
                if self._analyzer.game_state == GameState.GAME_BATTLE_EJECT
                else None
            )
        )

    def tick_events(self) -> None:
        """Fire the ammo event handlers whose analyzer events are set."""
        if self._analyzer.low_flares_event.is_set():
            self.handle_low_flares()
        if self._analyzer.no_missiles_event.is_set():
            self.handle_no_missiles()


class EnemyPresenceHandler:
    """Disengage when ENEMY_CLOSE_BY has shown no red for the idle window.

    Owns: `enemy_last_seen_ts`. The clock is armed on GAME_BATTLE entry and
    re-armed by the respawn flow — the latter is an explicit `arm()` call from
    the tick loop rather than a shared variable both concerns write
    (ADR 060 Phase 2 rule 2).

    Ordering note: runs after the ammo handlers and before target tracking,
    matching the extracted block's position.
    """

    def __init__(self, analyzer, ctrl, *, disengage_after_s: float = 30.0):
        self._analyzer = analyzer
        self._ctrl = ctrl
        self._disengage_after_s = disengage_after_s
        self._last_seen_ts = 0.0   # 0 = not in battle yet

    def arm(self):
        """Start (or restart) the idle clock — battle entry and post-respawn."""
        self._last_seen_ts = time.time()

    def on_state_change(self, new_state, prev_state=None):
        if new_state == GameState.GAME_BATTLE:
            self.arm()   # assume enemy present on battle entry

    @property
    def last_seen_ts(self) -> float:
        return self._last_seen_ts

    def tick(self, frame, current_game_state) -> bool:
        if current_game_state != GameState.GAME_BATTLE or self._last_seen_ts <= 0:
            return False
        if self._analyzer.detect_enemy_red(frame):
            self._last_seen_ts = time.time()
        elif (time.time() - self._last_seen_ts >= self._disengage_after_s
                and self._ctrl.is_mission_running()):
            logger.info("\033[93m↩ No enemy in ENEMY_CLOSE_BY for %.0fs — disengaging\033[0m",
                        self._disengage_after_s)
            self._last_seen_ts = time.time()  # reset to avoid re-triggering
            self._ctrl.disengage_roll_right()
        return False


class WaitingFallbackHandler:
    """GAME_WAITING confirmation: CANCEL scan, queue-diff fallback, PLAY re-click.

    Owns: `game_waiting_since`, `last_cancel_scan_ts`, `last_play_reclick_ts`,
    `play_ever_absent`, and the fallback score/consecutive counters.

    Ordering note (preserved from the tick loop): this runs after the FSM
    state-change block and before the GAME_END_B / GAME_STARTING_STALLED
    guards. It only acts in GAME_WAITING.
    """

    def __init__(self, analyzer, ctrl, mission_cfg, *, live_capture=None,
                 timeout_s: float = 180.0, cancel_scan_interval_s: float = 3.0):
        self._analyzer = analyzer
        self._ctrl = ctrl
        self._live_capture = live_capture
        self._timeout_s = timeout_s
        self._cancel_scan_interval_s = cancel_scan_interval_s

        self._enabled = bool(mission_cfg.get("waiting_fallback_enabled", True))
        self._diff_threshold = float(mission_cfg.get("waiting_fallback_diff_threshold", 0.08))
        self._score_threshold = int(mission_cfg.get("waiting_fallback_score_threshold", 4))
        self._consecutive_required = int(
            mission_cfg.get("waiting_fallback_consecutive_required", 2))
        self._min_elapsed_s = float(mission_cfg.get("waiting_fallback_min_elapsed_s", 6.0))
        # Re-click interval when PLAY was absent (matchmaking may be active) vs
        # when PLAY stayed visible the whole time (the click clearly missed).
        self._reclick_interval = float(mission_cfg.get("play_reclick_interval", 45.0))
        self._reclick_missed_interval = float(
            mission_cfg.get("play_reclick_missed_interval", 10.0))

        self._waiting_since = 0.0
        self._last_cancel_scan_ts = 0.0
        self._last_reclick_ts = 0.0
        self._play_ever_absent = False
        self._score = 0
        self._consecutive = 0

    # -- state --------------------------------------------------------------

    def _reset_fallback(self):
        self._score = 0
        self._consecutive = 0

    def on_state_change(self, new_state, prev_state=None):
        if new_state == GameState.GAME_WAITING:
            now = time.time()
            self._waiting_since = now
            self._last_cancel_scan_ts = now   # first scan after the interval, not immediately
            self._last_reclick_ts = now       # don't re-click immediately either
            self._play_ever_absent = False
        else:
            self._waiting_since = 0.0
        self._reset_fallback()

    @property
    def waiting_since(self) -> float:
        return self._waiting_since

    # -- tick ---------------------------------------------------------------

    def tick(self, frame, current_game_state) -> bool:
        """Returns True when the tick loop should `continue`."""
        if current_game_state != GameState.GAME_WAITING or self._waiting_since <= 0:
            return False

        elapsed_waiting = time.time() - self._waiting_since
        if elapsed_waiting > self._timeout_s:
            logger.warning(
                "GAME_WAITING timeout after %.0fs — CANCEL never detected; returning to GAME_LOBBY",
                elapsed_waiting)
            self._analyzer.trigger_event("waiting_timeout")
            self._waiting_since = 0.0
            self._reset_fallback()
            return False

        if time.time() - self._last_cancel_scan_ts < self._cancel_scan_interval_s:
            return False
        self._last_cancel_scan_ts = time.time()

        if self._analyzer.scan_region_for_cancel(frame):
            logger.info(
                "\033[92m✓ CANCEL detected (%.1fs) — matchmaking confirmed → GAME_STARTING\033[0m",
                elapsed_waiting)
            self._analyzer.trigger_event("cancel_detected")  # on_enter_GAME_STARTING fires game-starting loop
            self._reset_fallback()
            if self._live_capture is not None:
                # on_event("cancel_detected") already fired inside trigger_event via the
                # FSM_TRANSITION subscriber. Evaluate twice here with the explicit
                # pre-transition state so the debounce completes on this exact CANCEL
                # frame — relying on the subscriber's ordering is error-prone because
                # post-transition callbacks run before it.
                _now = time.time()
                self._live_capture.evaluate(frame, "GAME_WAITING", _now)
                self._live_capture.evaluate(frame, "GAME_WAITING", _now + 1e-6)
            return False

        crop = next((c for c in ("PLAY", "READY") if c in self._analyzer.crops), None)
        visible_crop = self._analyzer.scan_region_for_play_button(frame) if crop else None
        play_visible = visible_crop is not None
        if not play_visible:
            self._play_ever_absent = True

        self._score, self._consecutive, fallback_triggered, _ = update_waiting_fallback(
            self._analyzer,
            frame,
            elapsed_waiting,
            play_visible,
            self._score,
            self._consecutive,
            enabled=self._enabled,
            diff_threshold=self._diff_threshold,
            score_threshold=self._score_threshold,
            consecutive_required=self._consecutive_required,
            min_elapsed_s=self._min_elapsed_s,
            logger=logger,
        )

        if fallback_triggered:
            logger.info(
                "\033[92m✓ GAME_WAITING confirmed via QUEUE_FALLBACK (%.1fs) — "
                "matchmaking confirmed → GAME_STARTING\033[0m",
                elapsed_waiting,
            )
            self._analyzer.trigger_event("cancel_detected")
            self._reset_fallback()
            return True   # the extracted block ended in `continue`

        effective_interval = (self._reclick_interval if self._play_ever_absent
                              else self._reclick_missed_interval)
        if crop and time.time() - self._last_reclick_ts >= effective_interval:
            # Only re-click if PLAY/READY is actually visible — clicking PLAY while
            # matchmaking is in progress cancels it. If PLAY isn't visible the game
            # is still processing the previous click; leave it alone.
            if visible_crop:
                reason = "click missed" if not self._play_ever_absent else "returned from matchmaking"
                logger.info(
                    "GAME_WAITING: CANCEL not found (%.1fs) and %s visible — re-clicking (%s)",
                    elapsed_waiting, visible_crop, reason)
                self._last_reclick_ts = time.time()
                self._play_ever_absent = False  # reset: treat next window as a fresh click
                self._ctrl.click_crop(self._analyzer.crops[visible_crop], block=False,
                                      count=1, region_name=visible_crop)
            else:
                logger.debug(
                    "GAME_WAITING: CANCEL not found (%.1fs) but PLAY not visible — "
                    "matchmaking in progress, waiting",
                    elapsed_waiting)
                self._last_reclick_ts = time.time()  # reset timer to avoid spamming OCR
        elif crop:
            logger.debug(
                "GAME_WAITING: CANCEL not found (%.1fs) — waiting %.1fs before re-click (%s)",
                elapsed_waiting, effective_interval - (time.time() - self._last_reclick_ts),
                "click missed" if not self._play_ever_absent else "returned from matchmaking")
        return False
