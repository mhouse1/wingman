import argparse
import yaml
import time
import logging
import threading
from enum import Enum, auto

WINGMAN_VERSION = "1.5.2"
WINGMAN_VERSION_DETAILS = "Sub region optimization for OCR"

from .capture import Capture
from .controller import Controller, CANCEL_MISSION_KEY, MISSION_J20_KEY
from .analyzer import GameStateAnalyzer, GameState


class RespawnState(Enum):
    IDLE = auto()            # Normal gameplay
    RESPAWNING = auto()      # Respawn screen active; mission being cancelled
    PENDING_RESTART = auto() # Respawn gone; waiting for delay before restarting


def load_config(path):
    with open(path, "r") as f:
        return yaml.safe_load(f)



def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="wingman/config.yaml")
    parser.add_argument("--log-level", default="INFO", help="Logging level (DEBUG, INFO, WARNING, ERROR)")
    args = parser.parse_args()

    logging.basicConfig(level=getattr(logging, args.log_level.upper(), logging.INFO), format="%(asctime)s [%(levelname)s] %(message)s")
    logger = logging.getLogger("wingman")

    cfg = load_config(args.config)
    logger.info("Configuration loaded from %s", args.config)
    

    region = (
        cfg["region"]["left"],
        cfg["region"]["top"],
        cfg["region"]["width"],
        cfg["region"]["height"],
    )
    monitor_index = cfg["region"].get("monitor", 1)

    exit_requested = threading.Event()

    # Initialize main components
    cap = Capture(region, monitor_index)
    analyzer = GameStateAnalyzer(cfg)

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

    def _on_auto_mission_key():
        """Called when AUTO_MISSION_KEY is pressed. Activates unattended mode for the session."""
        if unattended_mode and not unattended_active.is_set():
            unattended_active.set()
            logger.info("Unattended mode activated by M key press")

    controls_cfg = cfg.get("controls", {})
    ready_button_region = controls_cfg.get("ready_button_region", 64)

    # Initialize controller with config-driven weapon loop interval and exit event
    ctrl = Controller(region, analyzer=analyzer, weapon_loop_interval=weapon_loop_interval, exit_event=exit_requested, capture=cap, on_auto_mission_key=_on_auto_mission_key, ready_button_region=ready_button_region)

    # Load loop interval from config
    loop_interval_sec = cfg.get("loop_interval_sec", 0.5)

    # Mission restart state machine
    respawn_state = RespawnState.IDLE
    respawn_cooldown_until = 0.0  # suppress re-detection for 10s after first trigger
    last_restart_attempt = 0.0
    restart_not_before = 0.0
    last_incoming_alert_ts = 0.0
    last_click_to_alert_ts = 0.0
    last_game_state = None

    def _deploy_flares_on_new_incoming() -> bool:
        """Deploy flares in a burst when a new incoming OCR detection arrives."""
        nonlocal last_incoming_alert_ts
        with analyzer._incoming_cache_lock:
            incoming_detected, _, _ = analyzer._incoming_cache['result']
            incoming_ts = analyzer._incoming_cache['timestamp']

        if incoming_detected and incoming_ts > last_incoming_alert_ts:
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
            game_state = analyzer.analyze_frame(frame)

            # Log game state transitions
            current_game_state = game_state.get('game_state')
            if current_game_state != last_game_state:
                logger.info("\033[96m🎮 Game state: %s → %s\033[0m",
                            last_game_state.name if last_game_state else "UNKNOWN",
                            current_game_state.name if current_game_state else "UNKNOWN")
                last_game_state = current_game_state
                if current_game_state == GameState.GAME_LOBBY and unattended_active.is_set():
                    logger.info("Unattended mode: auto-triggering mission from GAME_LOBBY")
                    ctrl.start_auto_mission()

            # Deploy flares immediately when a new incoming OCR result arrives.
            # Higher priority than respawn — must run before the respawn continue.
            _deploy_flares_on_new_incoming()

            # Detect respawn
            if game_state.get('is_respawning'):
                if respawn_state in (RespawnState.IDLE, RespawnState.PENDING_RESTART):
                    if time.time() < respawn_cooldown_until:
                        logger.debug("RESPAWN seen but suppressed by cooldown (%.1fs remaining)",
                                     respawn_cooldown_until - time.time())
                    else:
                        logger.info("\033[91m⚠ RESPAWN DETECTED - Cancelling active missions\033[0m")
                        respawn_cooldown_until = time.time() + 10.0
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
                        restart_not_before = time.time() + restart_delay_after_unlock
                        logger.info("Respawn screen active — will restart %.1fs after screen clears (or after %.1fs if stuck)",
                                    restart_delay_after_unlock, restart_delay_after_unlock)

                logger.info("\033[91mRESPAWN ACTIVE (%.0f%% confidence)\033[0m", game_state.get('respawn_confidence', 0) * 100)

                # Attempt restart while respawn screen is showing (fallback if OCR never clears).
                # Skips if mission still running, delay not yet elapsed, or auto-restart disabled by manual End press.
                if not ctrl._auto_respawn_restart:
                    logger.debug("Auto-respawn restart disabled ('%s' was pressed) — press '%s' to re-enable",
                                 CANCEL_MISSION_KEY, MISSION_J20_KEY)
                elif (not ctrl.is_mission_running()
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
            if (ctrl._auto_respawn_restart
                    and respawn_state == RespawnState.PENDING_RESTART
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
                logger.info("\033[93m📋 CLICK TO CONTINUE detected in region %d\033[0m", analyzer.click_to_region)
                last_click_to_alert_ts = click_to_ts
                ctrl.cancel_mission()
                ctrl.click_grid_region(analyzer.click_to_region, analyzer.grid_rows, analyzer.grid_cols, block=False)

            # Enforce configurable loop interval
            elapsed = time.time() - loop_start
            if elapsed < loop_interval_sec:
                sleep_end = loop_start + loop_interval_sec
                while True:
                    now = time.time()
                    if now >= sleep_end:
                        break
                    # Poll incoming cache during sleep so flare deploy is not delayed until next loop.
                    _deploy_flares_on_new_incoming()
                    time.sleep(min(0.05, sleep_end - now))
    except KeyboardInterrupt:
        logger.info("Exiting")
    except Exception:
        logger.exception("Unhandled exception in main loop")
    finally:
        analyzer.cleanup()


if __name__ == "__main__":
    main()
