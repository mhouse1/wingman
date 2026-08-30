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

import collections
import json
import logging
import threading
import time

from .analyzer import GameState, BATTLE_STATES
from .behavior_tree import (
    TACTIC_CLIMB,
    TACTIC_DISENGAGE,
    TACTIC_EJECT,
    TACTIC_ENGAGE,
    TACTIC_REGROUP,
    TACTIC_EJECT,
    TACTIC_RESPAWN_WAIT,
    TACTIC_MISSILE_EVADE,
    AnalyzerSnapshot,
    build_tree,
    make_climb_condition,
    make_snapshot_writer,
    selected_tactic,
)
from .engage_nav import RING_LONG, RING_MID, RING_SHORT, EngageNavigator, bin_rings

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


# Single definition lives in analyzer; aliased to keep the local name.
_BATTLE_STATES = BATTLE_STATES



def _fmt_rate(rate) -> str:
    """Altitude rate for the BT log line, or why it is missing (ADR 086 d2)."""
    return "n/a" if rate is None else f"{rate:+.0f}m/s"


def _fmt_ttg(alt, rate) -> str:
    """Predicted seconds to ground, for the BT log line (ADR 086 d2).

    Logged EVERY tick, not only when the trigger fires. The 2026-08-21 19:42
    review could not tell "the trigger is armed and correctly quiet" from "the
    trigger is dead" because neither state emitted anything — a safety-critical
    condition must be observable while it is silent.
    """
    if alt is None or rate is None or rate >= 0:
        return "n/a"
    return f"{alt / -rate:.0f}s"


class TrackingHudHandler:
    """Target tracking (HSV contour + proportional roll) and the HUD snapshot.

    Owns the TargetTracker and HudRenderer instances and the per-tick tracking
    observation the HUD consumes, so the two are never wired through a shared
    loop variable.

    Ordering note (preserved): runs after enemy presence and before respawn
    detection — the HUD must render before the respawn block's `continue`.
    """

    def __init__(self, target_tracker, hud_renderer, analyzer, ctrl, tracking_cfg):
        self._tracker = target_tracker
        self._hud = hud_renderer
        self._analyzer = analyzer
        self._ctrl = ctrl
        self._ctl_cfg = {
            "deadband": float(tracking_cfg.get("deadband", 0.05)),
            "kp": float(tracking_cfg.get("kp", 0.30)),
            "min_hold_sec": float(tracking_cfg.get("min_hold_sec", 0.08)),
            "max_hold_sec": float(tracking_cfg.get("max_hold_sec", 0.35)),
            "cooldown_sec": float(tracking_cfg.get("command_cooldown_sec", 0.15)),
        }

    def on_state_change(self, new_state, prev_state=None):
        """Reset tracking when leaving the battle states entirely."""
        if prev_state in _BATTLE_STATES and new_state not in _BATTLE_STATES:
            self._tracker.reset()

    def tick(self, frame, current_game_state, game_state) -> bool:
        # Target tracking — only during GAME_BATTLE (not GAME_BATTLE_MANUAL) and
        # only when a mission is running: no autonomous roll without mission control.
        tracking_obs = None
        if (self._tracker.enabled
                and current_game_state == GameState.GAME_BATTLE
                and self._ctrl.is_mission_running()):
            tracking_obs = self._tracker.update(frame)
            err = tracking_obs.get("error_norm")
            if err is not None and tracking_obs.get("visible"):
                cmd = self._ctrl.orient_nose_to_target(err, **self._ctl_cfg)
                if cmd is not None:
                    logger.debug(
                        "Tracker: roll_%s  err=%.2f  mode=%s",
                        cmd, err, tracking_obs["mode"],
                    )

        # HUD renderer — annotated snapshot; always runs in GAME_BATTLE when enabled.
        if self._hud is not None and current_game_state in (
            GameState.GAME_BATTLE, GameState.GAME_BATTLE_MANUAL
        ):
            self._hud.maybe_render(
                frame,
                tracking_obs,
                current_game_state.name,
                game_state.get("health"),
                self._analyzer.get_ammo_missiles(),
                self._analyzer.get_ammo_flares(),
            )
        return False


