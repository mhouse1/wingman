"""The operator's global hotkeys. CR-018-13.

These used to be registered at the end of ``Controller.__init__``, which made
building a Controller a global side effect: with a real keyboard backend it hooked
eleven keys and, on Linux, started the XRecord listener thread. Tests worked
around that by passing ``disable_hotkeys`` 71 times and by bypassing the
constructor with ``Controller.__new__``. Registration now happens once, in
``main``, through ``Controller.register_hotkeys()``.

The handlers moved verbatim; ``self`` became ``ctrl``. They are closures over
the controller, and most of them only call its methods.
"""

import logging
import threading
import time
from datetime import datetime
from pathlib import Path

import cv2

from . import capture_budget
from .crop_region import draw_crops
from .keybindings import (
    AUTO_MISSION_KEY,
    CANCEL_MISSION_KEY,
    CAPTURE_SCREEN_SHOT,
    FINISH_ROUND_THEN_EXIT,
    MISSION_J20_KEY,
    MISSION_LOITER_KEY,
    MISSION_SU30_KEY,
    NOSE_DOWN_KEY,
    NOSE_UP_KEY,
    PADLOCK_CAMERA,
    ROLL_LEFT_KEY,
    ROLL_RIGHT_KEY,
    SIMULATE_RESPAWN_KEY,
    TOGGLE_WEAPON_LOOP_KEY,
    _WATCHED_MANEUVER_KEYS,
)
from .state import BATTLE_STATES, GameState

logger = logging.getLogger(__name__)


