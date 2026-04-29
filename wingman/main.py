import argparse
import yaml
import time
import logging
import threading
from enum import Enum, auto

try:
    import colorama
    colorama.init()
except ImportError:
    colorama = None

WINGMAN_VERSION = "1.6.5"
WINGMAN_VERSION_DETAILS = "FSM stability improvements, mission restart retry logic, and enhanced logging"

from .capture import Capture
from .controller import Controller, REGION_CLICK_TO_CONTINUE, REGION_PLAY_BUTTON
from .analyzer import GameStateAnalyzer, GameState


class RespawnState(Enum):
    IDLE = auto()            # Normal gameplay
    RESPAWNING = auto()      # Respawn screen active; mission being cancelled
    PENDING_RESTART = auto() # Respawn gone; waiting for delay before restarting


def load_config(path):
    with open(path, "r") as f:
        return yaml.safe_load(f)


def _click_through_game_end(ctrl, analyzer, logger, settle_seconds: float = 0.8, sleep_fn=time.sleep):
    """Click through GAME_END prompt and force transition to GAME_LOBBY.

    Clicks the center prompt repeatedly, then clicks the lower-right continue
    button. After the final click, explicitly flips state flags so the analyzer
    exits GAME_END_B even if OCR polling is currently skipping in that state.
    """
    ctrl.click_crop(
        analyzer.crops["click_to"],
        block=True,
        count=7,
        region_name=REGION_CLICK_TO_CONTINUE,
    )
    sleep_fn(settle_seconds)
    ctrl.click_crop(
        analyzer.crops["PLAY"],
        block=True,
        count=1,
        region_name=REGION_PLAY_BUTTON,
    )
    analyzer._trigger("continue_clicked")
    logger.info("\033[93m📋 Final continue click complete → GAME_LOBBY\033[0m")