class RespawnHandler:
    """Respawn detection, the post-respawn restart flow, and the alive-event disposition.

    Owns: `respawn_state`, `respawn_cooldown_until`, `respawn_clear_since`.

    This is the ADR 059 (health-gated restart) / ADR 061 (eject termination) /
    ADR 064 (dual-sensor fallback) flow. Interactions with other concerns are
    explicit collaborator calls — `enemy_presence.arm()` and
    `ammo_events.suppress_after_respawn()` — rather than shared loop variables;
    coupling the eject interrupt to the restart cooldown by block nesting is
    exactly the CR-013-4 defect this structure prevents.

    Ordering note (preserved): runs after the ammo events and before the
    click-to-continue logging. `tick_detect()` returning True means the tick
    loop must sleep-and-continue while the respawn screen is up.
    """

    def __init__(self, analyzer, ctrl, mission_cfg, *, enemy_presence, ammo_events,
                 behavior_tree=None, live_capture=None, emit_capture_event=None,
                 disposition_fn, respawn_state_enum, cooldown_s: float = 10.0):
        self._analyzer = analyzer
        self._ctrl = ctrl
        self._enemy_presence = enemy_presence
        self._ammo_events = ammo_events
        self._behavior_tree = behavior_tree
        self._live_capture = live_capture
        self._emit_capture_event = emit_capture_event or (lambda _name: None)
        self._disposition_fn = disposition_fn
        self._RespawnState = respawn_state_enum
        self._cooldown_s = cooldown_s
        self._clear_stability_s = float(mission_cfg.get("respawn_clear_stability_s", 1.5))
        # SAF-001: a respawn does not revoke the operator's takeover.
        self._manual_persists_through_respawn = bool(
            (mission_cfg.get("manual_takeover", {}) or {}).get(
                "persist_through_respawn", True))

        self._state = respawn_state_enum.IDLE
        self._cooldown_until = 0.0
        self._clear_since = 0.0   # timestamp since the respawn cache has been continuously false

    # -- state --------------------------------------------------------------

    @property
    def state(self):
        return self._state

    def to_idle(self):
        """Clear the respawn latch (GAME_END_B: a match end is not a respawn flow)."""
        self._state = self._RespawnState.IDLE

    def note_respawn_screen(self, is_respawning: bool):
        """Track how long the respawn screen has been continuously clear."""
        if is_respawning:
            self._clear_since = 0.0
        elif self._clear_since == 0.0:
            self._clear_since = time.time()

    def note_gameplay_resumed(self) -> None:
        """Respawn screen cleared: drop the latch. No scheduled restart —
        the health alive transition restarts the mission when health returns."""
        if self._state == self._RespawnState.RESPAWNING:
            logger.info("\033[92m✓ Gameplay resumed — mission restarts on health return\033[0m")
            self._state = self._RespawnState.IDLE

    # -- alive transition ---------------------------------------------------

    def handle_alive_transition(self) -> None:
        """Restart mission immediately when health transitions dead → alive.

        This is the ONLY post-respawn restart path (the scheduled/delayed
        restart machinery was removed 2026-07-31): as soon as health returns,
        the mission restarts. Because alive_event is one-shot and health often
        returns while respawn OCR is still flapping, deferrals RE-ARM the event
        instead of swallowing it — the handler retries every tick until the
        respawn-clear stability window is met, then restarts.

        @relation(SAF-002, scope=function)
        """
        analyzer, ctrl = self._analyzer, self._ctrl
        analyzer.alive_event.clear()
        self._enemy_presence.arm()  # reset so the idle clock starts fresh after respawn
        if self._behavior_tree is not None:
            self._behavior_tree.arm_absence_clock()  # 3.1b analogue (ADR 024)

        # Only early-restart when respawn has remained clear for a short stability window.
        # This avoids relaunching while respawn OCR/health signals are still flapping.
        if self._clear_since == 0.0:
            logger.debug("HEALTH ALIVE deferred — respawn screen still detected")
            analyzer.alive_event.set()  # retry next tick; do not lose the transition
            return
        clear_elapsed = time.time() - self._clear_since
        if clear_elapsed < self._clear_stability_s:
            logger.debug(
                "HEALTH ALIVE deferred — respawn clear stability %.2fs/%.2fs",
                clear_elapsed,
                self._clear_stability_s,
            )
            analyzer.alive_event.set()  # retry next tick; do not lose the transition
            return

        # Explicit disposition of every case — ADR 061 rule 3: the one-shot
        # event is never consumed silently (the 2026-08-01 08:00 incident lost
        # the only restart signal for a life to an unlogged state-gate miss).
        disposition = self._disposition_fn(
            analyzer.game_state, analyzer.alive_after_observed_death
        )
        if disposition == "terminate_eject":
            # Respawn evidence during an eject: health returned after an OCR-
            # observed death, so the respawn happened but the overlay was missed.
            # Stop the eject (releases afterburner; FSM exits GAME_BATTLE_EJECT
            # via eject_complete) and keep the event armed so the restart fires
            # through the normal path once state returns to GAME_BATTLE.
            logger.info(
                "\033[92m💚 HEALTH ALIVE after observed death during eject — "
                "terminating eject (respawn overlay missed), re-arming restart\033[0m"
            )
            ctrl.stop_eject_sequence()
            analyzer.alive_event.set()
            return
        if disposition == "consume_spurious":
            logger.debug("HEALTH ALIVE consumed — spurious eject-start transition (no observed death)")
            return
        if disposition == "consume_other":
            logger.debug(
                "HEALTH ALIVE consumed — state %s does not auto-restart",
                analyzer.game_state.name,
            )
            return

        # ADR 076 d2: the aircraft is alive in battle — start the spawn
        # guard's release-overlap window regardless of which restart branch
        # follows (restart, missiles-empty skip, restart-disabled: the guard
        # must hand off in all of them).
        ctrl.notify_spawn_alive()

        if not ctrl.is_auto_respawn_restart_enabled():
            logger.debug("HEALTH ALIVE consumed — auto-respawn restart disabled")
            return
        if ctrl.is_mission_running():
            if ctrl.is_mission_teardown_in_progress():
                # The lock is held only by the CANCELLED mission thread
                # unwinding (the v1.6.29 teardown race). With the scheduled
                # fallback removed, consuming the event here would lose the
                # only remaining restart path for this life — retry instead.
                logger.debug("HEALTH ALIVE deferred — cancelled mission still tearing down")
                analyzer.alive_event.set()
            else:
                logger.debug("HEALTH ALIVE consumed — mission already running")
            return
        missiles = analyzer.get_ammo_missiles()
        if missiles is not None and missiles == 0:
            logger.info("\033[92m💚 HEALTH ALIVE — missiles empty, skipping restart\033[0m")
            return
        logger.info("\033[92m💚 HEALTH ALIVE — restarting mission immediately\033[0m")
        ctrl.restart_last_mission()
        self._emit_capture_event("restart_last_mission")
        self._state = self._RespawnState.IDLE

    # -- tick ---------------------------------------------------------------

    def tick_detect(self, frame, game_state, current_game_state) -> bool:
        """Detect and handle a respawn. Returns True when the loop must
        sleep-and-continue (respawn screen still up)."""
        analyzer, ctrl = self._analyzer, self._ctrl

        # Respawn from overlay OCR, or (ADR 064 dual mode) from the health
        # detector's composite evidence when OCR missed the episode.
        health_fallback = analyzer.health_respawn_event.is_set()
        if health_fallback:
            analyzer.health_respawn_event.clear()
            logger.info("\033[93m💛 HEALTH-FALLBACK RESPAWN accepted by main loop (ADR 064 dual)\033[0m")
        if not (game_state.get('is_respawning') or health_fallback):
            return False

        # Interrupt any in-progress eject_and_dive immediately on any detected respawn,
        # independent of the mission-restart dedup cooldown below — a real respawn screen
        # means afterburner should release now, not hold until the 120s safety timeout.
        ctrl.stop_eject_sequence()
        if self._state == self._RespawnState.IDLE:
            if time.time() < self._cooldown_until:
                logger.debug("RESPAWN seen but suppressed by cooldown (%.1fs remaining)",
                             self._cooldown_until - time.time())
            else:
                logger.info("\033[91m⚠ RESPAWN DETECTED - Cancelling active missions\033[0m")
                self._cooldown_until = time.time() + self._cooldown_s
                self._ammo_events.suppress_after_respawn(self._cooldown_s)
                self._enemy_presence.arm()  # reset so the idle clock starts fresh after respawn
                if self._behavior_tree is not None:
                    self._behavior_tree.arm_absence_clock()  # 3.1b analogue (ADR 024)
                ctrl.cancel_mission()
                analyzer.reset_health_for_respawn()
                # A death invalidates any pending (re-armed) alive event from the
                # previous life — without this, a deferred HEALTH ALIVE could
                # restart the mission after the NEXT respawn clears but before
                # health actually returns.
                analyzer.alive_event.clear()
                self._emit_capture_event("respawn_detected")
                # Live capture for the respawn frame itself rides the
                # RESPAWN_DETECTED event, which fires from the background OCR
                # thread with the exact respawn-screen frame — the main loop's
                # `frame` has already advanced past the overlay by the time
                # is_respawning surfaces from the cache.
                if current_game_state == GameState.GAME_BATTLE_MANUAL:
                    # Death ends manual takeover. Fire the P2_040 capture BEFORE
                    # the FSM transition so the screenshot still shows the
                    # manual-mode HUD, then return to auto: the mission restarts
                    # as soon as health returns. (An earlier stay-manual-through-
                    # respawn design left the aircraft flying uncommanded after
                    # every death — the scheduled restart was GAME_BATTLE-gated
                    # and the log promised a restart that could never fire,
                    # observed 2026-07-31 07:42.)
                    if self._live_capture is not None:
                        _cap_now = time.time()
                        self._live_capture.on_event("respawn_detected", _cap_now)
                        self._live_capture.evaluate(frame, "GAME_BATTLE_MANUAL", _cap_now)
                        self._live_capture.evaluate(frame, "GAME_BATTLE_MANUAL", _cap_now + 1e-6)
                    # SAF-001 / ADR 059: whether a respawn returns the
                    # aircraft to wingman is the operator's call, not the
                    # game's. Persisting is the default: taking over and then
                    # losing the aircraft 15 s later to the next death is not
                    # manual control in any useful sense (measured 2026-08-30 —
                    # both takeover windows ended on respawn detection, at 15 s
                    # and 85 s).
                    #
                    # The 2026-07-31 07:42 failure this replaced was NOT the
                    # persistence itself but a restart promised and never
                    # fired: the scheduler was GAME_BATTLE-gated while the FSM
                    # sat in manual. Here nothing is scheduled while manual, and
                    # the operator hands back explicitly with the auto-mission
                    # key, so no unfireable promise is made.
                    if self._manual_persists_through_respawn:
                        logger.info(
                            "SAF-001: respawn while in manual — staying in "
                            "GAME_BATTLE_MANUAL. Press the auto-mission key to "
                            "return control to wingman.")
                    else:
                        analyzer.trigger_event("respawn_reset")
                # Only promise an auto restart when wingman will actually own
                # the aircraft; in manual the operator does.
                if not (self._manual_persists_through_respawn
                        and analyzer.game_state == GameState.GAME_BATTLE_MANUAL):
                    ctrl.set_auto_respawn_restart(True)  # always restart after respawn
                # ADR 076 d1: death is latched — hold nose-up through the
                # respawn screen so the new life's first frames are already
                # pitching up (spawn-into-terrain anomaly). Inert while the
                # screen is up; the alive handoff below releases it.
                ctrl.start_spawn_guard()
                # Wait for the cancelled mission thread to release its lock so the
                # health-alive restart can't be skipped by a teardown race
                # (is_mission_running would read True).
                logger.info("Waiting for mission lock to release before restart...")
                for _ in range(50):
                    if not ctrl.is_mission_running():
                        break
                    time.sleep(0.1)
                else:
                    logger.warning("Timeout waiting for mission lock release; restart may be delayed.")
                # Latch: dedupes re-detection while the screen persists.
                self._state = self._RespawnState.RESPAWNING
                logger.info("Respawn screen active — mission restarts when health returns")

        logger.info("\033[91mRESPAWN ACTIVE (%.0f%% confidence)\033[0m",
                    game_state.get('respawn_confidence', 0) * 100)
        return True


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
                 flare_reload_cooldown_s: float = 30.0,
                 bt_owns_eject: bool = False):
        self._analyzer = analyzer
        self._ctrl = ctrl
        self._perf = perf_tracker
        self._stats = stats_tracker
        self._emit_capture_event = emit_capture_event or (lambda _name: None)
        self._flare_reload_cooldown_s = flare_reload_cooldown_s
        # ADR 024 3.1b: when the behavior tree owns eject actuation, a
        # confirmed no-missiles event raises a sticky flag for the Eject leaf
        # instead of firing eject_and_dive here. The debounce and every
        # suppression gate stay in this handler either way — the BT consumes
        # only the confirmed verdict, never the raw zero read (the 2026-08-08
        # shadow-session gate).
        self._bt_owns_eject = bool(bt_owns_eject)
        self._missiles_empty_confirmed = False

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

    def on_state_change(self, new_state, _prev_state=None):
        if new_state == GameState.GAME_BATTLE:
            self._battle_started_ts = time.time()
            self._fired_since_padlock = 0
            self._last_missile_count = None
        self._zero_streak = 0
        self._missiles_empty_confirmed = False

    def suppress_after_respawn(self, seconds: float = 10.0):
        """Ignore missile/incoming events for `seconds` — called by the respawn flow."""
        self._ignore_until = time.time() + seconds
        self._missiles_empty_confirmed = False

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
            now = time.time()
            try:
                self._perf.record_reaction(now - incoming_ts)
            except Exception as e:
                logger.warning("PerformanceTracker: record_reaction failed: %s", e)
            # ADR 096: split that total into detector versus dispatch. Guarded
            # separately so a diagnostic failure cannot cost the flare burst,
            # and skipped when the marks are absent (pre-instrumentation logs).
            try:
                frame_ts, pass_ts, detect_done_ts = \
                    self._analyzer.get_incoming_latency_marks()
                if frame_ts and detect_done_ts and detect_done_ts >= pass_ts:
                    self._perf.record_reaction_segments(
                        capture_to_pass=max(0.0, pass_ts - frame_ts),
                        detect=max(0.0, detect_done_ts - pass_ts),
                        dispatch=max(0.0, now - detect_done_ts))
                    logger.debug(
                        "ADR096 reaction split: capture->pass %.3fs, detect %.3fs, "
                        "dispatch %.3fs (total %.3fs)",
                        pass_ts - frame_ts, detect_done_ts - pass_ts,
                        now - detect_done_ts, now - frame_ts)
            except Exception as e:
                logger.debug("ADR096: reaction split unavailable: %s", e)
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
        if self._bt_owns_eject:
            # ADR 024 3.1b: hand the confirmed verdict to the Eject leaf. The
            # flag is sticky until consumed (the BT ticks later in the same
            # loop iteration) and cleared by any suppression reset above.
            self._missiles_empty_confirmed = True
            return
        self.fire_eject()

    def consume_missiles_empty_confirmed(self) -> bool:
        """Return-and-clear the confirmed no-missiles verdict (ADR 024 3.1b)."""
        confirmed = self._missiles_empty_confirmed
        self._missiles_empty_confirmed = False
        return confirmed

    def fire_eject(self) -> None:
        """Actuate the eject sequence: capture event, FSM transition, dive.

        One implementation for both callers — the legacy no-missiles path and
        the behavior tree's Eject leaf (ADR 024 3.1b).
        """
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

    def on_state_change(self, new_state, _prev_state=None):
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


