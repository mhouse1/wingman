import argparse
import json
import sys
import yaml
import time
import logging
import threading
from datetime import datetime
from enum import Enum, auto
from pathlib import Path

try:
    import colorama
    colorama.init()
except ImportError:
    colorama = None

WINGMAN_VERSION = "1.8.2"
WINGMAN_VERSION_DETAILS = "TBD"

from .capture import Capture
from .controller import Controller, REGION_CLICK_TO_CONTINUE, REGION_PLAY_BUTTON, MISSION_J20_KEY
from .analyzer import GameStateAnalyzer, GameState, GameEvent
from .hud import HudRenderer
from .mission_stats import MissionStatsTracker
from .performance import PerformanceTracker
from .tick_handlers import (
    AmmoEventsHandler,
    BehaviorTreeHandler,
    EnemyPresenceHandler,
    RespawnHandler,
    TrackingHudHandler,
    WaitingFallbackHandler,
)
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
    
    @relation(SAF-002, scope=function)
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
    parser.add_argument("--capture-pin-region", action="store_true",
                        help="Pin capture to the config region instead of auto-detecting the "
                             "game window. Required for the ADR 045 presenter lane (frames are "
                             "drawn AT the region); wrong for real-game capture (make p1/p2/p3), "
                             "where the game window sits at its own desktop offset.")
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
        # Two capture lanes share this branch and need OPPOSITE offsets:
        # - ADR 045 presenter lane (--capture-pin-region): the presenter draws
        #   the timed screenshots AT the config region, so capture must be
        #   pinned there — auto-detecting the game window is actively wrong
        #   (when capture followed the game window it recorded live gameplay
        #   instead of the presented frames and every step failed — 2026-08-09
        #   19:45 run).
        # - Real-game capture (make p1/p2/p3, no flag): the game window sits at
        #   its own desktop offset (observed +66+69), so capture must
        #   auto-detect exactly like a normal run. Pinning here shifts every
        #   crop by that offset, OCR classifies nothing, and the lane dies at
        #   the 90 s startup gate with zero screenshots (2026-08-13 21:48 and
        #   21:50 runs — the pin was unconditional from 2026-08-09 until now).
        if args.capture_pin_region:
            lane_offset = (int(region[0]), int(region[1]))
            cap = Capture(region, monitor_index, game_window_offset=lane_offset)
            offset_note = f"capture pinned to config region at ({lane_offset[0]}, {lane_offset[1]})"
        else:
            cap = Capture(region, monitor_index, game_window_offset=game_window_offset)
            offset_note = "game-window auto-detect (real-game capture)"
        logger.info(
            "Capture mode enabled: path=%s, screenshots=%s, mode=non-strict, %s",
            live_path.path_name,
            capture_screenshot_dir,
            offset_note,
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
    starting_stalled_reclassify_after_s = float(mission_cfg.get("starting_stalled_reclassify_after_s", 20.0))
    starting_max_wait_s = float(mission_cfg.get("starting_max_wait_s", 90.0))
    # Startup stall watchdog: exit wingman (never the host) if battle is never reached.
    startup_stall_exit_after_s = float(mission_cfg.get("startup_stall_exit_after_s", 600.0))
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
        good_luck_wait_s=float(mission_cfg.get("good_luck_wait_s", 13.0)),
        good_luck_bypass_on_alive=bool(mission_cfg.get("good_luck_bypass_on_alive", True)),
        telemetry_cfg=cfg.get("telemetry", {}),
        missile_evade_cfg=cfg.get("behavior_tree", {}).get("missile_evade", {}),
        capture_stale_inject_s=float(mission_cfg.get("capture_stale_inject_s", 10.0)),
    )

    # Wire FSM entry-hook callbacks (ADR 025) via the analyzer event registry
    # (ADR 060 Phase 1). Every subscriber is named; a duplicate name raises at
    # wiring time rather than silently replacing an earlier subscriber.
    analyzer.subscribe(GameEvent.CANCEL_MISSION, ctrl.cancel_mission, name="controller")
    analyzer.subscribe(GameEvent.START_GAME_STARTING_LOOP,
                       ctrl.start_game_starting_loop, name="controller")
    def _on_lobby_play_click_cb(crop, frame):
        ctrl.click_crop(analyzer.crops[crop], block=False, count=1, region_name=crop)
        if live_capture is not None:
            # PLAY/READY detected while still in GAME_LOBBY: capture P2_070 at
            # this exact frame before the FSM transitions to GAME_WAITING.
            _now = time.time()
            live_capture.on_event("play_clicked", _now)
            live_capture.evaluate(frame, "GAME_LOBBY", _now)
            live_capture.evaluate(frame, "GAME_LOBBY", _now + 1e-6)
    analyzer.subscribe(GameEvent.LOBBY_PLAY_CLICK, _on_lobby_play_click_cb, name="controller")

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
        analyzer.subscribe(GameEvent.RESPAWN_DETECTED, _on_respawn_detected_frame,
                           name="live_capture")

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

    analyzer.subscribe(GameEvent.LOBBY_POPUP_CLICK, _handle_lobby_popup, name="controller")
    analyzer.subscribe(GameEvent.LOBBY_STALL,
                       lambda: ctrl.press_escape(hold_seconds=0.05, block=False),
                       name="controller")

    def _emit_capture_event(event_name: str) -> None:
        if replay_assertions is not None and replay_capture is not None:
            replay_assertions.on_event(event_name, replay_capture.elapsed_s())
        if live_capture is not None:
            live_capture.on_event(event_name, time.time())
        stats_tracker.on_event(event_name, time.time())

    # FSM_TRANSITION subscribers are independent (ADR 060 Phase 1). These were
    # mutually exclusive if/elif/else branches only because the old single-slot
    # setter could hold one callback — so mission stats were silently not
    # recorded during replay and capture runs.
    if replay_assertions is not None:
        def _on_fsm_transition(trigger_name, _prev_state_name, next_state_name, _timestamp_s):
            if replay_capture is None:
                return
            now_s = replay_capture.elapsed_s()
            replay_assertions.on_event(trigger_name, now_s)
            replay_assertions.on_event(f"state_enter:{next_state_name}", now_s)

        analyzer.subscribe(GameEvent.FSM_TRANSITION, _on_fsm_transition, name="replay_assertions")

    if live_capture is not None:
        def _on_capture_fsm_transition(trigger_name, _prev_state_name, next_state_name, _timestamp_s):
            now_s = time.time()
            live_capture.on_event(trigger_name, now_s)
            live_capture.on_event(f"state_enter:{next_state_name}", now_s)

        analyzer.subscribe(GameEvent.FSM_TRANSITION, _on_capture_fsm_transition, name="live_capture")

    # Stats run in every lane now. Replay/capture runs write their JSON to
    # tests/test-output instead of docs/performance: those directories feed the
    # performance-trend and ADR 064 shadow ledgers, which must contain live
    # sessions only.
    _stats_live_lane = replay_assertions is None and live_capture is None
    stats_tracker = MissionStatsTracker(
        version=WINGMAN_VERSION,
        output_dir="docs/performance" if _stats_live_lane else "tests/test-output",
    )

    def _stats_fsm_cb(trigger_name, prev_state_name, next_state_name, timestamp_s):
        stats_tracker.on_fsm_transition(trigger_name, prev_state_name, next_state_name, timestamp_s)

    analyzer.subscribe(GameEvent.FSM_TRANSITION, _stats_fsm_cb, name="mission_stats")

    # Load loop interval from config
    loop_interval_sec = cfg.get("loop_interval_sec", 0.5)

    # Mission restart state machine
    last_click_to_alert_ts = 0.0
    last_game_state = None
    game_end_b_since = 0.0    # timestamp of GAME_END_B entry; used by stall timeout guard
    game_starting_stalled_since = 0.0  # timestamp of GAME_STARTING_STALLED entry; used by reclassify watchdog
    lobby_escape_stop: "threading.Event | None" = None
    lobby_escape_thread: "threading.Thread | None" = None
    bt_active = str(cfg.get("behavior_tree", {}).get("mode", "off")).lower() == "active"
    ammo_events = AmmoEventsHandler(
        analyzer, ctrl, mission_cfg,
        perf_tracker=tracker, stats_tracker=stats_tracker,
        emit_capture_event=_emit_capture_event,
        # ADR 024 3.1b: in active mode the Eject leaf actuates; this handler
        # keeps the debounce and every suppression gate and hands over only
        # the confirmed verdict.
        bt_owns_eject=bt_active,
    )
    enemy_presence = EnemyPresenceHandler(analyzer, ctrl)
    behavior_tree = BehaviorTreeHandler(
        analyzer, ctrl, cfg.get("behavior_tree", {}), j20_cfg, cfg.get("minimap", {}),
        ammo_events=ammo_events, stats_tracker=stats_tracker,
    )
    tracking_hud = TrackingHudHandler(
        target_tracker, hud_renderer, analyzer, ctrl, cfg.get("tracking", {}),
    )
    respawn = RespawnHandler(
        analyzer, ctrl, mission_cfg,
        enemy_presence=enemy_presence, ammo_events=ammo_events,
        behavior_tree=behavior_tree,
        live_capture=live_capture, emit_capture_event=_emit_capture_event,
        disposition_fn=_alive_transition_disposition,
        respawn_state_enum=RespawnState,
    )
    waiting_fallback = WaitingFallbackHandler(
        analyzer, ctrl, mission_cfg, live_capture=live_capture,
    )
    startup_time = time.time()
    battle_ever_reached = False

    def _stop_lobby_escape_loop():
        nonlocal lobby_escape_stop, lobby_escape_thread
        if lobby_escape_stop is not None:
            lobby_escape_stop.set()
            lobby_escape_stop = None
        lobby_escape_thread = None

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
            respawn.note_respawn_screen(bool(game_state.get('is_respawning')))

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
                    respawn.to_idle()
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
                waiting_fallback.on_state_change(current_game_state, prev_game_state)
                enemy_presence.on_state_change(current_game_state, prev_game_state)
                behavior_tree.on_state_change(current_game_state, prev_game_state)
                ammo_events.on_state_change(current_game_state, prev_game_state)
                tracking_hud.on_state_change(current_game_state, prev_game_state)
                if current_game_state == GameState.GAME_STARTING_STALLED:
                    game_starting_stalled_since = time.time()
                else:
                    game_starting_stalled_since = 0.0
                if current_game_state == GameState.GAME_BATTLE:
                    battle_ever_reached = True

            # Watchdog: if GAME_BATTLE is not entered within the stall window, exit
            # WINGMAN ONLY — never the machine. Skipped in replay/capture modes.
            #
            # This used to run `shutdown -h now` (`shutdown /s /t 0` on Windows),
            # which took the whole host down on any long stall: a game stuck on a
            # login screen, a matchmaking queue that never filled, or a capture
            # backend that came up before the game did. That destroys the session
            # under investigation along with everything else running on the box.
            # The stall is a wingman-level condition and gets a wingman-level
            # response — record why, then leave through the normal exit path so
            # cleanup() releases every held key and the stats/perf artifacts are
            # still written.
            if (not battle_ever_reached
                    and not replay_mode and not capture_mode
                    and time.time() - startup_time > startup_stall_exit_after_s):
                logger.error(
                    "STALL: GAME_BATTLE not reached within %.0fs of startup "
                    "(last state %s) — exiting wingman. The computer is left "
                    "running; check the game window and relaunch.",
                    startup_stall_exit_after_s,
                    getattr(current_game_state, "name", current_game_state),
                )
                exit_requested.set()
                break

            missiles_snapshot = analyzer.get_ammo_missiles()
            ammo_events.tick_missile_count(missiles_snapshot, current_game_state)


            # GAME_WAITING: scan for CANCEL every 3s to confirm matchmaking.
            # CANCEL visible → matchmaking active → advance to GAME_STARTING.
            # CANCEL absent  → PLAY click missed → re-click PLAY.
            # 180s timeout   → give up, return to GAME_LOBBY.
            if waiting_fallback.tick(frame, current_game_state):
                continue

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
            ammo_events.deploy_flares_on_new_incoming()

            # Restart mission immediately when health transitions dead → alive.
            if analyzer.alive_event.is_set():
                respawn.handle_alive_transition()

            # Ammo events (GAME_BATTLE only).
            ammo_events.tick_events()

            # Legacy ENEMY_CLOSE_BY disengage — retired in active mode
            # (ADR 024 3.1b): the Disengage leaf owns the job there, firing on
            # minimap ring absence with the legacy fire-once-and-reset
            # semantics. Still ticks in off/shadow modes.
            if not behavior_tree.active:
                enemy_presence.tick(frame, current_game_state)

            # ADR 024 behavior tree: tactic selection every tick; in active
            # mode the Engage selection also drives ring-engage geometry
            # (Design 003, FR-005) — before fine tracking so the shared
            # orient_nose_to_target cooldown lets the terminal loop win.
            behavior_tree.tick(frame, current_game_state, game_state)

            tracking_hud.tick(frame, current_game_state, game_state)

            # Detect respawn — from overlay OCR, or (ADR 064 dual mode) from the
            # health detector's composite evidence when OCR missed the episode.
            if respawn.tick_detect(frame, game_state, current_game_state):
                time.sleep(1)
                continue

            # Respawn screen cleared. No scheduled restart: the health alive
            # transition restarts the mission the moment health returns — it
            # re-arms itself while respawn OCR is still flapping, so the
            # one-shot event cannot be lost.
            respawn.note_gameplay_resumed()

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
                    ammo_events.deploy_flares_on_new_incoming()
                    if analyzer.alive_event.is_set():
                        respawn.handle_alive_transition()
                    ammo_events.tick_events()
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