def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="wingman/config.yaml")
    parser.add_argument("--log-level", default="INFO", help="Console log level (DEBUG, INFO, WARNING, ERROR)")
    parser.add_argument("--log-file", default=None, metavar="PATH",
                        help="Write DEBUG-level logs to this file (console keeps --log-level)")
    args = parser.parse_args()

    console_level = getattr(logging, args.log_level.upper(), logging.INFO)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")

    console_handler = logging.StreamHandler()
    console_handler.stream.reconfigure(encoding="utf-8", errors="replace")
    console_handler.setFormatter(fmt)
    console_handler.setLevel(console_level)

    handlers = [console_handler]

    if args.log_file:
        file_handler = logging.FileHandler(args.log_file, encoding="utf-8")
        file_handler.setFormatter(fmt)
        file_handler.setLevel(logging.DEBUG)
        handlers.append(file_handler)

    root_level = logging.DEBUG if args.log_file else console_level
    logging.basicConfig(level=root_level, handlers=handlers)
    logger = logging.getLogger("wingman")

    cfg = load_config(args.config)
    logger.info("Configuration loaded from %s", args.config)
    

    region = (
        cfg["region"]["left"],
        cfg["region"]["top"],
        cfg["region"]["width"],
        cfg["region"]["height"],
    )
    monitor_index = cfg.get("monitor", 1)

    exit_requested = threading.Event()

    # Initialize main components
    cap = Capture(region, monitor_index)
    analyzer = GameStateAnalyzer(cfg)  # also usable as a context manager via __enter__/__exit__

    unattended_mode = cfg.get("unattended_mode", False)
    unattended_active = threading.Event()
    if unattended_mode:
        unattended_active.set()
        logger.info("Unattended mode enabled from config")

    # Load mission restart timing from config
    mission_cfg = cfg.get("mission", {})
    weapon_loop_interval = mission_cfg.get("weapon_loop_interval", 0.5)
    restart_retry_interval = mission_cfg.get("restart_retry_interval", 2.0)
    restart_delay_after_unlock = mission_cfg.get("restart_delay_after_unlock", 4.0)
    respawn_fallback_timeout = mission_cfg.get("respawn_fallback_timeout", 20.0)

    def _on_auto_mission_key():
        """Called when AUTO_MISSION_KEY is pressed. Activates unattended mode for the session."""
        if unattended_mode and not unattended_active.is_set():
            unattended_active.set()
            logger.info("Unattended mode activated by M key press")

    # Initialize controller with config-driven weapon loop interval and exit event
    ctrl = Controller(region, analyzer=analyzer, weapon_loop_interval=weapon_loop_interval, exit_event=exit_requested, capture=cap, on_auto_mission_key=_on_auto_mission_key, crops=analyzer.crops)

    # Wire FSM entry-hook callbacks (ADR 025) — injected after both objects exist
    analyzer._on_cancel_mission = ctrl.cancel_mission
    analyzer._on_start_game_starting_loop = ctrl._start_game_starting_loop

    # Load loop interval from config
    loop_interval_sec = cfg.get("loop_interval_sec", 0.5)

    # Mission restart state machine
    respawn_state = RespawnState.IDLE
    respawn_cooldown_until = 0.0  # suppress re-detection for 10s after first trigger
    last_restart_attempt = 0.0
    restart_not_before = 0.0
    last_incoming_alert_ts = 0.0
    missile_ignore_until = 0.0       # suppress missile alerts for 10s after respawn
    last_click_to_alert_ts = 0.0
    last_game_state = None
    last_flare_reload_ts = 0.0    # cooldown: don't spam SPECIAL_ABILITY if flares stay at 2
    enemy_last_seen_ts = 0.0      # timestamp of last frame with red in ENEMY_CLOSE_BY (0 = not in battle yet)
    lobby_play_scan_interval = 5.0
    last_lobby_play_scan_attempt = 0.0
    game_end_b_since = 0.0    # timestamp of GAME_END_B entry; used by stall timeout guard
    game_waiting_since = 0.0      # timestamp of GAME_WAITING entry; used by CANCEL scan + 180s timeout
    last_cancel_scan_ts = 0.0     # last time CANCEL crop was scanned in GAME_WAITING
    last_play_reclick_ts = 0.0    # last time PLAY was re-clicked in GAME_WAITING
    last_waiting_popup_scan_ts = 0.0  # last time lobby popups were scanned in GAME_WAITING
    play_reclick_interval = 45.0  # minimum seconds between PLAY re-clicks

    def _handle_alive_transition():
        """Restart mission immediately when health transitions dead → alive."""
        nonlocal last_restart_attempt, respawn_state, enemy_last_seen_ts
        analyzer.alive_event.clear()
        enemy_last_seen_ts = time.time()  # reset so 30s clock starts fresh after respawn
        if (analyzer.game_state == GameState.GAME_BATTLE
                and not ctrl.is_mission_running()
                and ctrl._auto_respawn_restart):
            logger.info("\033[92m💚 HEALTH ALIVE — restarting mission immediately\033[0m")
            ctrl.restart_last_mission()
            last_restart_attempt = time.time()
            respawn_state = RespawnState.IDLE

    def _handle_low_flares():
        """Press SPECIAL_ABILITY to reload flares when count reaches 2."""
        nonlocal last_flare_reload_ts
        analyzer.low_flares_event.clear()
        if analyzer.game_state != GameState.GAME_BATTLE:
            return
        if time.time() - last_flare_reload_ts < 30.0:
            logger.debug("Low-flares event: reload suppressed by cooldown (%.1fs remaining)",
                         30.0 - (time.time() - last_flare_reload_ts))
            return
        ctrl.reload_flares()
        last_flare_reload_ts = time.time()

    def _handle_no_missiles():
        """End mission and eject when missile count reaches zero."""
        analyzer.no_missiles_event.clear()
        if not ctrl.is_mission_running():
            return
        with analyzer._ocr_cache_lock:
            currently_respawning, _, _ = analyzer._ocr_cache['result']
        if currently_respawning:
            logger.debug("No-missiles suppressed — respawn screen active")
            return
        if time.time() < missile_ignore_until:
            logger.debug("No-missiles suppressed — post-respawn grace (%.1fs remaining)",
                         missile_ignore_until - time.time())
            return
        ctrl.eject_and_dive()

    def _deploy_flares_on_new_incoming() -> bool:
        """Deploy flares in a burst when a new incoming OCR detection arrives."""
        nonlocal last_incoming_alert_ts
        with analyzer._incoming_cache_lock:
            incoming_detected, _, _ = analyzer._incoming_cache['result']
            incoming_ts = analyzer._incoming_cache['timestamp']

        if incoming_detected and incoming_ts > last_incoming_alert_ts:
            if time.time() < missile_ignore_until:
                logger.debug("Missile alert suppressed — post-respawn grace period (%.1fs remaining)",
                             missile_ignore_until - time.time())
                last_incoming_alert_ts = incoming_ts
                return False
            logger.info("\033[95m🚀 INCOMING MISSILE DETECTED - Deploying flares\033[0m")
            last_incoming_alert_ts = incoming_ts

            def _flare_burst():
                for _ in range(3):
                    ctrl.deploy_flares(hold_seconds=0.05, block=True, ignore_cancel=True)
                    time.sleep(0.3)
                logger.info("\033[95m🚀 Flare burst complete\033[0m")

            threading.Thread(target=_flare_burst, daemon=True).start()
            return True

        return False

    try:
        while True:
            loop_start = time.time()
            if exit_requested.is_set():
                logger.info("Exit requested, shutting down")
                break
            # Capture and analyze frame
            frame = cap.get_frame()
            if frame is None:
                logger.warning("Frame capture failed (monitor disconnected or region out of bounds) — skipping cycle")
                continue
            game_state = analyzer.analyze_frame(frame)

            # Log game state transitions
            current_game_state = game_state.get('game_state')
            if current_game_state != last_game_state:
                logger.info("\033[96m🎮 Game state: %s → %s\033[0m",
                            last_game_state.name if last_game_state else "UNKNOWN",
                            current_game_state.name if current_game_state else "UNKNOWN")
                prev_game_state = last_game_state
                last_game_state = current_game_state
                if current_game_state == GameState.GAME_END_B:
                    game_end_b_since = time.time()
                    # GAME_END_B is not a respawn flow; clear any stale pending-restart
                    # state so we do not relaunch a mission during click-through.
                    respawn_state = RespawnState.IDLE
                    restart_not_before = 0.0
                    last_restart_attempt = 0.0
                else:
                    game_end_b_since = 0.0
                if current_game_state == GameState.GAME_LOBBY:
                    game_waiting_since = 0.0
                    if prev_game_state is not None:
                        ctrl.cancel_mission()
                    if unattended_active.is_set():
                        logger.info("Unattended mode: auto-triggering mission from GAME_LOBBY")
                        last_lobby_play_scan_attempt = time.time()
                        ctrl.start_auto_mission()
                if current_game_state == GameState.GAME_WAITING:
                    game_waiting_since = time.time()
                    last_cancel_scan_ts = time.time()         # first scan after 3s, not immediately
                    last_play_reclick_ts = time.time()        # don't re-click immediately either
                    last_waiting_popup_scan_ts = 0.0          # allow popup scan immediately on entry
                else:
                    game_waiting_since = 0.0
                if current_game_state == GameState.GAME_BATTLE:
                    enemy_last_seen_ts = time.time()  # assume enemy present on battle entry

            # In unattended mode, keep retrying lobby PLAY detection/click every 5s
            # until GAME_LOBBY transitions out.
            if (unattended_active.is_set()
                    and current_game_state == GameState.GAME_LOBBY
                    and time.time() - last_lobby_play_scan_attempt >= lobby_play_scan_interval):
                logger.info("Unattended mode: GAME_LOBBY retry - scanning play_button for PLAY")
                last_lobby_play_scan_attempt = time.time()
                ctrl.start_auto_mission()


            # GAME_WAITING: scan for CANCEL every 3s to confirm matchmaking.
            # CANCEL visible → matchmaking active → advance to GAME_STARTING.
            # CANCEL absent  → PLAY click missed → re-click PLAY.
            # 180s timeout   → give up, return to GAME_LOBBY.
            if current_game_state == GameState.GAME_WAITING and game_waiting_since > 0:
                elapsed_waiting = time.time() - game_waiting_since
                if elapsed_waiting > 180.0:
                    logger.warning(
                        "GAME_WAITING timeout after %.0fs — CANCEL never detected; returning to GAME_LOBBY",
                        elapsed_waiting)
                    analyzer._trigger("waiting_timeout")
                    game_waiting_since = 0.0
                elif time.time() - last_cancel_scan_ts >= 3.0:
                    last_cancel_scan_ts = time.time()
                    cancel_detected = analyzer.scan_region_for_cancel(frame)
                    if cancel_detected:
                        logger.info(
                            "\033[92m✓ CANCEL detected (%.1fs) — matchmaking confirmed → GAME_STARTING\033[0m",
                            elapsed_waiting)
                        analyzer._trigger("cancel_detected")  # on_enter_GAME_STARTING fires _start_game_starting_loop
                    else:
                        # Scan for lobby popups every 5s while CANCEL is absent
                        if time.time() - last_waiting_popup_scan_ts >= 5.0:
                            last_waiting_popup_scan_ts = time.time()
                            popup = analyzer.scan_region_for_lobby_popups(frame)
                            if popup:
                                if not ctrl.popup_click_allowed(popup):
                                    logger.debug("GAME_WAITING: popup '%s' click suppressed by cooldown", popup)
                                else:
                                    logger.info(
                                        "\033[93m📋 GAME_WAITING: dismissing lobby popup '%s'\033[0m", popup)
                                    ctrl.record_popup_click(popup)
                                    click_target = "event_refresh_dismiss" if popup == "event_refresh" else popup
                                    ctrl.click_crop(analyzer.crops[click_target], block=False, count=1, region_name=click_target)
                                    last_play_reclick_ts = time.time()  # don't re-click PLAY right after a popup
                                    if popup == "REVEAL_ALL":
                                        def _reveal_all_second_click():
                                            time.sleep(3.0)
                                            logger.info("\033[93m📋 REVEAL_ALL second click after 3s delay\033[0m")
                                            ctrl.click_crop(analyzer.crops["REVEAL_ALL"], block=False, count=1, region_name="REVEAL_ALL")
                                        threading.Thread(target=_reveal_all_second_click, daemon=True).start()
                                    elif popup == "INVITED":
                                        def _click_ready_after_invite():
                                            time.sleep(1.5)
                                            new_frame = cap.get_frame()
                                            if new_frame is None:
                                                return
                                            ready = analyzer.scan_region_for_play_button(new_frame)
                                            if ready:
                                                logger.info("\033[92m📋 INVITED accepted — clicking %s\033[0m", ready)
                                                ctrl.click_crop(analyzer.crops[ready], block=False, count=1, region_name=ready)
                                        threading.Thread(target=_click_ready_after_invite, daemon=True).start()
                        crop = next((c for c in ("PLAY", "READY") if c in analyzer.crops), None)
                        if crop and time.time() - last_play_reclick_ts >= play_reclick_interval:
                            # Only re-click if PLAY/READY is actually visible — clicking PLAY while
                            # matchmaking is in progress cancels it. If PLAY isn't visible the game
                            # is still processing the previous click; leave it alone.
                            visible_crop = analyzer.scan_region_for_play_button(frame)
                            if visible_crop:
                                logger.info(
                                    "GAME_WAITING: CANCEL not found (%.1fs) and %s visible — re-clicking",
                                    elapsed_waiting, visible_crop)
                                last_play_reclick_ts = time.time()
                                ctrl.click_crop(analyzer.crops[visible_crop], block=False, count=1, region_name=visible_crop)
                            else:
                                logger.debug(
                                    "GAME_WAITING: CANCEL not found (%.1fs) but PLAY not visible — matchmaking in progress, waiting",
                                    elapsed_waiting)
                                last_play_reclick_ts = time.time()  # reset timer to avoid spamming OCR
                        elif crop:
                            logger.debug(
                                "GAME_WAITING: CANCEL not found (%.1fs) — waiting %.1fs before re-click",
                                elapsed_waiting, play_reclick_interval - (time.time() - last_play_reclick_ts))

            # GAME_END_B stall guard: if click-to OCR cache gets stuck, force recovery
            if (current_game_state == GameState.GAME_END_B
                    and game_end_b_since > 0
                    and time.time() - game_end_b_since > 30.0):
                logger.warning("GAME_END_B timeout — click-to OCR may be stuck; forcing recovery to GAME_LOBBY")
                analyzer._trigger("manual_reset")
                game_end_b_since = 0.0

            # Deploy flares immediately when a new incoming OCR result arrives.
            # Higher priority than respawn — must run before the respawn continue.
            _deploy_flares_on_new_incoming()

            # Restart mission immediately when health transitions dead → alive.
            if analyzer.alive_event.is_set():
                _handle_alive_transition()

            # Ammo events (GAME_BATTLE only).
            if analyzer.low_flares_event.is_set():
                _handle_low_flares()
            if analyzer.no_missiles_event.is_set():
                _handle_no_missiles()

            # Enemy presence check: if ENEMY_CLOSE_BY has had no red for 30s, disengage.
            if current_game_state == GameState.GAME_BATTLE and enemy_last_seen_ts > 0:
                if analyzer.detect_enemy_red(frame):
                    enemy_last_seen_ts = time.time()
                elif time.time() - enemy_last_seen_ts >= 30.0 and ctrl.is_mission_running():
                    logger.info("\033[93m↩ No enemy in ENEMY_CLOSE_BY for 30s — disengaging\033[0m")
                    enemy_last_seen_ts = time.time()  # reset to avoid re-triggering
                    ctrl.disengage_roll_right()

            # Detect respawn
            if game_state.get('is_respawning'):
                if respawn_state in (RespawnState.IDLE, RespawnState.PENDING_RESTART):
                    if time.time() < respawn_cooldown_until:
                        logger.debug("RESPAWN seen but suppressed by cooldown (%.1fs remaining)",
                                     respawn_cooldown_until - time.time())
                    else:
                        logger.info("\033[91m⚠ RESPAWN DETECTED - Cancelling active missions\033[0m")
                        respawn_cooldown_until = time.time() + 10.0
                        missile_ignore_until = time.time() + 10.0
                        enemy_last_seen_ts = time.time()  # reset so 30s clock starts fresh after respawn
                        ctrl._auto_respawn_restart = True  # always restart after respawn
                        # Exit manual mode on death — mission restarts when health returns
                        if current_game_state == GameState.GAME_BATTLE_MANUAL:
                            analyzer._trigger("respawn_reset")
                        ctrl._eject_stop.set()            # interrupt any in-progress eject_and_dive immediately
                        ctrl.cancel_mission()
                        # Wait for mission lock to release before restart
                        logger.info("Waiting for mission lock to release before restart...")
                        for _ in range(50):
                            if not ctrl.is_mission_running():
                                break
                            time.sleep(0.1)
                        else:
                            logger.warning("Timeout waiting for mission lock release; will keep retrying restart.")
                        respawn_state = RespawnState.RESPAWNING
                        restart_not_before = time.time() + respawn_fallback_timeout
                        logger.info("Respawn screen active — will restart %.1fs after screen clears (stuck OCR fallback in %.1fs)",
                                    restart_delay_after_unlock, respawn_fallback_timeout)

                logger.info("\033[91mRESPAWN ACTIVE (%.0f%% confidence)\033[0m", game_state.get('respawn_confidence', 0) * 100)

                # Attempt restart while respawn screen is showing (fallback if OCR never clears).
                # Skips if mission still running or delay not yet elapsed.
                if (not ctrl.is_mission_running()
                        and time.time() >= restart_not_before
                        and time.time() - last_restart_attempt > restart_retry_interval):
                    logger.info("Attempting to restart mission after respawn...")
                    if ctrl.restart_last_mission():
                        logger.info("Restarted last mission after respawn")
                        respawn_state = RespawnState.IDLE
                    last_restart_attempt = time.time()

                time.sleep(1)
                continue

            # Gameplay resumed after respawn — reset delay timer from this point
            if respawn_state == RespawnState.RESPAWNING:
                restart_not_before = time.time() + restart_delay_after_unlock
                logger.info("\033[92m✓ Gameplay resumed - scheduling restart in %.1fs\033[0m", restart_delay_after_unlock)
                respawn_state = RespawnState.PENDING_RESTART

            # Retry mission restart if pending and delay has passed (persists across gameplay resume)
            if (respawn_state == RespawnState.PENDING_RESTART
                    and current_game_state == GameState.GAME_BATTLE
                    and time.time() >= restart_not_before
                    and time.time() - last_restart_attempt > restart_retry_interval):
                if not ctrl.is_mission_running():
                    logger.info("Attempting to restart mission (delay expired)...")
                    result = ctrl.restart_last_mission()
                    if result is True:
                        logger.info("Restarted last mission after respawn")
                        respawn_state = RespawnState.IDLE
                    elif result is None:
                        logger.info("No previous mission to restart; clearing pending restart")
                        respawn_state = RespawnState.IDLE
                    else:
                        logger.info("Mission restart attempt failed; will retry in %.1fs", restart_retry_interval)
                    last_restart_attempt = time.time()

            # Log "Click to Continue" prompt when newly detected (informational only).
            with analyzer._click_to_cache_lock:
                click_to_detected, _, _ = analyzer._click_to_cache['result']
                click_to_ts = analyzer._click_to_cache['timestamp']
            if click_to_detected and click_to_ts > last_click_to_alert_ts:
                logger.info("\033[93m📋 CLICK TO CONTINUE detected in CLICK_TO_CONTINUE region\033[0m")
                last_click_to_alert_ts = click_to_ts
                ctrl.cancel_mission()
                threading.Thread(
                    target=_click_through_game_end,
                    args=(ctrl, analyzer, logger),
                    daemon=True,
                ).start()

            # Enforce configurable loop interval.
            # Block on incoming_event so flare deployment wakes immediately on new OCR results
            # rather than spinning at 20 Hz.  The event is set by the background OCR thread
            # whenever a new incoming result is written; we clear it after acting on it.
            elapsed = time.time() - loop_start
            if elapsed < loop_interval_sec:
                sleep_end = loop_start + loop_interval_sec
                while True:
                    now = time.time()
                    remaining = sleep_end - now
                    if remaining <= 0:
                        break
                    analyzer.incoming_event.wait(timeout=remaining)
                    analyzer.incoming_event.clear()
                    _deploy_flares_on_new_incoming()
                    if analyzer.alive_event.is_set():
                        _handle_alive_transition()
                    if analyzer.low_flares_event.is_set():
                        _handle_low_flares()
                    if analyzer.no_missiles_event.is_set():
                        _handle_no_missiles()
    except KeyboardInterrupt:
        logger.info("Exiting")
    except Exception:
        logger.exception("Unhandled exception in main loop")
    finally:
        analyzer.cleanup()


if __name__ == "__main__":
    main()