class BehaviorTreeHandler:
    """ADR 024 Phase 3 behavior tree: tactic selection + 3.1a geometry cutover.

    mode: off | shadow | active.
    - **shadow**: build one frozen AnalyzerSnapshot per tick, tick the
      selector, log the selected tactic — actuate nothing.
    - **active**: same, plus an Engage selection actuates ring-engage
      geometry (Design 003 / ADR 028, FR-005) through the mission-agnostic
      EngageNavigator: steer via orient_nose_to_target with coarse gains,
      orbit via the open-loop roll cadence. This absorbs the retired
      EngageNavHandler; one minimap scan per tick serves both the snapshot
      and the actuation. With an ammo handler wired (3.1b), the Eject leaf
      actuates via AmmoEventsHandler.fire_eject on the DEBOUNCED
      missiles_empty_confirmed verdict — never the raw zero read (the
      2026-08-08 shadow-session gate) — and the Disengage leaf fires
      disengage_roll_right with legacy fire-once-and-reset semantics.
      Evade stays selection-only: threshold unset, no Controller tactic.

    Arbitration with target tracking is unchanged: steer intents share
    orient_nose_to_target's single cooldown timestamp, so the fine tracking
    loop wins whenever both want the roll axis.

    Owns: the tree, the snapshot writer, the minimap-based `enemy_absent`
    clock (ring-occupancy replacement for the legacy ENEMY_CLOSE_BY timer),
    the EngageNavigator, and the orbit cadence timer.
    """

    def __init__(self, analyzer, ctrl, bt_cfg, j20_cfg=None, minimap_cfg=None,
                 ammo_events=None, stats_tracker=None):
        self._analyzer = analyzer
        self._ctrl = ctrl
        self._mode = str(bt_cfg.get("mode", "off")).lower()
        self._enemy_last_seen_ts = 0.0
        # ADR 028 revision 4 / Design 010 instrumentation.
        self._friendly_components = None
        self._rtb_active = False
        self._boundary_crossings = 0
        self._boundary_approaches = 0
        self._boundary_near_since = 0.0
        self._rtb_false_positives = 0
        self._boundary_near_frac = float(
            (minimap_cfg or {}).get("boundary_near_frac", 0.25))
        # ~30 s of lead-up at a 1.5 s tick. Bounded: a session must not grow a
        # trace buffer, and only the approach matters, not the whole mission.
        self._boundary_trace = collections.deque(
            maxlen=int((minimap_cfg or {}).get("boundary_trace_ticks", 20)))
        self._session_start = time.time()
        self._last_selection = "none"
        self._ammo_events = ammo_events
        # ADR 070: the evade entry event is emitted from the actuator wrapper —
        # the Controller holds no stats tracker, so this is the only seam.
        self._stats = stats_tracker
        j20_cfg = j20_cfg or {}
        self._dry_run = bool(j20_cfg.get("attack_mode_dry_run", False))
        self._nav = EngageNavigator(j20_cfg, minimap_cfg)
        self._ctl_cfg = {
            "deadband": self._nav.deadband_norm,
            "kp": float(j20_cfg.get("coarse_kp", 0.5)),
            "min_hold_sec": float(j20_cfg.get("coarse_min_hold_s", 0.15)),
            "max_hold_sec": float(j20_cfg.get("coarse_max_hold_s", 0.6)),
            "cooldown_sec": float(j20_cfg.get("coarse_cooldown_s", 2.0)),
        }
        self._orbit_hold_s = float(j20_cfg.get("orbit_roll_hold_s", 0.3))
        self._orbit_interval_s = float(j20_cfg.get("orbit_roll_interval_s", 2.0))
        self._last_orbit_roll_ts = 0.0
        self._last_nav_mode = self._nav.mode
        # ADR 073 Phase 3.2a: while the Climb leaf is disabled it stays OUT of
        # the selector (a selection-only leaf would pre-empt Engage actuation —
        # not shadow). Instead an independent instance of the same condition is
        # evaluated against the same frozen snapshot and transitions are
        # logged as would-select evidence.
        climb_cfg = bt_cfg.get("climb", {}) or {}
        self._climb_shadow = None
        self._climb_shadow_active = False
        self._climb_shadow_since = 0.0
        self._climb_band = (climb_cfg.get("enter_below_alt"),
                            climb_cfg.get("exit_above_alt"))
        self._climb_confirm = int(climb_cfg.get("confirm_reads", 1))
        # ADR 075: armed altitude-sustain band and the evade fuel reserve. The
        # start_fn wrapper picks the sustain target when the aircraft is above
        # the emergency band — the leaf is shared, the targets are not.
        _sustain_cfg = climb_cfg.get("sustain", {}) or {}
        self._sustain_enabled = bool(_sustain_cfg.get("enabled", False))
        self._sustain_exit_alt = _sustain_cfg.get("exit_above_alt")
        self._sustain_max_s = float(_sustain_cfg.get("max_climb_s", 90.0))
        self._climb_fuel_reserve = float(climb_cfg.get("fuel_reserve_pct", 0.0))
        # ADR 083 d1/d2: predictive exit lead, sustain climbs only.
        self._climb_exit_lead_s = float(climb_cfg.get("exit_lead_s", 0.0))
        self._last_altitude: "float | None" = None
        if not bool(climb_cfg.get("enabled", False)):
            self._climb_shadow = make_climb_condition(
                *self._climb_band, confirm_reads=self._climb_confirm)
        if self.enabled:
            # ADR 024 3.1b: in active mode with an ammo handler wired, the
            # Eject and Disengage leaves actuate their Controller tactics.
            actuators = {}
            if self.active and ammo_events is not None:
                actuators.update({
                    TACTIC_EJECT: (ammo_events.fire_eject, ctrl.is_ejecting),
                    TACTIC_DISENGAGE: (self._start_disengage,
                                       ctrl.is_disengage_running),
                })
            # ADR 070: MissileEvade actuates when active and enabled; disabled
            # leaves the leaf selection-only (the shadow pattern), so agreement
            # can be checked against the flare-burst log before keys are pressed.
            me_cfg = bt_cfg.get("missile_evade", {}) or {}
            if self.active and bool(me_cfg.get("enabled", False)):
                actuators[TACTIC_MISSILE_EVADE] = (self._start_missile_evade,
                                                   ctrl.is_missile_evading)
            # ADR 073 3.2b: Climb actuates when active and enabled — the leaf
            # is only inserted in that case (see build_tree), so there is no
            # in-tree selection-only variant to wire.
            if self.active and bool(climb_cfg.get("enabled", False)):
                actuators[TACTIC_CLIMB] = (self._start_climb, ctrl.is_climbing)
            self._tree = build_tree(
                bt_cfg, actuators=actuators or None,
                regroup_enabled=bool((minimap_cfg or {}).get("regroup_enabled", False)))
            self._writer = make_snapshot_writer()

    def _start_climb(self) -> None:
        """Climb leaf start_fn (ADR 075): pick the band the selection came from.

        Below the emergency enter threshold (or with altitude unknown) this is
        a terrain-avoidance climb: Controller defaults, no fuel held back —
        terrain outranks the evade reserve. Otherwise the sustain band selected
        it: climb to the operating altitude with the evade fuel reserve
        honoured, so the burner is released once fuel drops to the reserve.
        """
        alt = self._last_altitude
        emergency_enter = self._climb_band[0]
        is_sustain = (self._sustain_enabled
                      and self._sustain_exit_alt is not None
                      and alt is not None
                      and (emergency_enter is None or alt >= float(emergency_enter)))
        if is_sustain:
            self._ctrl.climb_mode(target_alt=float(self._sustain_exit_alt),
                                  max_s=self._sustain_max_s,
                                  fuel_floor_pct=self._climb_fuel_reserve,
                                  exit_lead_s=self._climb_exit_lead_s)
        else:
            self._ctrl.climb_mode()

    def _start_disengage(self) -> None:
        """Disengage leaf start_fn: fire the roll, then re-arm the absence
        clock — the legacy handler's fire-once-and-reset semantics, so the
        next disengage requires a fresh full absence window."""
        self._ctrl.disengage_roll_right()
        self._enemy_last_seen_ts = time.time()

    def _start_missile_evade(self) -> None:
        """MissileEvade leaf start_fn (ADR 070): start the hold and count the
        event. The stats call sits after the start so a duplicate-suppressed
        trigger (d8) still counts the EVENT — the quantity V5 compares against
        flare_burst_count."""
        self._ctrl.missile_evade_mode()
        if self._stats is not None:
            self._stats.on_event("missile_evade", time.time())

    def arm_absence_clock(self) -> None:
        """Restart the enemy-absence clock — called by the respawn flow, the
        3.1b analogue of EnemyPresenceHandler.arm()."""
        self._enemy_last_seen_ts = time.time()

    @property
    def enabled(self) -> bool:
        return self._mode in ("shadow", "active")

    @property
    def active(self) -> bool:
        return self._mode == "active"

    def on_state_change(self, new_state, prev_state=None):
        """Arm the absence clock on battle entry; reset the navigator on exit."""
        if new_state == GameState.GAME_BATTLE:
            self._enemy_last_seen_ts = time.time()
        if prev_state in _BATTLE_STATES and new_state not in _BATTLE_STATES:
            self._nav.reset()
            self._last_orbit_roll_ts = 0.0
            self._last_nav_mode = self._nav.mode

    def tick(self, frame, current_game_state, game_state) -> bool:
        if not self.enabled:
            return False
        now = time.time()
        components = self._analyzer.detect_enemy_map_components(frame)
        # ADR 028 revision 4: scanned every tick but consumed only when no enemy
        # is on the minimap, so it costs one extra mask over an already-decoded
        # crop and never competes with an enemy contact.
        self._friendly_components = self._analyzer.detect_friendly_map_components(frame)
        rings = bin_rings(components or [])
        if (rings[RING_SHORT].count or rings[RING_MID].count or rings[RING_LONG].count):
            self._enemy_last_seen_ts = now
        absent_s = now - self._enemy_last_seen_ts if self._enemy_last_seen_ts else 0.0
        snapshot_obj = self._analyzer.get_telemetry()
        altitude = None
        altitude_rate = None
        if snapshot_obj is not None and snapshot_obj.altitude_fresh():
            altitude = snapshot_obj.altitude.stable_value
            altitude_rate = getattr(snapshot_obj.altitude, "rate", None)
        # Stored for _start_climb, which runs inside tree.tick() below and
        # needs the altitude the selection was made against (ADR 075).
        self._last_altitude = altitude
        is_respawning, _, _ = self._analyzer.get_respawn_cache_result()
        incoming, _, _ = self._analyzer.get_incoming_cache_result()
        missiles_empty_confirmed = False
        if self.active and self._ammo_events is not None:
            missiles_empty_confirmed = (
                self._ammo_events.consume_missiles_empty_confirmed())
        snap = AnalyzerSnapshot(
            health=game_state.get("health"),
            missiles=self._analyzer.get_ammo_missiles(),
            flares=self._analyzer.get_ammo_flares(),
            ring_short=rings[RING_SHORT].count,
            ring_mid=rings[RING_MID].count,
            ring_long=rings[RING_LONG].count,
            enemy_absent_seconds=absent_s,
            altitude=altitude,
            is_respawning=bool(is_respawning),
            incoming_detected=bool(incoming),
            mission_running=self._ctrl.is_mission_running(),
            game_state=current_game_state,
            missiles_empty_confirmed=missiles_empty_confirmed,
            fuel_pct=self._analyzer.get_afterburner_fuel_pct(),
            altitude_rate=altitude_rate,
            friendly_contacts=len(self._friendly_components or []),
        )
        self._writer.set("snapshot", snap)
        self._tree.tick()
        selection = selected_tactic(self._tree)
        if selection != self._last_selection:
            logger.info("BT[%s]: tactic %s → %s", self._mode, self._last_selection, selection)
            self._last_selection = selection
        logger.debug(
            "BT[%s]: selected=%s missiles=%s rings=%d/%d/%d absent=%.0fs "
            "respawn=%s alt=%s alt_rate=%s ttg=%s fuel=%s mission=%s",
            self._mode, selection, snap.missiles, snap.ring_short, snap.ring_mid,
            snap.ring_long, absent_s, snap.is_respawning, altitude,
            _fmt_rate(altitude_rate), _fmt_ttg(altitude, altitude_rate),
            snap.fuel_pct, snap.mission_running,
        )
        if self._climb_shadow is not None:
            # Outside GAME_BATTLE the Idle leaf would own selection, and the
            # freeze-on-None policy would otherwise carry a stale would-select
            # through the lobby — force-release there instead of evaluating.
            if snap.game_state == GameState.GAME_BATTLE:
                would = self._climb_shadow(snap)
            else:
                would = False
                if self._climb_shadow_active:
                    # Drop the closure's frozen hysteresis state too, or the
                    # next battle would open on last battle's verdict.
                    self._climb_shadow = make_climb_condition(
                        *self._climb_band, confirm_reads=self._climb_confirm)
            if would != self._climb_shadow_active:
                if would:
                    self._climb_shadow_since = now
                    logger.info(
                        "BT[shadow-climb]: would_select=True alt=%s "
                        "selected=%s respawn=%s", altitude, selection,
                        snap.is_respawning)
                else:
                    logger.info(
                        "BT[shadow-climb]: would_select=False alt=%s held=%.0fs",
                        altitude, now - self._climb_shadow_since)
                self._climb_shadow_active = would
        # After the selection, so each buffered record carries the tactic that
        # was actually chosen on that tick — the question a crossing trace has
        # to answer is what wingman was doing on the way out.
        self._instrument_boundary(frame, now, snap, selection)
        # SAF-001: never command flight outside GAME_BATTLE. In
        # GAME_BATTLE_MANUAL the selector already yields Idle, but the gate is
        # stated here too so a future leaf cannot reintroduce commanded flight
        # while the operator has taken over.
        _may_fly = (self.active and snap.mission_running
                    and snap.game_state == GameState.GAME_BATTLE)
        if _may_fly and selection in (TACTIC_ENGAGE, TACTIC_REGROUP):
            # Regroup: enemy components are empty by the condition that selected
            # the leaf, so the navigator falls through to the friendly centroid.
            self._actuate_engage(components, altitude, now)
        elif _may_fly and selection == TACTIC_CLIMB:
            # ADR 028 revision 5. Climb owns the PITCH axis (ADR 073); the
            # navigator commands only roll (ADR 028: "this policy only commands
            # the roll axis"). The selector's one-tactic-at-a-time model forced
            # an either/or the axes do not require, and Climb takes ~43% of
            # battle ticks — so for nearly half of every battle nothing steered
            # horizontally at all.
            #
            # Measured 2026-08-30: a confirmed boundary crossing ran entirely
            # under Climb with friendly icons visible the whole way (4, 3, 3, 1)
            # and the boundary closing 0.29R to 0.02R. Regroup had a signal and
            # was outranked by a tactic that was not using the roll axis.
            #
            # Orbit is suppressed here: it is a sustained roll hold, and holding
            # roll through a climb is a different manoeuvre from correcting
            # heading during one.
            self._actuate_engage(components, altitude, now, steer_only=True)
        return False

    def _instrument_boundary(self, frame, now, snap=None, selection=None):
        """Count map-boundary approaches and crossings. Design 010.

        INSTRUMENTATION ONLY — nothing steers on this. It exists because the
        question "did the navigator change reduce boundary crossings?" is
        currently unanswerable: there is no detector, so no soak of any length
        can measure the outcome. Counting them is the prerequisite for tuning
        anything, including whether a guard is needed at all.
        """
        try:
            # The banner only means anything while the aircraft is flying.
            # Live 2026-08-30 06:20: the colour test fired one tick after an
            # eject, on the fireball — bright red, centre screen, exactly where
            # the banner sits. EJECTED's dark plate does not match, but the
            # explosion does. Gate on actually being in battle and not ejecting
            # rather than chase a threshold the fireball would eventually beat.
            flying = (snap is not None
                      and snap.game_state == GameState.GAME_BATTLE
                      and selection not in (TACTIC_EJECT, TACTIC_RESPAWN_WAIT)
                      and not snap.is_respawning)
            crossed = self._analyzer.detect_return_to_battle(frame) if flying else False
            reading = self._analyzer.detect_map_boundary(frame)

            # Every tick goes into the buffer, crossing or not. The buffer is
            # the point: a crossing logged on its own says it happened, not why.
            self._boundary_trace.append({
                "t": round(now - self._session_start, 1),
                "dist": None if reading is None else round(reading[0], 3),
                "fwd": None if reading is None else round(reading[1], 3),
                "tactic": selection,
                "rings": None if snap is None else
                         [snap.ring_short, snap.ring_mid, snap.ring_long],
                "friendly": None if snap is None else snap.friendly_contacts,
                "alt": None if snap is None or snap.altitude is None
                       else round(snap.altitude),
                "alt_rate": None if snap is None or snap.altitude_rate is None
                            else round(snap.altitude_rate, 1),
                "outside": crossed,
            })

            if crossed and not self._rtb_active:
                self._boundary_crossings += 1
                logger.warning(
                    "\033[93m🗺  MAP BOUNDARY: crossed — RETURN TO BATTLE "
                    "(crossing %d this session)\033[0m", self._boundary_crossings)
                # The trace is dumped only once OCR CONFIRMS the crossing.
                # Measured 2026-08-30: 26 colour triggers in 26 minutes, all 26
                # retracted, and the false positives reach red_frac 0.773 —
                # REDDER than the real banner at 0.390 — so no threshold
                # separates them. Explosions simply fill the region. Dumping 20
                # records per trigger would put ~180 unnecessary multi-KB
                # WARNING lines into a three-hour soak.
                pending = list(self._boundary_trace)
                self._analyzer.confirm_return_to_battle_async(
                    frame,
                    lambda ok, text: self._on_rtb_confirmed(ok, text, pending))
            elif not crossed and self._rtb_active:
                logger.info("🗺  MAP BOUNDARY: back inside")
            self._rtb_active = crossed

            if reading is None:
                self._boundary_near_since = 0.0
                logger.debug("BOUNDARY: no reading")
                return
            dist, forward = reading
            # Per-tick at DEBUG so the thresholds can be calibrated from the
            # distribution rather than only from the ticks that happen to
            # precede a crossing.
            logger.debug("BOUNDARY: dist=%.3f fwd=%+.3f", dist, forward)
            near = forward > 0 and dist <= self._boundary_near_frac
            if near and not self._boundary_near_since:
                self._boundary_near_since = now
                self._boundary_approaches += 1
                logger.info(
                    "🗺  MAP BOUNDARY: ahead at %.2fR (approach %d this session)",
                    dist, self._boundary_approaches)
            elif not near:
                self._boundary_near_since = 0.0
        except Exception as e:
            logger.debug("Boundary instrumentation failed: %s", e)

    def _on_rtb_confirmed(self, detected, text, trace=None):
        """OCR verdict on the banner. The colour test is the cheap trigger; this
        is the arbiter of the COUNT, so a false positive does not silently
        inflate the crossings figure the tuning depends on.

        Never raises — it runs on an OCR pool thread, where an exception would
        be swallowed anyway.
        """
        try:
            if detected:
                logger.warning("🗺  MAP BOUNDARY: OCR confirms banner (%r)", text)
                if trace:
                    logger.warning(
                        "🗺  MAP BOUNDARY trace (last %d ticks before the "
                        "crossing): %s", len(trace), json.dumps(trace))
                return
            self._boundary_crossings = max(0, self._boundary_crossings - 1)
            self._rtb_false_positives += 1
            logger.warning(
                "🗺  MAP BOUNDARY: OCR did not confirm (read %r) — retracted, "
                "%d confirmed crossing(s), %d false positive(s) this session",
                text, self._boundary_crossings, self._rtb_false_positives)
        except Exception:
            pass

    def _actuate_engage(self, components, altitude, now, steer_only: bool = False):
        """3.1a: the Engage selection drives ring-engage geometry (ported from
        the retired EngageNavHandler; log labels kept for parser continuity)."""
        intent = self._nav.update(
            components, altitude, now,
            friendly_components=getattr(self, "_friendly_components", None),
        )
        if intent.mode != self._last_nav_mode:
            logger.info(
                "EngageNav: mode %s → %s (%s)",
                self._last_nav_mode, intent.mode, intent.reason,
            )
            self._last_nav_mode = intent.mode
        logger.debug(
            "EngageNav: mode=%s kind=%s reason=%s err=%s alt=%s",
            intent.mode, intent.kind, intent.reason, intent.error_norm, altitude,
        )
        if intent.kind == "steer":
            if self._dry_run:
                logger.info(
                    "EngageNav[dry-run]: would roll err=%.2f (%s)",
                    intent.error_norm, intent.mode,
                )
            else:
                cmd = self._ctrl.orient_nose_to_target(intent.error_norm, **self._ctl_cfg)
                if cmd is not None:
                    logger.debug("EngageNav: roll_%s err=%.2f", cmd, intent.error_norm)
        elif intent.kind == "orbit":
            if steer_only:
                # Concurrent with a climb: correcting heading is compatible with
                # a pitch manoeuvre, holding a roll through one is not.
                logger.debug("EngageNav: orbit suppressed during climb")
                return
            if now - self._last_orbit_roll_ts >= self._orbit_interval_s:
                self._last_orbit_roll_ts = now
                if self._dry_run:
                    logger.info(
                        "EngageNav[dry-run]: would orbit roll_%s hold=%.2fs",
                        intent.direction, self._orbit_hold_s,
                    )
                elif intent.direction == "left":
                    self._ctrl.roll_left(hold_seconds=self._orbit_hold_s, block=False)
                    logger.debug("EngageNav: orbit roll_left")
                else:
                    self._ctrl.roll_right(hold_seconds=self._orbit_hold_s, block=False)
                    logger.debug("EngageNav: orbit roll_right")


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

    def on_state_change(self, new_state, _prev_state=None):
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


