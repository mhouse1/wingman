import argparse
import json
import sys
import yaml
import time
import logging
import subprocess
import threading
from datetime import datetime
from enum import Enum, auto
from pathlib import Path

try:
    import colorama
    colorama.init()
except ImportError:
    colorama = None

WINGMAN_VERSION = "1.6.29"
WINGMAN_VERSION_DETAILS = "fix disengage roll self-cancel and restart teardown race - no more uncommanded flight"

from .capture import Capture
from .controller import Controller, REGION_CLICK_TO_CONTINUE, REGION_PLAY_BUTTON, MISSION_J20_KEY
from .analyzer import GameStateAnalyzer, GameState
from .hud import HudRenderer
from .mission_stats import MissionStatsTracker
from .performance import PerformanceTracker
from .tracker import TargetTracker
from .replay import (
    LivePathCaptureEngine,
    ReplayAssertionEngine,
    ScreenshotReplayCapture,
    build_required_screenshot_dictionary,
    find_missing_screenshots,
    load_replay_paths,
    select_replay_path,
    write_required_screenshot_report,
)


class RespawnState(Enum):
    IDLE = auto()            # Normal gameplay
    RESPAWNING = auto()      # Respawn screen active; restart fires on health return


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
    play_crop = analyzer.crops.get("PLAY")
    if play_crop is None:
        logger.warning("_click_through_game_end: PLAY crop not configured — skipping final click")
        return
    ctrl.click_crop(
        play_crop,
        block=True,
        count=1,
        region_name=REGION_PLAY_BUTTON,
    )
    analyzer.trigger_event("continue_clicked")
    logger.info("\033[93m📋 Final continue click complete → GAME_LOBBY\033[0m")


def _update_waiting_fallback(
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
    logger,
):
    """Update GAME_WAITING fallback confidence and report if promotion should fire."""
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