def register_hotkeys(ctrl, keyboard_module) -> None:
    """Register every operator hotkey on ``keyboard_module`` for ``ctrl``.

    Does nothing when there is no keyboard backend or the controller was built
    with ``disable_hotkeys`` (replay and capture modes).
    """
    # Exit script hotkey (Backspace).
    # Honor disable_hotkeys so replay/capture automation is not interrupted by
    # ambient keyboard events from the host environment.
    # Probe keyboard access on the first registration; if ImportError (Linux not in
    # 'input' group), emit one warning and skip all remaining hotkeys.
    _kbd_ok = True
    if keyboard_module and not ctrl._disable_hotkeys:
        try:
            def exit_script_hotkey(_e):
                # Debounced: X auto-repeats a held key at ~25 Hz, and an
                # undebounced handler would read one long press as both
                # stages and close the game the operator meant to keep.
                now = time.time()
                if now - ctrl._last_exit_press < 0.5:
                    return
                ctrl._last_exit_press = now
                if ctrl._operator_stop_event.is_set():
                    # Second press, during standby.
                    ctrl._close_all_event.set()
                    logger.info("\033[93mController: Backspace again — closing "
                                "MetalStorm and the nested display\033[0m")
                    return
                ctrl._operator_stop_event.set()
                logger.info("\033[93mController: Backspace — ending wingman; "
                            "MetalStorm stays up for manual control. Press "
                            "Backspace again to close everything.\033[0m")
                if ctrl._exit_event:
                    ctrl._exit_event.set()
            # Kept on the controller so cleanup(keep_hotkeys=True) can re-register
            # just this one hotkey after tearing every other one down —
            # see the comment there for why.
            ctrl._exit_script_hotkey = exit_script_hotkey
            keyboard_module.on_press_key('backspace', exit_script_hotkey, suppress=False)
            logger.info("Controller: registered hotkey 'backspace' to exit script")
        except ImportError as e:
            logger.warning(
                "Controller: keyboard hotkeys disabled — %s  "
                "(fix: sudo usermod -aG input $USER then log out and back in)",
                e,
            )
            _kbd_ok = False
        except Exception:
            logger.exception("Controller: failed to register exit script hotkey")

    # Register hotkey for weapon loop toggle and other hotkeys
    if keyboard_module and not ctrl._disable_hotkeys and _kbd_ok:
        # Cancel mission hotkey (End)
        try:
            ctrl._last_cancel_key_ts = 0.0
            def cancel_mission_hotkey(_e):
                now = time.time()
                if now - ctrl._last_cancel_key_ts < 0.5:  # debounce: ignore key-repeat
                    return
                ctrl._last_cancel_key_ts = now
                logger.info("Controller: '%s' key pressed - cancelling mission and disabling auto-respawn restart", CANCEL_MISSION_KEY)
                ctrl._auto_respawn_restart = False
                ctrl._eject_stop_reason = "manual_cancel_key"
                ctrl._eject_stop.set()
                ctrl.cancel_mission()
            keyboard_module.on_press_key(CANCEL_MISSION_KEY, cancel_mission_hotkey, suppress=False)
            logger.info("Controller: registered hotkey '%s' to cancel mission", CANCEL_MISSION_KEY)
        except Exception:
            logger.exception("Controller: failed to register cancel mission hotkey")

        # Maneuver keys cancel mission when pressed during GAME_BATTLE (manual takeover)
        try:
            def maneuver_key_pressed(e):
                # getattr: the Windows `keyboard` fallback delivers real
                # KeyboardEvents, which carry no display or modifier state.
                ctrl._handle_maneuver_key_press(
                    key_name=getattr(e, 'name', str(e)),
                    is_injected=getattr(e, 'is_injected', False),
                    display=getattr(e, 'display', None),
                    state=getattr(e, 'state', 0),
                )
            for _key in _WATCHED_MANEUVER_KEYS:
                keyboard_module.on_press_key(_key, maneuver_key_pressed, suppress=False)
            logger.info(
                "Controller: registered maneuver keys (%s/%s/%s/%s) and arrow keys to cancel mission on manual press",
                NOSE_UP_KEY, NOSE_DOWN_KEY, ROLL_LEFT_KEY, ROLL_RIGHT_KEY,
            )
        except Exception:
            logger.exception("Controller: failed to register maneuver key hotkeys")
        try:
            keyboard_module.add_hotkey(TOGGLE_WEAPON_LOOP_KEY, ctrl.toggle_weapon_loop)
            logger.info("Controller: registered hotkey '%s' to toggle weapon loop", TOGGLE_WEAPON_LOOP_KEY)
        except Exception:
            logger.exception("Controller: failed to register weapon loop hotkey")

        try:
            ctrl._last_j20_key_ts = 0.0
            def start_j20_mission(_e):
                # Our own game_starting-loop presses echo back through
                # XRecord — recognize them by the programmatic bracket +
                # release grace, NOT by FSM state.
                with ctrl._programmatic_key_lock:
                    if (ctrl._programmatic_key_counts.get(MISSION_J20_KEY, 0) > 0
                            or time.time() < ctrl._prog_release_grace_until.get(
                                MISSION_J20_KEY, 0.0)):
                        logger.debug(
                            "Controller: '%s' key is wingman's own injected press (echo), ignoring",
                            MISSION_J20_KEY)
                        return
                now = time.time()
                if now - ctrl._last_j20_key_ts < 0.5:  # debounce: ignore key-repeat
                    return
                ctrl._last_j20_key_ts = now
                # 'u' skips rather than preempts a running mission, and the
                # skip must have no side effects: relabelling _last_mission
                # before the launch is refused would retag the mission that
                # is actually flying (su30's padlock block turns off, the
                # next respawn restarts the wrong mission) and reset the
                # 2 s takeover grace. A cancelled mission still unwinding
                # is not "flying" — that is the resume-from-manual case.
                if ctrl.is_mission_running() and not ctrl.is_mission_teardown_in_progress():
                    with ctrl._last_mission_lock:
                        flying = ctrl._last_mission
                    logger.info("Controller: '%s' key pressed - mission %s already "
                                "running, ignoring", MISSION_J20_KEY, flying)
                    return
                ctrl._auto_respawn_restart = True
                current_state = ctrl._analyzer.game_state if ctrl._analyzer is not None else None
                if current_state == GameState.GAME_BATTLE_MANUAL:
                    # Only force FSM back to GAME_BATTLE when resuming from manual takeover.
                    logger.info(
                        "Controller: '%s' key pressed — resuming auto mode from GAME_BATTLE_MANUAL",
                        MISSION_J20_KEY,
                    )
                    if not ctrl._analyzer.trigger_event("manual_force_battle"):
                        logger.warning("Controller: unable to force GAME_BATTLE via FSM trigger")
                else:
                    # NOTE: there is deliberately no GAME_STARTING special
                    # case anymore. Echoes of wingman's own presses are
                    # filtered by the programmatic bracket above; a genuine
                    # 'u' here is the player asking for the mission NOW
                    # (e.g. after taking over during the Good-Luck wait) and
                    # must work — the old state-based echo check ate those.
                    logger.info("Controller: '%s' key pressed - starting the configured mission "
                                "(%s, state=%s)",
                                MISSION_J20_KEY, ctrl._default_mission,
                                current_state.name if current_state is not None and hasattr(current_state, 'name') else current_state)
                    # Force FSM into GAME_BATTLE so lobby-only background loops (quick-scan
                    # stall-ESC, GAME_LOBBY escape loop) stop treating this as an idle lobby.
                    if ctrl._analyzer is not None and current_state != GameState.GAME_BATTLE:
                        if not ctrl._analyzer.trigger_event("manual_force_battle"):
                            logger.warning("Controller: unable to force GAME_BATTLE via FSM trigger")
                # ADR 145: 'u' starts whichever mission mission.default_mission
                # names — the same one battle entry launches — rather than J20
                # by name. The config picks the mission, so a new mission needs
                # no hotkey of its own. With the default set to j20 this is
                # exactly the old behaviour.
                ctrl._start_default_mission()
            keyboard_module.on_press_key(MISSION_J20_KEY, start_j20_mission, suppress=False)
            logger.info("Controller: registered hotkey '%s' to start the configured mission (%s)",
                        MISSION_J20_KEY, ctrl._default_mission)
        except Exception:
            logger.exception("Controller: failed to register configured-mission hotkey")

        try:
            def start_loiter_mission(_e):
                logger.info("Controller: '%s' key pressed - starting loiter mission", MISSION_LOITER_KEY)
                ctrl._set_last_mission("loiter")
                threading.Thread(target=ctrl.mission_loiter, daemon=True).start()
            keyboard_module.on_press_key(MISSION_LOITER_KEY, start_loiter_mission, suppress=False)
            logger.info("Controller: registered hotkey '%s' to start loiter mission", MISSION_LOITER_KEY)
        except Exception:
            logger.exception("Controller: failed to register loiter mission hotkey")

        try:
            ctrl._last_su30_key_ts = 0.0
            def start_su30_mission(_e):
                now = time.time()
                if now - ctrl._last_su30_key_ts < 0.5:  # debounce: ignore key-repeat
                    return
                ctrl._last_su30_key_ts = now
                current_state = ctrl._analyzer.game_state if ctrl._analyzer is not None else None
                logger.info("Controller: '%s' key pressed - starting SU-30 mission (state=%s)",
                            MISSION_SU30_KEY,
                            current_state.name if current_state is not None and hasattr(current_state, 'name') else current_state)
                # Same as the J20 hotkey: a press means "fly it now", so the
                # FSM is forced into GAME_BATTLE (which also resumes from
                # GAME_BATTLE_MANUAL) before the mission thread starts.
                if ctrl._analyzer is not None and current_state != GameState.GAME_BATTLE:
                    if not ctrl._analyzer.trigger_event("manual_force_battle"):
                        logger.warning("Controller: unable to force GAME_BATTLE via FSM trigger")
                ctrl._set_last_mission("su30")
                threading.Thread(target=ctrl.mission_su30, kwargs={"preempt": True},
                                 daemon=True).start()
            keyboard_module.on_press_key(MISSION_SU30_KEY, start_su30_mission, suppress=False)
            logger.info("Controller: registered hotkey '%s' to start SU-30 mission", MISSION_SU30_KEY)
        except Exception:
            logger.exception("Controller: failed to register SU-30 mission hotkey")

        # ADR 094: finish the round, then exit. Deferred, and reversible.
        try:
            ctrl._last_finish_round_press = 0.0
            def finish_round_then_exit(_e):
                now = time.time()
                if now - ctrl._last_finish_round_press < 0.5:
                    return                      # debounce key-repeat
                ctrl._last_finish_round_press = now
                if ctrl._finish_round_event.is_set():
                    # A deferred action that cannot be recalled is a trap:
                    # the operator waits minutes with no way back except
                    # killing the process (ADR 094).
                    ctrl._finish_round_event.clear()
                    logger.info("\033[93m🏁 FINISH ROUND: cancelled — the "
                                "session continues\033[0m")
                    return
                ctrl._finish_round_event.set()
                # Pressed in the lobby the stop is immediate: the main loop's
                # safe point is already true, and the quick-scan is now barred
                # from starting another round. Say which one is happening -
                # "at the next lobby" while sitting IN the lobby reads as a
                # long wait and invites a second press that cancels it.
                _st = ctrl._analyzer.game_state if ctrl._analyzer is not None else None
                if _st is not None and _st not in BATTLE_STATES:
                    logger.info("\033[93m🏁 FINISH ROUND: requested in %s — no "
                                "round in progress, stopping now and closing "
                                "MetalStorm (ADR 094). Press '%s' again to "
                                "cancel.\033[0m", _st.name, FINISH_ROUND_THEN_EXIT)
                else:
                    logger.info("\033[93m🏁 FINISH ROUND: requested — wingman will "
                                "stop at the next lobby, then close MetalStorm "
                                "(ADR 094). Press '%s' again to cancel.\033[0m",
                                FINISH_ROUND_THEN_EXIT)
            keyboard_module.on_press_key(FINISH_ROUND_THEN_EXIT,
                                         finish_round_then_exit, suppress=False)
            logger.info("Controller: registered hotkey '%s' to finish the round "
                        "then exit", FINISH_ROUND_THEN_EXIT)
        except Exception:
            logger.exception("Controller: failed to register finish-round hotkey")

        # Register hotkey for simulating respawn detected (for testing)
        try:
            ctrl._simulate_respawn_flag = threading.Event()
            ctrl._last_b_press_time = 0.0
            def simulate_respawn(_e):
                now = time.time()
                if now - ctrl._last_b_press_time < 0.5:  # debounce: ignore key-repeat
                    return
                ctrl._last_b_press_time = now
                logger.info("Controller: '%s' key pressed - simulating respawn detected (as if OCR detected 'RESPAWN')", SIMULATE_RESPAWN_KEY)
                if ctrl._analyzer is not None:
                    ctrl._analyzer.inject_respawn_ocr_result(True, 1.0, "ocr")
                    logger.info("Controller: Injected fake OCR respawn result into analyzer cache.")
                else:
                    logger.warning("Controller: No analyzer reference to inject fake OCR respawn result.")
                ctrl._simulate_respawn_flag.set()
            keyboard_module.on_press_key(SIMULATE_RESPAWN_KEY, simulate_respawn, suppress=False)
            logger.info("Controller: registered hotkey '%s' to simulate respawn detected", SIMULATE_RESPAWN_KEY)
        except Exception:
            logger.exception("Controller: failed to register simulate respawn hotkey")

        # Register hotkey for capturing screenshots (for testing/debugging)
        try:
            def capture_screenshot(e):
                logger.info("Controller: '%s' key pressed - capturing screenshot", CAPTURE_SCREEN_SHOT)
                if ctrl._capture is not None and ctrl._analyzer is not None:
                    try:
                        frame = ctrl._capture.grab_from_thread()

                        # Create output directory if it doesn't exist
                        output_dir = Path("tests/test-output")
                        # Only screenshot_*.png: this folder is shared
                        # with live_hud.png, output_grid.png and reports.
                        if not capture_budget.admit(output_dir, "Screenshot hotkey",
                                                    patterns="screenshot_*.png"):
                            return
                        output_dir.mkdir(parents=True, exist_ok=True)

                        # Generate timestamp filename
                        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                        filename = output_dir / f"screenshot_{timestamp}.png"

                        if ctrl._capture_with_overlay:
                            # Draw only state-relevant crop overlays when enabled.
                            crops = ctrl._analyzer.crops_for_state()
                            frame = draw_crops(frame, crops)
                            logger.info("Controller: Screenshot saved to %s with crop overlays", filename)
                        else:
                            logger.info("Controller: Screenshot saved to %s without overlays", filename)

                        cv2.imwrite(str(filename), frame)
                    except Exception as e:
                        logger.exception("Controller: Failed to capture screenshot: %s", e)
                else:
                    logger.warning("Controller: No capture or analyzer reference to take screenshot.")
            keyboard_module.on_press_key(CAPTURE_SCREEN_SHOT, capture_screenshot, suppress=False)
            logger.info("Controller: registered hotkey '%s' to capture screenshot", CAPTURE_SCREEN_SHOT)
        except Exception:
            logger.exception("Controller: failed to register capture screenshot hotkey")

        # Padlock camera cooldown hotkey: when P is pressed manually, suppress
        # the padlock loop for 10 seconds so it doesn't immediately re-lock.
        try:
            def padlock_key_pressed(_e):
                # Only a *manual* press should suppress the loop. Without this
                # guard the loop's own padlock_camera() presses echo back through
                # this hook and set the 10s cooldown on every tick, halving the
                # effective cadence from 6s to ~12s (observed 2026-07-30).
                with ctrl._programmatic_key_lock:
                    if ctrl._programmatic_key_counts.get(PADLOCK_CAMERA, 0) > 0:
                        return
                    if time.time() < ctrl._prog_release_grace_until.get(PADLOCK_CAMERA, 0.0):
                        return
                cooldown = 10.0
                ctrl._padlock_cooldown_until = time.time() + cooldown
                # ADR 140 D3: a genuine manual press, outside padlock_camera()'s
                # own call graph entirely — still flips the real toggle, so
                # confidence in the last-known state is lost here too.
                ctrl._padlock_engaged = None
                logger.info("Controller: '%s' key pressed manually - padlock loop cooldown set for %.0fs", PADLOCK_CAMERA, cooldown)
            keyboard_module.on_press_key(PADLOCK_CAMERA, padlock_key_pressed, suppress=False)
            logger.info("Controller: registered hotkey '%s' to set padlock loop cooldown", PADLOCK_CAMERA)
        except Exception:
            logger.exception("Controller: failed to register padlock camera cooldown hotkey")

        # Auto-mission hotkey: force GAME_LOBBY state, then click PLAY/READY
        try:
            keyboard_module.on_press_key(AUTO_MISSION_KEY, ctrl._on_auto_mission_hotkey, suppress=False)
            logger.info("Controller: registered hotkey '%s' to click PLAY/READY in GAME_LOBBY", AUTO_MISSION_KEY)
        except Exception:
            logger.exception("Controller: failed to register auto mission hotkey")