class UnknownAnomalyRecorder:
    """ADR 074: archive screenshots of unclassifiable GAME_UNKNOWN episodes.

    A GAME_UNKNOWN state that persists past ``screenshot_after_s`` means the
    screen is showing something neither the classifier nor any calibrated
    popup crop recognises — the 2026-08-15 "Event refresh" stranding was
    diagnosed only because a screenshot happened to be taken afterwards.
    This recorder captures that evidence automatically: timestamped frames
    saved under ``dir`` for later triage and popup-crop calibration
    (``make add-crops``), so each new stranding variant can be added to
    stall handling instead of being lost when the session ends.

    Normal startup classification (~4 s in GAME_UNKNOWN) never triggers a
    capture. Follows the ADR 060 handler contract; owns only its own state.
    """

    def __init__(self, cfg: "dict | None", clock=time.time):
        import cv2  # heavy import kept local: recorder is constructed once
        self._cv2 = cv2
        cfg = cfg or {}
        self._after_s = float(cfg.get("screenshot_after_s", 30.0))
        self._recapture_s = float(cfg.get("recapture_interval_s", 120.0))
        self._max_per_episode = int(cfg.get("max_per_episode", 5))
        self._dir = str(cfg.get("dir", "test_screenshots/unknown_anomalies"))
        # Grace period after the FIRST dismissal attempt of an episode. Measured
        # from the first attempt, not the latest, so a popup that is detected and
        # re-clicked every cycle without clearing still gets captured as evidence
        # that handling failed.
        self._dismiss_grace_s = float(cfg.get("dismiss_grace_s", 20.0))
        # ADR 093: the screenshot cap used to silence the WARNING too, so the
        # 2026-08-24 livelock stopped complaining at 511s and ran another 100
        # minutes in silence. Captures stay capped — the fifth frame of an
        # unchanging screen adds nothing — but the complaint continues on a
        # doubling interval for as long as the condition holds.
        self._stuck_warn_interval_s = float(cfg.get("stuck_warn_interval_s", 300.0))
        self._stuck_warn_max_s = float(cfg.get("stuck_warn_max_interval_s", 1800.0))
        self._last_stuck_warn_ts = 0.0
        self._stuck_warns = 0
        self._clock = clock
        self._unknown_since = 0.0
        self._captured = 0
        self._last_capture_ts = 0.0
        # ADR 087: a classified state whose defining crops all read empty is
        # just as unclassifiable as GAME_UNKNOWN, but produced no evidence
        # because the capture was gated on the state name.
        self._blackout_since = 0.0
        self._blackout_last_ts = 0.0
        # LOBBY_STALL re-emits every 10s while the blackout lasts; allow three
        # missed beats before calling the episode over.
        self._blackout_idle_s = 30.0
        self._first_dismiss_ts = 0.0
        self._dismiss_attempts = 0
        self._dismiss_popups: "list[str]" = []
        self._cleared_popups: "list[str]" = []

    def on_state_change(self, current_game_state, _prev_game_state) -> None:
        if current_game_state == GameState.GAME_UNKNOWN:
            self._unknown_since = self._clock()
            self._captured = 0
            self._last_capture_ts = 0.0
        else:
            self._unknown_since = 0.0
        # ADR 087: any state change means classification is producing fresh
        # answers again, so a blackout episode cannot span it.
        self._blackout_since = 0.0
        self._blackout_last_ts = 0.0
        self._last_stuck_warn_ts = 0.0
        self._stuck_warns = 0
        # Dismissal history belongs to the episode: leaving GAME_UNKNOWN means
        # handling worked (or was never needed), so it must not carry forward.
        self._first_dismiss_ts = 0.0
        self._dismiss_attempts = 0
        self._dismiss_popups = []
        self._cleared_popups = []

    def note_lobby_stall(self) -> None:
        """Record a lobby blackout beat (ADR 087).

        The analyzer re-emits LOBBY_STALL every 10s while no lobby crop
        matches. A gap longer than ``_blackout_idle_s`` means the scan
        recovered, so the next beat opens a fresh episode.

        The clock starts at the FIRST beat, not at the true start of the
        blackout, so ``stuck_for`` understates the stall by the analyzer's own
        10s threshold. That is deliberate: it keeps this handler independent
        of that constant, and erring late never produces a spurious capture.
        """
        now = self._clock()
        if (self._blackout_since == 0.0
                or now - self._blackout_last_ts > self._blackout_idle_s):
            self._blackout_since = now
            self._captured = 0
            self._last_capture_ts = 0.0
            self._last_stuck_warn_ts = 0.0
            self._stuck_warns = 0
        self._blackout_last_ts = now

    def _blackout_active(self, now: float) -> bool:
        return (self._blackout_since != 0.0
                and now - self._blackout_last_ts <= self._blackout_idle_s)

    def note_dismiss_attempt(self, popup: str) -> None:
        """Record that popup dismissal was attempted during this episode.

        Called from the LOBBY_POPUP_CLICK handler. Suppresses the capture for
        ``dismiss_grace_s`` so a popup that IS handled never produces an
        anomaly screenshot; if the stall outlives the grace window the capture
        proceeds and names the popups that failed to clear it.
        """
        now = self._clock()
        if self._first_dismiss_ts == 0.0:
            self._first_dismiss_ts = now
        self._dismiss_attempts += 1
        if popup not in self._dismiss_popups:
            self._dismiss_popups.append(popup)

    def note_popup_absent(self) -> None:
        """Record that a popup scan completed with nothing on screen.

        Distinguishes "the dismissal failed" from "the dismissal worked but the
        screen is still unclassifiable". Without this the recorder infers
        failure from the state alone and mislabels a slow classification as a
        failed dismissal (observed live 2026-08-20 00:33:37).
        """
        if self._first_dismiss_ts:
            self._cleared_popups = list(self._dismiss_popups)
            self._first_dismiss_ts = 0.0
            self._dismiss_popups = []

    def _warn_still_stuck(self, now: float, episode: str, stuck_for: float) -> None:
        """Keep complaining after the screenshot cap (ADR 093).

        Silence must mean healthy. The interval doubles up to a ceiling so a
        multi-hour stall neither spams the log nor goes quiet.
        """
        interval = min(self._stuck_warn_interval_s * (2 ** self._stuck_warns),
                       self._stuck_warn_max_s)
        if self._last_stuck_warn_ts and now - self._last_stuck_warn_ts < interval:
            return
        self._last_stuck_warn_ts = now
        self._stuck_warns += 1
        logger.warning(
            "ADR093: %s STILL stuck after %.0fs (%.0f min) — screenshot cap "
            "%d reached, no further captures. Recovery has not worked.",
            episode, stuck_for, stuck_for / 60.0, self._max_per_episode)

    def tick(self, frame, current_game_state) -> "str | None":
        """Capture when GAME_UNKNOWN has persisted past the threshold.

        Returns the saved path (for tests/logging), else None. Never raises —
        a capture failure must not take down the tick loop.
        """
        if frame is None:
            return None
        now = self._clock()
        if current_game_state == GameState.GAME_UNKNOWN:
            if self._unknown_since == 0.0:
                # Startup enters GAME_UNKNOWN without a state-change callback —
                # arm the clock on first sight instead.
                self._unknown_since = now
                return None
            stuck_for = now - self._unknown_since
            episode = "GAME_UNKNOWN"
        elif self._blackout_active(now):
            # ADR 087: classified, but every defining crop reads empty.
            stuck_for = now - self._blackout_since
            episode = "%s blackout" % current_game_state.name
        else:
            return None
        if stuck_for < self._after_s:
            return None
        if self._captured >= self._max_per_episode:
            self._warn_still_stuck(now, episode, stuck_for)
            return None
        # Handling was attempted and may still be taking effect — a popup that
        # clears is not an anomaly, so hold the capture until the grace window
        # from the FIRST attempt expires.
        if self._first_dismiss_ts:
            grace_left = self._dismiss_grace_s - (now - self._first_dismiss_ts)
            if grace_left > 0:
                logger.debug(
                    "ADR074 anomaly: capture deferred %.0fs — dismissal of %s "
                    "attempted %dx, waiting to see if it clears",
                    grace_left, ", ".join(self._dismiss_popups) or "popup",
                    self._dismiss_attempts)
                return None
        if self._last_capture_ts and now - self._last_capture_ts < self._recapture_s:
            return None
        try:
            from datetime import datetime
            from pathlib import Path
            out_dir = Path(self._dir)
            out_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            slug = "unknown" if episode == "GAME_UNKNOWN" else "blackout"
            path = out_dir / f"{slug}_{stamp}_stuck{int(stuck_for)}s.png"
            if not self._cv2.imwrite(str(path), frame):
                logger.warning("ADR074 anomaly: screenshot write failed: %s", path)
                return None
        except Exception as e:
            logger.warning("ADR074 anomaly: screenshot capture failed: %s: %s",
                           type(e).__name__, e)
            return None
        self._captured += 1
        self._last_capture_ts = now
        if self._first_dismiss_ts:
            handling = ("dismissal of %s attempted %dx and did NOT clear it"
                        % (", ".join(self._dismiss_popups), self._dismiss_attempts))
        elif self._cleared_popups:
            handling = ("dismissal of %s cleared the popup (%dx) but GAME_UNKNOWN "
                        "persisted — cause is NOT popup handling"
                        % (", ".join(self._cleared_popups), self._dismiss_attempts))
        else:
            handling = "no calibrated popup crop matched — nothing to dismiss"
        logger.warning(
            "ADR074 anomaly: %s stuck for %.0fs (%s) — screenshot %d/%d "
            "saved to %s", episode, stuck_for, handling, self._captured,
            self._max_per_episode, path)
        return str(path)