def _alive_transition_disposition(state, alive_after_observed_death: bool) -> str:
    """Classify an alive (dead→alive health) transition by FSM state (ADR 061).

    Returns one of:
      restart_path     — GAME_BATTLE: run the ADR 059 restart flow.
      terminate_eject  — GAME_BATTLE_EJECT after an observed death: the respawn
                         happened but overlay OCR missed it; stop the eject and
                         keep the event armed.
      consume_spurious — GAME_BATTLE_EJECT without an observed death: the
                         synthetic eject-start transition; consume it.
      consume_other    — any other state (manual, lobby, ...): consume it.
    """
    if state == GameState.GAME_BATTLE:
        return "restart_path"
    if state == GameState.GAME_BATTLE_EJECT:
        return "terminate_eject" if alive_after_observed_death else "consume_spurious"
    return "consume_other"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="wingman/config.yaml")
    parser.add_argument("--log-level", default="INFO", help="Console log level (DEBUG, INFO, WARNING, ERROR)")
    parser.add_argument("--log-file", default=None, metavar="PATH",
                        help="Write DEBUG-level logs to this file (console keeps --log-level)")
    parser.add_argument("--replay-config", default=None,
                        help="Path to replay config mapping PATH names to [SCREENSHOTNAME, TIME_TO_INJECT] steps")
    parser.add_argument("--replay-path", default=None,
                        help="Replay path name to run (defaults to first path in --replay-config)")
    parser.add_argument("--replay-screenshot-dir", default="test_screenshots/integration_test",
                        help="Directory containing replay screenshots")
    parser.add_argument("--replay-exit-after", type=float, default=3.0,
                        help="Seconds to run after the last replay injection before exiting")
    parser.add_argument("--replay-report", default="tests/test-output/replay_required_screenshots.json",
                        help="Where to write required/missing replay screenshot report")
    parser.add_argument("--replay-intents-output", default="tests/test-output/replay_action_intents.json",
                        help="Where to write recorded replay action intents")
    parser.add_argument("--replay-assertions-output", default="tests/test-output/replay_assertions.json",
                        help="Where to write replay assertion results and timing gates")
    parser.add_argument("--capture-path-config", default=None,
                        help="Path to capture config mapping PATH names to replay steps")
    parser.add_argument("--capture-path", default=None,
                        help="Capture path name to run")
    parser.add_argument("--capture-screenshot-dir", default="test_screenshots/integration_test",
                        help="Directory to write captured screenshots")
    parser.add_argument("--capture-overwrite", action="store_true",
                        help="Overwrite existing capture screenshots")
    parser.add_argument("--capture-timeout-s", type=float, default=20.0,
                        help="Seconds to wait per capture step before timing out")
    parser.add_argument("--capture-summary", default="tests/test-output/capture_summary.json",
                        help="Where to write live capture summary")
    parser.add_argument("--capture-allow-inject", action="store_true",
                        help="Allow synthetic inject_trigger use during live capture")
    parser.add_argument("--capture-start-at-step", default=None,
                        help="Optional screenshot_name to resume capture from")
    args = parser.parse_args()

    console_level = getattr(logging, args.log_level.upper(), logging.INFO)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")

    console_handler = logging.StreamHandler()
    console_handler.stream.reconfigure(encoding="utf-8", errors="replace")
    console_handler.setFormatter(fmt)
    console_handler.setLevel(console_level)

    handlers = [console_handler]

    if args.log_file:
        # Rotate, never truncate: mode="w" on a fixed filename destroyed two
        # sessions' forensic records (the 2026-07-30 18:51 log — 21541 lines —
        # was overwritten before its review could re-verify anything). Rename
        # the previous log into logs/<stem>_<last-write-time><suffix> before
        # opening; rename is atomic and a process still holding the old fd is
        # unaffected. mode="w" is kept for the NEW file so the ADR044/045
        # replay validators still see only the current run's lines.
        log_path = Path(args.log_file)
        try:
            if log_path.exists() and log_path.stat().st_size > 0:
                stamp = datetime.fromtimestamp(log_path.stat().st_mtime).strftime("%Y%m%d_%H%M%S")
                backup_dir = log_path.parent / "logs"
                backup_dir.mkdir(exist_ok=True)
                log_path.rename(backup_dir / f"{log_path.stem}_{stamp}{log_path.suffix}")
        except OSError as e:
            print(f"WARNING: could not rotate previous log {log_path}: {e}", file=sys.stderr)
        file_handler = logging.FileHandler(args.log_file, mode="w", encoding="utf-8")
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

    _gwo = cfg.get("game_window_offset", {}) or {}
    _gwo_x = _gwo.get("x")
    _gwo_y = _gwo.get("y")
    game_window_offset = (int(_gwo_x), int(_gwo_y)) if (_gwo_x is not None and _gwo_y is not None) else None
    if game_window_offset:
        logger.info("game_window_offset from config: (%d, %d)", *game_window_offset)

    exit_requested = threading.Event()
    # SIGTERM must take the same graceful path as Backspace: daemon threads die
    # without their finally blocks, and XTest key state lives in the X SERVER —
    # a hard kill mid-eject leaves NOSE_DOWN/AFTERBURNER pressed for the whole
    # X session. Route it through exit_requested so cleanup() releases keys.
    try:
        import signal
        signal.signal(signal.SIGTERM, lambda _sig, _frm: exit_requested.set())
    except (ValueError, OSError):  # non-main thread or unsupported platform
        pass
    replay_mode = bool(args.replay_config)
    capture_mode = bool(args.capture_path_config)
    replay_capture = None
    replay_assertions = None
    live_capture = None

    # Initialize main components
    if replay_mode and capture_mode:
        raise ValueError("Use either --replay-config or --capture-path-config, not both")

    if replay_mode:
        replay_config_path = Path(args.replay_config)
        replay_screenshot_dir = Path(args.replay_screenshot_dir)
        replay_report_path = Path(args.replay_report)

        replay_path_map = load_replay_paths(replay_config_path)
        required = build_required_screenshot_dictionary(replay_path_map)
        missing = find_missing_screenshots(required, replay_screenshot_dir)
        write_required_screenshot_report(replay_report_path, replay_screenshot_dir, required, missing)

        replay_path = select_replay_path(replay_config_path, args.replay_path)
        replay_assertions = ReplayAssertionEngine(replay_path.path_name, replay_path.steps)
        logger.info("Replay mode enabled: path=%s, screenshots=%s", replay_path.path_name, replay_screenshot_dir)
        if any(missing_names for missing_names in missing.values()):
            logger.warning("Replay screenshot report indicates missing files; see %s", replay_report_path)

        replay_capture = ScreenshotReplayCapture(
            region=region,
            screenshot_dir=replay_screenshot_dir,
            steps=replay_path.steps,
        )
        cap = replay_capture
    elif capture_mode:
        capture_config_path = Path(args.capture_path_config)
        capture_screenshot_dir = Path(args.capture_screenshot_dir)
        live_path = select_replay_path(capture_config_path, args.capture_path)
        live_capture = LivePathCaptureEngine(
            path_name=live_path.path_name,
            steps=live_path.steps,
            screenshot_dir=capture_screenshot_dir,
            region=region,
            overwrite=args.capture_overwrite,
            timeout_s=args.capture_timeout_s,
            allow_inject=args.capture_allow_inject,
            start_at_step=args.capture_start_at_step,
            auto_resume=False,
            timeout_advances=False,
            out_of_order=True,
        )
        cap = Capture(region, monitor_index, game_window_offset=game_window_offset)
        logger.info(
            "Capture mode enabled: path=%s, screenshots=%s, mode=non-strict",
            live_path.path_name,
            capture_screenshot_dir,
        )
    else:
        cap = Capture(region, monitor_index, game_window_offset=game_window_offset)

    tracker = PerformanceTracker(cfg, version=WINGMAN_VERSION)
    analyzer = GameStateAnalyzer(cfg, tracker=tracker)  # also usable as a context manager via __enter__/__exit__

    unattended_mode = cfg.get("unattended_mode", False)
    unattended_active = threading.Event()
    if unattended_mode:
        unattended_active.set()
        logger.info("Unattended mode enabled from config")

    # Load mission restart timing from config
    mission_cfg = cfg.get("mission", {})
    startup_cfg = cfg.get("startup_state_detection", {})
    weapon_loop_interval = mission_cfg.get("weapon_loop_interval", 0.5)
    no_missiles_consecutive_required = int(mission_cfg.get("no_missiles_consecutive_required", 2))
    no_missiles_abort_grace_s = float(mission_cfg.get("no_missiles_abort_grace_s", 6.0))
    waiting_fallback_enabled = mission_cfg.get("waiting_fallback_enabled", True)
    waiting_fallback_diff_threshold = float(mission_cfg.get("waiting_fallback_diff_threshold", 0.08))
    waiting_fallback_score_threshold = int(mission_cfg.get("waiting_fallback_score_threshold", 4))
    waiting_fallback_consecutive_required = int(mission_cfg.get("waiting_fallback_consecutive_required", 2))
    waiting_fallback_min_elapsed_s = float(mission_cfg.get("waiting_fallback_min_elapsed_s", 6.0))
    respawn_clear_stability_s = float(mission_cfg.get("respawn_clear_stability_s", 1.5))
    starting_stalled_reclassify_after_s = float(mission_cfg.get("starting_stalled_reclassify_after_s", 20.0))
    starting_max_wait_s = float(mission_cfg.get("starting_max_wait_s", 90.0))
    unknown_max_wait_s = float(startup_cfg.get("unknown_max_wait_s", 90.0))
    unknown_state_since = 0.0
    startup_classification_complete = False
    capture_startup_failure_reason: str | None = None

    def _on_auto_mission_key():
        """Called when AUTO_MISSION_KEY is pressed. Activates unattended mode for the session."""
        if unattended_mode and not unattended_active.is_set():
            unattended_active.set()
            logger.info("Unattended mode activated by M key press")

    # Target tracker and HUD renderer
    target_tracker = TargetTracker(cfg)
    hud_renderer = HudRenderer.from_config(cfg)
    _tracking_cfg = cfg.get("tracking", {})
    _tracking_ctl_cfg = {
        "deadband": float(_tracking_cfg.get("deadband", 0.05)),
        "kp": float(_tracking_cfg.get("kp", 0.30)),
        "min_hold_sec": float(_tracking_cfg.get("min_hold_sec", 0.08)),
        "max_hold_sec": float(_tracking_cfg.get("max_hold_sec", 0.35)),
        "cooldown_sec": float(_tracking_cfg.get("command_cooldown_sec", 0.15)),
    }

    # Initialize controller with config-driven weapon loop interval and exit event
    j20_cfg = cfg.get("j20_mission", {})
    debug_cfg = cfg.get("debug", {})
    target_painting_mode = j20_cfg.get("target_painting_mode", False)
    capture_with_overlay = bool(debug_cfg.get("capture_with_overlay", True))
    ctrl = Controller(
        region,
        analyzer=analyzer,
        weapon_loop_interval=weapon_loop_interval,
        exit_event=exit_requested,
        capture=cap,
        on_auto_mission_key=_on_auto_mission_key,
        crops=analyzer.crops,
        target_painting_mode=target_painting_mode,
        simulate_os_input=replay_mode,
        disable_hotkeys=(replay_mode or capture_mode),
        capture_with_overlay=capture_with_overlay,
        starting_max_wait_s=starting_max_wait_s,
        telemetry_cfg=cfg.get("telemetry", {}),
    )

    # Wire FSM entry-hook callbacks (ADR 025) via analyzer public callback setters.
    analyzer.set_on_cancel_mission(ctrl.cancel_mission)
    analyzer.set_on_start_game_starting_loop(ctrl.start_game_starting_loop)
    def _on_lobby_play_click_cb(crop, frame):
        ctrl.click_crop(analyzer.crops[crop], block=False, count=1, region_name=crop)
        if live_capture is not None:
            # PLAY/READY detected while still in GAME_LOBBY: capture P2_070 at
            # this exact frame before the FSM transitions to GAME_WAITING.
            _now = time.time()
            live_capture.on_event("play_clicked", _now)
            live_capture.evaluate(frame, "GAME_LOBBY", _now)
            live_capture.evaluate(frame, "GAME_LOBBY", _now + 1e-6)
    analyzer.set_on_lobby_play_click(_on_lobby_play_click_cb)

    if live_capture is not None:
        def _on_good_luck_frame(gl_frame):
            # Good Luck OCR succeeded: capture immediately with the detected frame
            # before the 13s countdown begins and the main loop moves to GAME_BATTLE.
            _now = time.time()
            live_capture.on_event("good_luck_detected", _now)
            live_capture.evaluate(gl_frame, "GAME_STARTING", _now)
            live_capture.evaluate(gl_frame, "GAME_STARTING", _now + 1e-6)
        ctrl.set_on_good_luck_frame(_on_good_luck_frame)

        def _on_respawn_detected_frame(rs_frame):
            # Respawn OCR succeeded in background thread: capture with the exact OCR
            # frame.  The main loop's `frame` variable is already a newer capture by
            # the time is_respawning=True surfaces from the cache — this is the only
            # reliable way to get the actual respawn-screen frame.
            _now = time.time()
            _state = analyzer.game_state.name
            live_capture.on_event("respawn_detected", _now)
            live_capture.evaluate(rs_frame, _state, _now)
            live_capture.evaluate(rs_frame, _state, _now + 1e-6)
        analyzer.set_on_respawn_detected(_on_respawn_detected_frame)

        def _on_manual_takeover_frame(mt_frame):
            # Maneuver key pressed in GAME_BATTLE: frame captured just before the
            # FSM transition so it still shows the GAME_BATTLE HUD.  Evaluate with
            # "GAME_BATTLE" explicitly to match P2_020 expected_state.
            _now = time.time()
            live_capture.on_event("manual_takeover", _now)
            live_capture.evaluate(mt_frame, "GAME_BATTLE", _now)
            live_capture.evaluate(mt_frame, "GAME_BATTLE", _now + 1e-6)
        ctrl.set_on_manual_takeover_frame(_on_manual_takeover_frame)

    def _handle_lobby_popup(popup):
        current = analyzer.game_state
        if current not in (GameState.GAME_LOBBY, GameState.GAME_WAITING):
            logger.debug("Lobby popup '%s' suppressed — state is %s", popup, current.name)
            return
        if not ctrl.popup_click_allowed(popup):
            logger.debug("Lobby quick-scan: popup '%s' click suppressed by cooldown", popup)
            return
        logger.info("\033[93m📋 Lobby quick-scan: dismissing popup '%s'\033[0m", popup)
        ctrl.record_popup_click(popup)
        click_target = "event_refresh_dismiss" if popup == "event_refresh" else popup
        ctrl.click_crop(analyzer.crops[click_target], block=False, count=1, region_name=click_target)
        if popup == "REVEAL_ALL":
            def _reveal_all_second_click():
                time.sleep(3.0)
                if analyzer.game_state != GameState.GAME_LOBBY:
                    logger.debug("REVEAL_ALL second click suppressed — state is %s", analyzer.game_state)
                    return
                logger.info("\033[93m📋 REVEAL_ALL second click after 3s delay\033[0m")
                ctrl.click_crop(analyzer.crops["REVEAL_ALL"], block=False, count=1, region_name="REVEAL_ALL")
            threading.Thread(target=_reveal_all_second_click, daemon=True).start()
        elif popup == "INVITED":
            if replay_mode:
                logger.info("INVITED popup click-through skipped in replay mode")
                return
            def _click_ready_after_invite():
                time.sleep(1.5)
                new_frame = cap.grab_from_thread()
                if new_frame is None:
                    logger.warning("INVITED: frame capture returned None")
                    return
                ready = analyzer.scan_region_for_play_button(new_frame)
                if ready:
                    logger.info("\033[92m📋 INVITED accepted — clicking %s\033[0m", ready)
                    ctrl.click_crop(analyzer.crops[ready], block=False, count=1, region_name=ready)
            threading.Thread(target=_click_ready_after_invite, daemon=True).start()

    analyzer.set_on_lobby_popup_click(_handle_lobby_popup)
    analyzer.set_on_lobby_stall(lambda: ctrl.press_escape(hold_seconds=0.05, block=False))

    stats_tracker: "MissionStatsTracker | None" = None

    def _emit_capture_event(event_name: str) -> None:
        if replay_assertions is not None and replay_capture is not None:
            replay_assertions.on_event(event_name, replay_capture.elapsed_s())
        if live_capture is not None:
            live_capture.on_event(event_name, time.time())
        if stats_tracker is not None:
            stats_tracker.on_event(event_name, time.time())

    if replay_assertions is not None:
        def _on_fsm_transition(trigger_name, _prev_state_name, next_state_name, _timestamp_s):
            if replay_capture is None:
                return
            now_s = replay_capture.elapsed_s()
            replay_assertions.on_event(trigger_name, now_s)
            replay_assertions.on_event(f"state_enter:{next_state_name}", now_s)

        analyzer.set_on_fsm_transition(_on_fsm_transition)
    elif live_capture is not None:
        def _on_capture_fsm_transition(trigger_name, _prev_state_name, next_state_name, _timestamp_s):
            now_s = time.time()
            live_capture.on_event(trigger_name, now_s)
            live_capture.on_event(f"state_enter:{next_state_name}", now_s)

        analyzer.set_on_fsm_transition(_on_capture_fsm_transition)
    else:
        stats_tracker = MissionStatsTracker(
            version=WINGMAN_VERSION,
            output_dir="docs/performance",
        )

        def _stats_fsm_cb(trigger_name, prev_state_name, next_state_name, timestamp_s):
            stats_tracker.on_fsm_transition(trigger_name, prev_state_name, next_state_name, timestamp_s)

        analyzer.set_on_fsm_transition(_stats_fsm_cb)

    # Load loop interval from config
    loop_interval_sec = cfg.get("loop_interval_sec", 0.5)

    # Mission restart state machine
    respawn_state = RespawnState.IDLE
    respawn_cooldown_until = 0.0  # suppress re-detection for 10s after first trigger
    last_incoming_alert_ts = 0.0
    missile_ignore_until = 0.0       # suppress missile alerts for 10s after respawn
    last_click_to_alert_ts = 0.0
    battle_started_ts = 0.0
    no_missiles_zero_streak = 0
    missiles_fired_since_padlock = 0   # cumulative missiles fired since last padlock target-switch
    last_missile_count_for_padlock: "int | None" = None  # previous tick missile count for delta tracking
    last_game_state = None
    last_flare_reload_ts = 0.0    # cooldown: don't spam SPECIAL_ABILITY if flares stay at 2
    enemy_last_seen_ts = 0.0      # timestamp of last frame with red in ENEMY_CLOSE_BY (0 = not in battle yet)
    game_end_b_since = 0.0    # timestamp of GAME_END_B entry; used by stall timeout guard
    game_waiting_since = 0.0      # timestamp of GAME_WAITING entry; used by CANCEL scan + 180s timeout
    game_starting_stalled_since = 0.0  # timestamp of GAME_STARTING_STALLED entry; used by reclassify watchdog
    respawn_clear_since = 0.0  # timestamp since respawn cache has been continuously false
    last_cancel_scan_ts = 0.0     # last time CANCEL crop was scanned in GAME_WAITING
    last_play_reclick_ts = 0.0    # last time PLAY was re-clicked in GAME_WAITING
    play_reclick_interval = 45.0  # re-click interval when PLAY was absent (matchmaking may be active)
    play_reclick_missed_interval = 10.0  # re-click interval when PLAY was continuously visible (click missed)
    play_ever_absent = False      # whether PLAY disappeared at any tick since entering GAME_WAITING
    waiting_fallback_score = 0
    waiting_fallback_consecutive = 0
    lobby_escape_stop: "threading.Event | None" = None
    lobby_escape_thread: "threading.Thread | None" = None
    startup_time = time.time()
    battle_ever_reached = False

    def _stop_lobby_escape_loop():
        nonlocal lobby_escape_stop, lobby_escape_thread
        if lobby_escape_stop is not None:
            lobby_escape_stop.set()
            lobby_escape_stop = None
        lobby_escape_thread = None

    def _reset_waiting_fallback():
        nonlocal waiting_fallback_score, waiting_fallback_consecutive
        waiting_fallback_score = 0
        waiting_fallback_consecutive = 0

    def _handle_alive_transition():
        """Restart mission immediately when health transitions dead → alive.

        This is the ONLY post-respawn restart path (the scheduled/delayed
        restart machinery was removed 2026-07-31): as soon as health returns,
        the mission restarts. Because alive_event is one-shot and health often
        returns while respawn OCR is still flapping, deferrals RE-ARM the event
        instead of swallowing it — the handler retries every tick until the
        respawn-clear stability window is met, then restarts.
        """
        nonlocal respawn_state, enemy_last_seen_ts
        analyzer.alive_event.clear()
        enemy_last_seen_ts = time.time()  # reset so 30s clock starts fresh after respawn

        # Only early-restart when respawn has remained clear for a short stability window.
        # This avoids relaunching while respawn OCR/health signals are still flapping.
        if respawn_clear_since == 0.0:
            logger.debug("HEALTH ALIVE deferred — respawn screen still detected")
            analyzer.alive_event.set()  # retry next tick; do not lose the transition
            return
        clear_elapsed = time.time() - respawn_clear_since
        if clear_elapsed < respawn_clear_stability_s:
            logger.debug(
                "HEALTH ALIVE deferred — respawn clear stability %.2fs/%.2fs",
                clear_elapsed,
                respawn_clear_stability_s,
            )
            analyzer.alive_event.set()  # retry next tick; do not lose the transition
            return

        # Explicit disposition of every case — ADR 061 rule 3: the one-shot
        # event is never consumed silently (the 2026-08-01 08:00 incident lost
        # the only restart signal for a life to an unlogged state-gate miss).
        disposition = _alive_transition_disposition(
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
        _emit_capture_event("restart_last_mission")
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
        if stats_tracker is not None:
            stats_tracker.on_event("flare_reload", last_flare_reload_ts)

    def _handle_no_missiles():
        """End mission and eject when missile count reaches zero."""
        nonlocal no_missiles_zero_streak
        analyzer.no_missiles_event.clear()
        if not ctrl.is_mission_running():
            no_missiles_zero_streak = 0
            return
        if analyzer.game_state != GameState.GAME_BATTLE:
            # Ejecting is an auto-mode behavior. Without this gate, the narrow
            # window where a cancelled mission is still tearing down during a
            # manual takeover could inject NOSE_DOWN+AFTERBURNER into the
            # player's manual flight (and fire eject_started from a state
            # where it is invalid).
            no_missiles_zero_streak = 0
            return
        currently_respawning, _, _ = analyzer.get_respawn_cache_result()
        if currently_respawning:
            logger.debug("No-missiles suppressed — respawn screen active")
            no_missiles_zero_streak = 0
            return
        if battle_started_ts > 0 and (time.time() - battle_started_ts) < no_missiles_abort_grace_s:
            logger.debug(
                "No-missiles suppressed — post-mission-start grace (%.1fs remaining)",
                no_missiles_abort_grace_s - (time.time() - battle_started_ts),
            )
            no_missiles_zero_streak = 0
            return
        if time.time() < missile_ignore_until:
            logger.debug("No-missiles suppressed — post-respawn grace (%.1fs remaining)",
                         missile_ignore_until - time.time())
            no_missiles_zero_streak = 0
            return

        no_missiles_zero_streak += 1
        if no_missiles_zero_streak < no_missiles_consecutive_required:
            logger.debug(
                "No-missiles event awaiting confirmation (%d/%d)",
                no_missiles_zero_streak,
                no_missiles_consecutive_required,
            )
            return

        no_missiles_zero_streak = 0
        _emit_capture_event("missiles_empty")
        analyzer.trigger_event("eject_started")
        ctrl.eject_and_dive(
            on_complete=lambda: (
                analyzer.trigger_event("eject_complete")
                if analyzer.game_state == GameState.GAME_BATTLE_EJECT
                else None
            )
        )

    def _deploy_flares_on_new_incoming() -> bool:
        """Deploy flares in a burst when a new incoming OCR detection arrives."""
        nonlocal last_incoming_alert_ts
        incoming_detected, _, _ = analyzer.get_incoming_cache_result()
        incoming_ts = analyzer.get_incoming_cache_timestamp()

        if incoming_detected and incoming_ts > last_incoming_alert_ts:
            if time.time() < missile_ignore_until:
                logger.debug("Missile alert suppressed — post-respawn grace period (%.1fs remaining)",
                             missile_ignore_until - time.time())
                last_incoming_alert_ts = incoming_ts
                return False
            logger.info("\033[95m🚀 INCOMING MISSILE DETECTED - Deploying flares\033[0m")
            last_incoming_alert_ts = incoming_ts
            try:
                tracker.record_reaction(time.time() - incoming_ts)
            except Exception as e:
                logger.warning("PerformanceTracker: record_reaction failed: %s", e)

            if stats_tracker is not None:
                stats_tracker.on_event("flare_burst_deployed", time.time())

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
                time.sleep(loop_interval_sec)
                continue

            if replay_assertions is not None and replay_capture is not None:
                now_s = replay_capture.elapsed_s()
                for step in replay_capture.consume_activated_steps():
                    replay_assertions.on_step_activated(step, now_s)
                    # Report current state before injecting any trigger so that a
                    # state assertion on this step evaluates against the pre-transition state.
                    replay_assertions.on_state(analyzer.game_state.name, now_s)
                    if step.inject_trigger:
                        logger.info(
                            "Replay: injecting FSM trigger '%s' at %.2fs",
                            step.inject_trigger, now_s,
                        )
                        analyzer.trigger_event(step.inject_trigger)
                        # on_event for the fired trigger is called via _on_fsm_transition callback.
                        # Also report state AFTER the transition so that a state assertion on this
                        # step can match the post-transition state (e.g. GAME_END_B after
                        # click_to_detected fires).  If the assertion already passed on the
                        # pre-transition report this call is a no-op.
                        replay_assertions.on_state(analyzer.game_state.name, now_s)

            if live_capture is not None:
                now_s = time.time()
                if live_capture.is_complete():
                    logger.info("Capture finished — exiting main loop")
                    break

            game_state = analyzer.analyze_frame(frame)
            if game_state.get('is_respawning'):
                respawn_clear_since = 0.0
            elif respawn_clear_since == 0.0:
                respawn_clear_since = time.time()

            if live_capture is not None:
                _now = time.time()
                live_capture.evaluate(frame, analyzer.game_state.name, _now)
                live_capture.evaluate(frame, analyzer.game_state.name, _now + 1e-6)
                pending_inject = live_capture.consume_pending_inject_trigger()
                if pending_inject is not None:
                    logger.info("Capture: injecting FSM trigger '%s'", pending_inject)
                    analyzer.trigger_event(pending_inject)
                if live_capture.has_failures():
                    raise RuntimeError(
                        "Capture failure(s): "
                        + json.dumps(live_capture.to_dict(), indent=2)
                    )

            if replay_assertions is not None and replay_capture is not None:
                now_s = replay_capture.elapsed_s()
                replay_assertions.on_state(analyzer.game_state.name, now_s)
                replay_assertions.tick(now_s)
                if replay_assertions.has_failures():
                    raise RuntimeError(
                        "Replay assertion failure(s): " + "; ".join(replay_assertions.failures)
                    )

            # Log game state transitions
            current_game_state = game_state.get('game_state')

            if current_game_state == GameState.GAME_UNKNOWN:
                if unknown_state_since == 0.0:
                    unknown_state_since = time.time()
                unknown_elapsed = time.time() - unknown_state_since
                if not startup_classification_complete and unknown_elapsed >= unknown_max_wait_s:
                    capture_startup_failure_reason = f"unknown_timeout_after_{unknown_max_wait_s:.1f}s"
                    logger.error(
                        "GAME_UNKNOWN startup classification timeout after %.1fs",
                        unknown_elapsed,
                    )
                    if live_capture is not None:
                        raise RuntimeError(
                            "Capture startup classification failure: "
                            + capture_startup_failure_reason
                        )
            else:
                unknown_state_since = 0.0

            if current_game_state != last_game_state:
                logger.info("\033[96m🎮 Game state: %s → %s\033[0m",
                            last_game_state.name if last_game_state else "UNKNOWN",
                            current_game_state.name if current_game_state else "UNKNOWN")
                prev_game_state = last_game_state
                last_game_state = current_game_state
                if current_game_state != GameState.GAME_UNKNOWN:
                    startup_classification_complete = True
                if current_game_state == GameState.GAME_END_B:
                    game_end_b_since = time.time()
                    # GAME_END_B is not a respawn flow; clear the respawn latch
                    # and any pending alive event so the match-end click-through
                    # cannot relaunch a mission.
                    respawn_state = RespawnState.IDLE
                    analyzer.alive_event.clear()
                    # A match can end mid-eject with no respawn ever detected —
                    # only respawn stopped the sequence, so the 120s afterburner
                    # hold survived into the NEXT match. That stale _ejecting
                    # flag let a maneuver key trigger "manual takeover" during
                    # the next round's GAME_STARTING wait, wedging the FSM and
                    # locking out the 'u' resume (observed 2026-08-01 02:54:55).
                    ctrl.stop_eject_sequence(reason="match_ended")
                else:
                    game_end_b_since = 0.0
                if current_game_state == GameState.GAME_LOBBY:
                    game_waiting_since = 0.0
                    if prev_game_state is not None:
                        ctrl.cancel_mission()
                    try:
                        tracker.on_enter_game_lobby()
                    except Exception as e:
                        logger.warning("PerformanceTracker: on_enter_game_lobby failed: %s", e)
                    _stop_lobby_escape_loop()
                    _stop_ev = threading.Event()
                    lobby_escape_stop = _stop_ev
                    def _lobby_escape_loop(_stop=_stop_ev):
                        while not _stop.wait(timeout=45.0):
                            if analyzer.game_state != GameState.GAME_LOBBY:
                                return
                            logger.info("GAME_LOBBY escape loop: pressing ESC")
                            ctrl.press_escape(hold_seconds=0.05, block=False)
                    lobby_escape_thread = threading.Thread(
                        target=_lobby_escape_loop, daemon=True, name="lobby-escape-loop"
                    )
                    lobby_escape_thread.start()
                else:
                    _stop_lobby_escape_loop()
                if current_game_state == GameState.GAME_WAITING:
                    game_waiting_since = time.time()
                    last_cancel_scan_ts = time.time()         # first scan after 3s, not immediately
                    last_play_reclick_ts = time.time()        # don't re-click immediately either
                    play_ever_absent = False
                    _reset_waiting_fallback()
                else:
                    game_waiting_since = 0.0
                    _reset_waiting_fallback()
                if current_game_state == GameState.GAME_STARTING_STALLED:
                    game_starting_stalled_since = time.time()
                else:
                    game_starting_stalled_since = 0.0
                if current_game_state == GameState.GAME_BATTLE:
                    battle_ever_reached = True
                    enemy_last_seen_ts = time.time()  # assume enemy present on battle entry
                    battle_started_ts = time.time()
                    no_missiles_zero_streak = 0
                    missiles_fired_since_padlock = 0
                    last_missile_count_for_padlock = None
                elif current_game_state != GameState.GAME_BATTLE:
                    no_missiles_zero_streak = 0
                    _battle_states = {GameState.GAME_BATTLE, GameState.GAME_BATTLE_MANUAL, GameState.GAME_BATTLE_EJECT}
                    if prev_game_state in _battle_states and current_game_state not in _battle_states:
                        target_tracker.reset()

            # Watchdog: if GAME_BATTLE not entered within 10 minutes, shut down wingman and PC.
            # Skipped in replay/capture modes. Uses `shutdown /s /t 0` which does not require
            # elevated privileges on Windows — standard users hold SeShutdownPrivilege by default.
            if (not battle_ever_reached
                    and not replay_mode and not capture_mode
                    and time.time() - startup_time > 600.0):
                logger.warning(
                    "GAME_BATTLE not reached within 10 minutes of startup — shutting down wingman and computer"
                )
                exit_requested.set()
                if sys.platform == "win32":
                    subprocess.run(["shutdown", "/s", "/t", "0"], check=False)
                else:
                    subprocess.run(["shutdown", "-h", "now"], check=False)
                break

            missiles_snapshot = analyzer.get_ammo_missiles()
            if missiles_snapshot is not None and missiles_snapshot > 0:
                no_missiles_zero_streak = 0

            # Target-spread: when 2 cumulative missiles fired in GAME_BATTLE, press padlock twice
            # to switch to the next target so missiles are spread across enemy jets.
            if current_game_state == GameState.GAME_BATTLE and missiles_snapshot is not None:
                if last_missile_count_for_padlock is not None and missiles_snapshot < last_missile_count_for_padlock:
                    missiles_fired_since_padlock += last_missile_count_for_padlock - missiles_snapshot
                    if missiles_fired_since_padlock >= 2:
                        logger.info("Controller: %d missiles fired — switching padlock target", missiles_fired_since_padlock)
                        ctrl.padlock_target_switch()
                        missiles_fired_since_padlock = 0
                if missiles_snapshot > (last_missile_count_for_padlock or 0):
                    # Missiles reloaded — reset so we don't carry over a pre-reload partial count
                    missiles_fired_since_padlock = 0
                last_missile_count_for_padlock = missiles_snapshot


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
                    analyzer.trigger_event("waiting_timeout")
                    game_waiting_since = 0.0
                    _reset_waiting_fallback()
                elif time.time() - last_cancel_scan_ts >= 3.0:
                    last_cancel_scan_ts = time.time()
                    cancel_detected = analyzer.scan_region_for_cancel(frame)
                    if cancel_detected:
                        logger.info(
                            "\033[92m✓ CANCEL detected (%.1fs) — matchmaking confirmed → GAME_STARTING\033[0m",
                            elapsed_waiting)
                        analyzer.trigger_event("cancel_detected")  # on_enter_GAME_STARTING fires game-starting loop
                        _reset_waiting_fallback()
                        if live_capture is not None:
                            # on_event("cancel_detected") was already called inside trigger_event
                            # via _on_capture_fsm_transition.  Evaluate twice here with the
                            # explicit pre-transition state so the debounce completes on this
                            # exact CANCEL frame.  The closure-based approach is unreliable
                            # because trigger_event's post_callbacks run before _on_fsm_transition,
                            # making the call order non-obvious and error-prone.
                            _now = time.time()
                            live_capture.evaluate(frame, "GAME_WAITING", _now)
                            live_capture.evaluate(frame, "GAME_WAITING", _now + 1e-6)
                    else:
                        crop = next((c for c in ("PLAY", "READY") if c in analyzer.crops), None)
                        visible_crop = analyzer.scan_region_for_play_button(frame) if crop else None
                        play_visible = visible_crop is not None
                        if not play_visible:
                            play_ever_absent = True

                        waiting_fallback_score, waiting_fallback_consecutive, fallback_triggered, _ = _update_waiting_fallback(
                            analyzer,
                            frame,
                            elapsed_waiting,
                            play_visible,
                            waiting_fallback_score,
                            waiting_fallback_consecutive,
                            enabled=waiting_fallback_enabled,
                            diff_threshold=waiting_fallback_diff_threshold,
                            score_threshold=waiting_fallback_score_threshold,
                            consecutive_required=waiting_fallback_consecutive_required,
                            min_elapsed_s=waiting_fallback_min_elapsed_s,
                            logger=logger,
                        )

                        if fallback_triggered:
                            logger.info(
                                "\033[92m✓ GAME_WAITING confirmed via QUEUE_FALLBACK (%.1fs) — matchmaking confirmed → GAME_STARTING\033[0m",
                                elapsed_waiting,
                            )
                            analyzer.trigger_event("cancel_detected")
                            _reset_waiting_fallback()
                            continue

                        # Use short interval when PLAY was never absent (click clearly missed).
                        # Use long interval when PLAY disappeared at some point (matchmaking may still be active).
                        effective_interval = play_reclick_interval if play_ever_absent else play_reclick_missed_interval
                        if crop and time.time() - last_play_reclick_ts >= effective_interval:
                            # Only re-click if PLAY/READY is actually visible — clicking PLAY while
                            # matchmaking is in progress cancels it. If PLAY isn't visible the game
                            # is still processing the previous click; leave it alone.
                            if visible_crop:
                                reason = "click missed" if not play_ever_absent else "returned from matchmaking"
                                logger.info(
                                    "GAME_WAITING: CANCEL not found (%.1fs) and %s visible — re-clicking (%s)",
                                    elapsed_waiting, visible_crop, reason)
                                last_play_reclick_ts = time.time()
                                play_ever_absent = False  # reset: treat next window as a fresh click
                                ctrl.click_crop(analyzer.crops[visible_crop], block=False, count=1, region_name=visible_crop)
                            else:
                                logger.debug(
                                    "GAME_WAITING: CANCEL not found (%.1fs) but PLAY not visible — matchmaking in progress, waiting",
                                    elapsed_waiting)
                                last_play_reclick_ts = time.time()  # reset timer to avoid spamming OCR
                        elif crop:
                            logger.debug(
                                "GAME_WAITING: CANCEL not found (%.1fs) — waiting %.1fs before re-click (%s)",
                                elapsed_waiting, effective_interval - (time.time() - last_play_reclick_ts),
                                "click missed" if not play_ever_absent else "returned from matchmaking")

            # GAME_END_B stall guard: if click-to OCR cache gets stuck, force recovery
            if (current_game_state == GameState.GAME_END_B
                    and game_end_b_since > 0
                    and time.time() - game_end_b_since > 30.0):
                logger.warning("GAME_END_B timeout — click-to OCR may be stuck; forcing recovery to GAME_LOBBY")
                analyzer.trigger_event("manual_reset")
                game_end_b_since = 0.0

            # GAME_STARTING_STALLED guard: re-enter GAME_UNKNOWN after a short hold so
            # the unknown-state classifier can route to lobby or battle from live screen state.
            if (current_game_state == GameState.GAME_STARTING_STALLED
                    and game_starting_stalled_since > 0
                    and time.time() - game_starting_stalled_since >= starting_stalled_reclassify_after_s):
                logger.warning(
                    "GAME_STARTING_STALLED persisted for %.0fs — reclassifying via GAME_UNKNOWN",
                    starting_stalled_reclassify_after_s,
                )
                analyzer.trigger_event("starting_stalled_reclassify")
                game_starting_stalled_since = 0.0
                continue

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

            # Target tracking — HSV contour detection + proportional roll correction.
            # Only active during GAME_BATTLE (not GAME_BATTLE_MANUAL) and only when
            # a mission is running (safety: no autonomous roll without mission control).
            tracking_obs: "dict | None" = None
            if (target_tracker.enabled
                    and current_game_state == GameState.GAME_BATTLE
                    and ctrl.is_mission_running()):
                tracking_obs = target_tracker.update(frame)
                err = tracking_obs.get("error_norm")
                if err is not None and tracking_obs.get("visible"):
                    cmd = ctrl.orient_nose_to_target(err, **_tracking_ctl_cfg)
                    if cmd is not None:
                        logger.debug(
                            "Tracker: roll_%s  err=%.2f  mode=%s",
                            cmd, err, tracking_obs["mode"],
                        )

            # HUD renderer — annotated snapshot; always runs in GAME_BATTLE when enabled.
            if hud_renderer is not None and current_game_state in (
                GameState.GAME_BATTLE, GameState.GAME_BATTLE_MANUAL
            ):
                hud_renderer.maybe_render(
                    frame,
                    tracking_obs,
                    current_game_state.name,
                    game_state.get("health"),
                    analyzer.get_ammo_missiles(),
                    analyzer.get_ammo_flares(),
                )

            # Detect respawn — from overlay OCR, or (ADR 064 dual mode) from the
            # health detector's composite evidence when OCR missed the episode.
            health_fallback_respawn = analyzer.health_respawn_event.is_set()
            if health_fallback_respawn:
                analyzer.health_respawn_event.clear()
                logger.info("\033[93m💛 HEALTH-FALLBACK RESPAWN accepted by main loop (ADR 064 dual)\033[0m")
            if game_state.get('is_respawning') or health_fallback_respawn:
                # Interrupt any in-progress eject_and_dive immediately on any detected respawn,
                # independent of the mission-restart dedup cooldown below — a real respawn screen
                # means afterburner should release now, not hold until the 120s safety timeout.
                ctrl.stop_eject_sequence()
                if respawn_state == RespawnState.IDLE:
                    if time.time() < respawn_cooldown_until:
                        logger.debug("RESPAWN seen but suppressed by cooldown (%.1fs remaining)",
                                     respawn_cooldown_until - time.time())
                    else:
                        logger.info("\033[91m⚠ RESPAWN DETECTED - Cancelling active missions\033[0m")
                        respawn_cooldown_until = time.time() + 10.0
                        missile_ignore_until = time.time() + 10.0
                        enemy_last_seen_ts = time.time()  # reset so 30s clock starts fresh after respawn
                        ctrl.cancel_mission()
                        analyzer.reset_health_for_respawn()
                        # A death invalidates any pending (re-armed) alive event
                        # from the previous life — without this, a deferred
                        # HEALTH ALIVE could restart the mission after the NEXT
                        # respawn clears but before health actually returns.
                        analyzer.alive_event.clear()
                        _emit_capture_event("respawn_detected")
                        # Live capture is handled via analyzer.set_on_respawn_detected —
                        # the callback fires from the background OCR thread with the exact
                        # respawn-screen frame, avoiding the stale-frame race where the
                        # main loop's `frame` variable has already advanced past the
                        # respawn overlay by the time is_respawning surfaces from the cache.
                        if current_game_state == GameState.GAME_BATTLE_MANUAL:
                            # Death ends manual takeover. Fire the P2_040 capture
                            # BEFORE the FSM transition so the screenshot still
                            # shows the manual-mode HUD, then return to auto: the
                            # mission restarts as soon as health returns. (An
                            # earlier stay-manual-through-respawn design left the
                            # aircraft flying uncommanded after every death — the
                            # scheduled restart was GAME_BATTLE-gated and the log
                            # promised a restart that could never fire, observed
                            # 2026-07-31 07:42.)
                            if live_capture is not None:
                                _cap_now = time.time()
                                live_capture.on_event("respawn_detected", _cap_now)
                                live_capture.evaluate(frame, "GAME_BATTLE_MANUAL", _cap_now)
                                live_capture.evaluate(frame, "GAME_BATTLE_MANUAL", _cap_now + 1e-6)
                            analyzer.trigger_event("respawn_reset")
                        ctrl.set_auto_respawn_restart(True)  # always restart after respawn
                        # Wait for the cancelled mission thread to release its lock
                        # so the health-alive restart can't be skipped by a
                        # teardown race (is_mission_running would read True).
                        logger.info("Waiting for mission lock to release before restart...")
                        for _ in range(50):
                            if not ctrl.is_mission_running():
                                break
                            time.sleep(0.1)
                        else:
                            logger.warning("Timeout waiting for mission lock release; restart may be delayed.")
                        # Latch: dedupes re-detection while the screen persists.
                        respawn_state = RespawnState.RESPAWNING
                        logger.info("Respawn screen active — mission restarts when health returns")

                logger.info("\033[91mRESPAWN ACTIVE (%.0f%% confidence)\033[0m", game_state.get('respawn_confidence', 0) * 100)

                time.sleep(1)
                continue

            # Respawn screen cleared. No scheduled restart: the health alive
            # transition (_handle_alive_transition) restarts the mission the
            # moment health returns — it re-arms itself while respawn OCR is
            # still flapping, so the one-shot event cannot be lost.
            if respawn_state == RespawnState.RESPAWNING:
                logger.info("\033[92m✓ Gameplay resumed — mission restarts on health return\033[0m")
                respawn_state = RespawnState.IDLE

            # Log "Click to Continue" prompt when newly detected (informational only).
            click_to_detected, _, _ = analyzer.get_click_to_cache_result()
            click_to_ts = analyzer.get_click_to_cache_timestamp()
            if click_to_detected and click_to_ts > last_click_to_alert_ts:
                logger.info("\033[93m📋 CLICK TO CONTINUE detected in CLICK_TO_CONTINUE region\033[0m")
                last_click_to_alert_ts = click_to_ts
                _emit_capture_event("click_to_detected")
                if live_capture is not None:
                    # Capture this exact pre-click frame for click_to steps (for example
                    # P1_070) before click-through advances to later end-screen visuals.
                    now_s = time.time()
                    live_capture.evaluate(frame, analyzer.game_state.name, now_s)
                    live_capture.evaluate(frame, analyzer.game_state.name, now_s + 1e-6)
                ctrl.cancel_mission()
                threading.Thread(
                    target=_click_through_game_end,
                    args=(ctrl, analyzer, logger),
                    daemon=True,
                ).start()

            if replay_capture is not None and replay_capture.is_finished(grace_s=args.replay_exit_after):
                logger.info("Replay finished (grace %.1fs) — exiting main loop", args.replay_exit_after)
                break

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
        if replay_mode:
            intents_output = Path(args.replay_intents_output)
            intents_output.parent.mkdir(parents=True, exist_ok=True)
            intents_output.write_text(
                json.dumps(
                    {
                        "generated_at": time.time(),
                        "replay_config": args.replay_config,
                        "replay_path": args.replay_path,
                        "action_intents": ctrl.get_action_intents(),
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
            logger.info("Replay action intents saved to %s", intents_output)

            assertions_output = Path(args.replay_assertions_output)
            assertions_output.parent.mkdir(parents=True, exist_ok=True)
            assertions_output.write_text(
                json.dumps(
                    {
                        "generated_at": time.time(),
                        "replay_config": args.replay_config,
                        "replay_path": args.replay_path,
                        "assertions": replay_assertions.to_dict() if replay_assertions is not None else None,
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
            logger.info("Replay assertions saved to %s", assertions_output)
        if live_capture is not None:
            summary_output = Path(args.capture_summary)
            summary_output.parent.mkdir(parents=True, exist_ok=True)
            not_updated = live_capture.not_updated_screenshot_names()
            if not_updated:
                logger.warning(
                    "Capture did not update %d screenshot(s): %s",
                    len(not_updated),
                    ", ".join(not_updated),
                )
            else:
                logger.info("Capture updated all requested screenshots")
            summary_output.write_text(
                json.dumps(
                    {
                        "generated_at": time.time(),
                        "capture_config": args.capture_path_config,
                        "capture_path": args.capture_path,
                        "startup_failure_reason": capture_startup_failure_reason,
                        "summary": live_capture.to_dict(),
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
            logger.info("Capture summary saved to %s", summary_output)
        # ADR 062 Phase A: per-session shadow-detector agreement summary.
        # Emitted BEFORE the cleanups — analyzer.cleanup() can block on stuck
        # OCR futures (the 2026-08-01 10:01 session ended there and lost its
        # summary and stats JSON), and this only reads analyzer fields.
        shadow_summary = analyzer.shadow_respawn_summary()
        if shadow_summary is not None:
            logger.info("Shadow respawn detector (ADR 062 Phase A): %s", json.dumps(shadow_summary))
        if hasattr(cap, "cleanup"):
            cap.cleanup()
        ctrl.cleanup()
        analyzer.cleanup()
        if stats_tracker is not None:
            try:
                extra = {"respawn_shadow": shadow_summary} if shadow_summary is not None else None
                stats_tracker.finalize(run_id=tracker.run_id, extra=extra)
                stats_tracker.print_summary()
            except Exception as e:
                logger.warning("MissionStatsTracker: finalize failed: %s", e)


if __name__ == "__main__":
    main()