class HealthDropoutRecorder:
    """ADR 080 d2: capture full frames during live-flight health OCR dropouts.

    An episode is one continuous confirmed-read gap past ``capture_after_s``
    while telemetry is live (stale telemetry means a death/menu gap — never
    captured). One frame per episode plus one recapture per
    ``recapture_interval_s`` for long episodes, capped per session. Mirrors
    the ADR 074 anomaly recorder contract: never raises, capture failure
    must not take down the tick loop.
    """

    def __init__(self, cfg: "dict | None", analyzer, clock=time.time):
        import cv2  # heavy import kept local: recorder is constructed once
        self._cv2 = cv2
        cfg = cfg or {}
        self._enabled = bool(cfg.get("enabled", True))
        self._after_s = float(cfg.get("capture_after_s", 5.0))
        self._recapture_s = float(cfg.get("recapture_interval_s", 60.0))
        self._max_per_session = int(cfg.get("max_per_session", 12))
        self._dir = str(cfg.get("dir", "test_screenshots/health_dropouts"))
        self._analyzer = analyzer
        self._clock = clock
        self._captured_total = 0
        self._episode_captured = False
        self._last_capture_ts = 0.0

    def tick(self, frame, current_game_state) -> "str | None":
        """Capture when a live-telemetry health gap has persisted past the
        threshold. Returns the saved path (for tests/logging), else None."""
        if not self._enabled or frame is None:
            return None
        if current_game_state != GameState.GAME_BATTLE:
            self._episode_captured = False
            return None
        gap = self._analyzer.health_confirmed_gap_s()
        if gap is None or gap < self._after_s:
            # A confirmed read (or no anchor yet) closed the episode.
            self._episode_captured = False
            return None
        if not self._analyzer.telemetry_hud_live():
            return None   # death/menu gap — not a dropout
        if self._captured_total >= self._max_per_session:
            return None
        now = self._clock()
        if self._episode_captured and now - self._last_capture_ts < self._recapture_s:
            return None
        try:
            from datetime import datetime
            from pathlib import Path
            out_dir = Path(self._dir)
            out_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            path = out_dir / f"dropout_{stamp}_gap{int(gap)}s.png"
            if not self._cv2.imwrite(str(path), frame):
                logger.warning("ADR080 dropout: screenshot write failed: %s", path)
                return None
        except Exception as e:
            logger.warning("ADR080 dropout: screenshot capture failed: %s: %s",
                           type(e).__name__, e)
            return None
        self._captured_total += 1
        self._episode_captured = True
        self._last_capture_ts = now
        logger.info(
            "ADR080 dropout: health unconfirmed %.0fs with live telemetry — "
            "frame %d/%d saved to %s",
            gap, self._captured_total, self._max_per_session, path)
        return str(path)
