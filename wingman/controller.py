import contextlib
import ctypes
import logging
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

import cv2
from mss import mss

from .analyzer import GameState, BATTLE_STATES
from .controller_config import ControllerConfig
from .crop_region import CropCoords, crop_centre, draw_crops
from .input_linux import (  # noqa: F401  — re-exported: conftest.py, move_game_window.py and tests import these from here
    _WINGMAN_XAUTH,
    _XKEY_ALIASES,
    _ensure_xauthority,
    _linux_click,
    _linux_key_event,
    _LinuxXTestKeyboard,
    _XKeyEvent,
    maybe_install_linux_keyboard,
)

try:
    import keyboard as keyboard_module
except Exception:
    keyboard_module = None

logger = logging.getLogger(__name__)

# Module-level so tests can monkeypatch `controller.keyboard_module` in one place.
keyboard_module = maybe_install_linux_keyboard(keyboard_module)

# ADR 098: the focus guard. Set by Controller.__init__ from config; None means
# no guard (every injection allowed), which is also the default.
focus_guard = None


def set_focus_guard(guard) -> None:
    """Install the process-wide focus guard (ADR 098)."""
    global focus_guard
    focus_guard = guard


def _press_key(key) -> bool:
    """Inject a key press unless the focus guard forbids it (ADR 098).

    Returns True if the key was actually pressed. Callers keep their hold loop,
    their release, and their finally blocks either way: gating the control flow
    instead collapses a two-second hold into zero and, at the disengage-roll
    site, skipped the stop_search_and_destroy_loop() cleanup that a started loop
    depends on. Releasing a key that was never pressed is a harmless no-op.
    """
    if not _may_inject("key"):
        return False
    keyboard_module.press(key)
    return True


def _may_inject(what: str = "key") -> bool:
    """Gate for every injection call site. Never raises.

    Gating happens HERE rather than at the 17 individual press sites: one place
    cannot be forgotten when a new tactic adds a keypress. Observation paths
    (on_press_key, add_hotkey, XRecord) are deliberately NOT gated — they read
    the operator's own keys and must keep working while focus is elsewhere,
    which is exactly when the operator is most likely to want the exit hotkey.
    """
    guard = focus_guard
    if guard is None:
        return True
    try:
        return guard.may_inject(what)
    except Exception:                        # noqa: BLE001 - never break the loop
        return True

# A failed key RELEASE is the start of a stuck-key incident, and the key does not
# come back when this process dies: on Linux XTest key state lives in the X
# SERVER and survives for the whole session; on Windows the injected key stays
# down in the OS input queue until something releases it. The consequence is the
# same on both — uncommanded flight input the operator cannot clear — so the
# ERROR is unconditional and only the mechanism named in it is platform-specific.
_LATCH_NOTE = ("key may stay latched in the X server for this session"
               if sys.platform != "win32"
               else "key may stay held down in the Windows input queue")


# Key bindings and the emote list live in keybindings.py so they are findable
# without reading the controller. Re-exported here because callers and tests
# import them from this module.
from .keybindings import (                                          # noqa: F401
    AFTERBURNER_KEY,
    AIRBRAKE_KEY,
    ALT_FLIGHT_KEYS,
    MANUAL_TAKEOVER_KEY,
    AUTO_MISSION_KEY,
    CANCEL_MISSION_KEY,
    CAPTURE_SCREEN_SHOT,
    DEPLOY_FLARES_KEY,
    EMOTES,
    FINISH_ROUND_THEN_EXIT,
    FIRE_ACTIVE_WEAPON,
    FIRE_MACHINE_GUN,
    MISSION_J20_KEY,
    MISSION_LOITER_KEY,
    NOSE_DOWN_KEY,
    NOSE_UP_KEY,
    PADLOCK_CAMERA,
    ROLL_LEFT_KEY,
    ROLL_RIGHT_KEY,
    SIMULATE_RESPAWN_KEY,
    SPECIAL_ABILITY,
    SWITCH_WEAPON,
    TOGGLE_WEAPON_LOOP_KEY,
    WINGSWEEP_KEY,
    YAW_LEFT,
    _WATCHED_MANEUVER_KEYS,
)

# SAF-001: the manual-takeover keys. These must always reach the hotkey handler,
# including on the injection display where wingman presses them itself —
# SAF-001.1's echo discrimination, not a display filter, decides whether a press
# was wingman's own.
TAKEOVER_KEYS = ((MANUAL_TAKEOVER_KEY, NOSE_UP_KEY, NOSE_DOWN_KEY,
                  ROLL_LEFT_KEY, ROLL_RIGHT_KEY) + tuple(ALT_FLIGHT_KEYS))

# SAF-007: every key wingman injects anywhere must be in this list, or it is
# left pressed when the process dies. ADR 099 gives it a second job — these are
# the keys the hotkey listener must IGNORE on the injection display, because
# there they are wingman's own keystrokes rather than the operator's.
INJECTABLE_KEYS = (
    NOSE_UP_KEY, NOSE_DOWN_KEY, ROLL_LEFT_KEY, ROLL_RIGHT_KEY,
    YAW_LEFT, AFTERBURNER_KEY, AIRBRAKE_KEY, WINGSWEEP_KEY,
    DEPLOY_FLARES_KEY, FIRE_MACHINE_GUN, FIRE_ACTIVE_WEAPON,
    PADLOCK_CAMERA, SPECIAL_ABILITY, MISSION_J20_KEY,
    'escape',
)

# Region name constants — used as log labels in click_grid_region and elsewhere.
# Defining them as constants means the string is written once; a rename is a
# single-line change here rather than a grep-and-replace across the codebase.
REGION_GOOD_LUCK         = "good_luck"
REGION_EVENT_REFRESH     = "event_refresh"
REGION_PLAY_BUTTON       = "PLAY"
REGION_CLICK_TO_CONTINUE = "click_to_continue"
REGION_REVEAL_ALL        = "REVEAL_ALL"
REGION_TAP_HERE          = "TAP_HERE_TO_CONTINUE"
REGION_UNLOCK_CLOSE      = "UNLOCK_CLOSE"
REGION_FINAL_CONTINUE    = "FINAL_CONTINUE"

class Controller:
    def __init__(
        self,
        region,
        *,
        config: "ControllerConfig | None" = None,
        analyzer=None,
        capture=None,
        exit_event=None,
        on_auto_mission_key=None,
        crops: "dict[str, CropCoords] | None" = None,
    ):
        """Wire the controller.

        Collaborators are explicit arguments; every tuned value lives in
        `config` (Future 002 A-02 — this signature was 21 parameters, four of
        them raw `*_cfg` dicts appended one per feature).

        The config fields are unpacked to locals below under their historical
        names. That is deliberate: it confines this change to the signature and
        keeps the ~460-line body — and the behaviour the gates cover — byte
        identical, so the parameter-object change cannot hide a semantic one.
        """
        self.config = config if config is not None else ControllerConfig()
        _c = self.config
        fire_button = _c.fire_button
        fire_hold_seconds = _c.fire_hold_seconds
        weapon_loop_interval = _c.weapon_loop_interval
        target_painting_mode = _c.target_painting_mode
        simulate_os_input = _c.simulate_os_input
        disable_hotkeys = _c.disable_hotkeys
        capture_with_overlay = _c.capture_with_overlay
        starting_max_wait_s = _c.starting_max_wait_s
        good_luck_wait_s = _c.good_luck_wait_s
        good_luck_bypass_on_alive = _c.good_luck_bypass_on_alive
        capture_stale_inject_s = _c.capture_stale_inject_s
        telemetry_cfg = _c.telemetry
        missile_evade_cfg = _c.missile_evade
        climb_cfg = _c.climb
        fuel_cfg = _c.fuel

        # region is (left, top, width, height)
        self.region = region
        self.fire_button = fire_button
        self.fire_hold_seconds = float(fire_hold_seconds or 0.0)
        self._firing_lock = threading.Lock()
        self._mission_lock = threading.Lock()
        self._mission_complete = threading.Event()
        self._mission_cancel = threading.Event()
        self._exit_event = exit_event  # Event to signal program exit
        # ADR 094: deferred exit. Set by the FINISH_ROUND_THEN_EXIT hotkey and
        # read by the main loop at its safe point. An Event rather than a bool
        # because the hotkey toggles it from the listener thread.
        self._finish_round_event = threading.Event()
        # ADR 099: set only by the Backspace hotkey. exit_requested cannot stand
        # in for this — SIGTERM and the startup-stall exit set that too, and the
        # stall path deliberately leaves the game up for inspection.
        self._operator_stop_event = threading.Event()
        # ADR 099: second Backspace, pressed during standby, closes MetalStorm
        # and the nested display. Separate from the stop event so the first
        # press cannot be mistaken for the second.
        self._close_all_event = threading.Event()
        self._last_exit_press = 0.0
        self._last_mission = None
        self._last_mission_lock = threading.Lock()
        self._analyzer = analyzer
        self._capture = capture
        self._on_auto_mission_key = on_auto_mission_key
        self._last_auto_mission_key_ts = 0.0
        # Battle-state guard arm timestamp for the double-press force (see
        # _on_auto_mission_hotkey).
        self._auto_mission_force_armed_ts = 0.0
        self._crops: "dict[str, CropCoords]" = crops or {}
        self._auto_respawn_restart = True  # cleared by manual End press; restored when a mission starts
        self._game_battle_since = 0.0  # timestamp of last GAME_BATTLE entry; used by grace period guard
        self._ready_button_region = 0  # grid region number for the ready-button click; 0 = not configured
        self._popup_last_clicked: "dict[str, float]" = {}  # popup name → timestamp of last click

        # Padlock camera cooldown: set when the key is pressed manually
        self._padlock_cooldown_until = 0.0

        # Target tracking: timestamp of last orient_nose_to_target command
        self._last_orient_ts: float = 0.0

        # Weapon loop state (configurable via config or start_weapon_loop)
        self._weapon_loop_active = False
        self._weapon_loop_thread = None
        self._weapon_loop_stop = threading.Event()
        self._weapon_loop_interval = float(weapon_loop_interval or 0.5)  # Firing interval from config or default
        self._starting_max_wait_s = float(starting_max_wait_s)
        # Suppress key injection when the last good frame is older than this:
        # with capture stalled (display loss, KVM switch) the game may no longer
        # be on screen, so presses land in whatever window is focused.
        self._capture_stale_inject_s = float(capture_stale_inject_s)
        # Post-"Good Luck" settle before launching the mission. Interruptible:
        # a battle-alive signal ends it early (2026-08-05).
        self._good_luck_wait_s = float(good_luck_wait_s)
        self._good_luck_bypass_on_alive = bool(good_luck_bypass_on_alive)

        # Search-and-destroy loop state (padlock + weapon fire; used during disengage)
        self._sdl_stop: threading.Event | None = None
        self._sdl_padlock_thread: threading.Thread | None = None
        self._sdl_weapon_thread: threading.Thread | None = None
        self._target_painting_mode = target_painting_mode
        self._simulate_os_input = bool(simulate_os_input)
        self._disable_hotkeys = bool(disable_hotkeys)
        self._capture_with_overlay = bool(capture_with_overlay)
        self._action_intents: list[dict] = []
        self._action_intents_lock = threading.Lock()

        # Eject-and-dive cancellation: set by End key to abort the dive thread early
        self._eject_stop = threading.Event()
        # Why _eject_stop was set — logged with the cancellation so the ADR044/045
        # validators can tell a respawn-triggered stop (success) from a manual one.
        self._eject_stop_reason: str = ""
        # Set while eject_and_dive thread is running; cleared by the thread's finally block.
        self._ejecting = threading.Event()
        # Handle to the current eject thread so cleanup() can join it briefly
        # and let its finally block release keys before the process exits.
        self._eject_thread: "threading.Thread | None" = None
        # Handle to the current disengage_roll_right maneuver thread
        # (ADR 024 3.1b — liveness for the Disengage leaf).
        self._disengage_thread: "threading.Thread | None" = None
        # ADR 070: set SYNCHRONOUSLY by missile_evade_mode() before the thread
        # spawns (d8 — the duplicate-start guard is a design property, closed
        # in the caller's thread before any concurrency exists), cleared by the
        # thread's finally block.
        self._missile_evading = threading.Event()
        self._me_thread: "threading.Thread | None" = None
        # Stop event for the evade hold loop — set in cleanup() so shutdown
        # releases the three keys via the thread's own finally (repo rule:
        # stoppable daemon threads).
        self._me_stop = threading.Event()
        # Tracks which eject-sequence keys are currently physically held, so
        # _eject_key() calls are idempotent (a cleanup release of an
        # already-released key is a no-op) and _programmatic_key_counts stays
        # balanced regardless of which exit path a given eject run takes.
        self._eject_held_keys: set = set()

        # ADR 038 closed-loop eject verification parameters (telemetry: block).
        _tel_cfg = telemetry_cfg or {}
        _ecl = _tel_cfg.get("eject_closed_loop", {}) or {}
        self._eject_cl_enabled = bool(_ecl.get("enabled", True))
        self._eject_cl_check_interval_s = float(_ecl.get("check_interval_s", 1.5))
        self._eject_nose_hold_s = float(_ecl.get("legacy_nose_hold_s", 5.0))
        self._eject_cl_confirm_consecutive = max(1, int(_ecl.get("confirm_consecutive", 2)))
        # ADR 069: the descent CRITERION is the raw altitude rate — speed-free,
        # so it cannot be corrupted by the smoothing lag that saturated the
        # angle metric (d1). Thresholds from 624 archived eject windows.
        self._eject_cl_descent_target_mps = float(_ecl.get("descent_target_mps", 100.0))
        self._eject_cl_descent_floor_mps = float(_ecl.get("descent_floor_mps", 50.0))
        # ADR 069 d2: rotation is a bounded impulse followed by a mandatory
        # observation gap — the controller never holds the key while waiting to
        # see what the last input did.
        self._eject_cl_rotation_pulse_s = float(_ecl.get("rotation_pulse_s", 2.0))
        self._eject_cl_observe_after_pulse_s = float(
            _ecl.get("observe_after_pulse_s", 3.5))
        # ADR 069 d5: actuation budget counted in PULSES, plus a wall-clock
        # backstop for the whole sequence. Held seconds stop being a meaningful
        # quantity once the key is only ever pulsed.
        self._eject_cl_max_rotation_pulses = int(_ecl.get("max_rotation_pulses", 4))
        self._eject_cl_max_s = float(_ecl.get("eject_max_s", 120.0))
        # ADR 058 d12 / ADR 068 d1 (carried forward): continuous nose-down hold
        # after which a CLIMB is read as over-rotation. 0 disables.
        self._eject_cl_over_rotation_after_s = float(_ecl.get("over_rotation_after_s", 6.0))
        # ADR 069 d1 (revised): the flight-path angle IS the criterion — the
        # dive must reach the target, and rotation resumes once it sags past
        # the floor. The band between them is the anti-flapping deadband.
        self._eject_cl_target_dive_angle_deg = float(
            _ecl.get("target_dive_angle_deg", 75.0))
        self._eject_cl_dive_angle_floor_deg = float(
            _ecl.get("dive_angle_floor_deg", 60.0))
        # ADR 068 d1: True once ANY descending sample has been seen during the
        # CURRENT rotation attempt. The over-rotation guard requires it —
        # rotating past vertical means passing THROUGH a dive, so a flight path
        # that has only ever climbed is under-rotated, not over-rotated.
        self._eject_descended_since_press = False
        self._eject_tel_stale_after_s = float(_tel_cfg.get("stale_after_s", 6.0))
        # True while AFTERBURNER is deliberately engaged by the descent
        # controller (ADR 069 d8 — burner is gated on descending flight).
        self._eject_ab_engaged = False
        # Cumulative time NOSE_DOWN has actually been held during the current
        # eject, used only by the over-rotation guard's "held long enough to
        # have over-rotated" test. None means "not inside an eject".
        self._eject_nose_held_total_s: "float | None" = None
        # Timestamp NOSE_DOWN was most recently pressed, or None when it is up.
        self._eject_nose_down_since: "float | None" = None
        # Why the descent controller returned: established / rate_target /
        # pulses_exhausted / over_rotation / no_telemetry / timeout / cancelled.
        self._eject_phase_exit_reason: str = ""
        self._eject_steep_min_sin = float(_tel_cfg.get("steep_dive_min_sin", 0.8))
        self._eject_level_max_sin = float(_tel_cfg.get("level_max_sin", 0.15))

        # ADR 070 d10: MISSILE_EVADE_MODE tuning, constructor-injected from the
        # behavior_tree.missile_evade config block (the Controller takes no
        # config dict, and the ADR 024 actuator contract calls start_fn with no
        # arguments — so the values can only arrive here). `enabled` is read by
        # BehaviorTreeHandler, not by us.
        _me_cfg = missile_evade_cfg or {}
        self._me_clear_s = float(_me_cfg.get("clear_seconds", 3.0))
        self._me_min_clear_samples = int(_me_cfg.get("min_clear_samples", 2))
        self._me_max_hold_s = float(_me_cfg.get("max_hold_s", 15.0))
        # ADR 070 d12: the TACTICAL limit — a normal exit, distinct from the
        # max_hold_s fault backstop above. Beyond ~5 s the manoeuvre is only
        # bleeding energy (2026-08-12 evidence: a 14 s hold traded 620 KPH for
        # altitude and ended slow, high and nearly level).
        self._me_max_manoeuvre_s = float(_me_cfg.get("max_manoeuvre_s", 6.0))
        # ADR 070 d13: optional NOSE_DOWN in the hold, making the manoeuvre a
        # descending break instead of the zoom climb the base triple produces.
        # Off by default — it is the unproven variant, not the shipped one.
        self._me_pitch_down = bool(_me_cfg.get("pitch_down", False))

        # ADR 073 Phase 3.2b — CLIMB tactic hold (NOSE_UP + AFTERBURNER).
        # The mission-start prologue climb (3.2c) is retired: the ADR 075
        # sustain band selects Climb from the tree whenever the armed aircraft
        # is below operating altitude, so mission_j20 no longer climbs itself.
        _cl_cfg = climb_cfg or {}
        self.climb_tactic_enabled = bool(_cl_cfg.get("enabled", False))
        self._climb_exit_alt = _cl_cfg.get("exit_above_alt")
        self._climb_confirm_reads = max(1, int(_cl_cfg.get("confirm_reads", 2)))
        self._climb_max_s = float(_cl_cfg.get("max_climb_s", 15.0))
        # ADR 075: afterburner fuel discipline. The game recharges fuel only
        # while the key is UP; at 0% the burner is off and a held key blocks
        # the recharge, so every burner hold releases at its floor and re-arms
        # only after the margin refills.
        _fuel_cfg = fuel_cfg or {}
        self._fuel_rearm_margin = float(_fuel_cfg.get("rearm_margin_pct", 5.0))
        # Pulse-and-observe pitch control (3.2c live finding 2026-08-15
        # 20:24: 60 s of HELD nose-up looped the aircraft, alt oscillating
        # 1650-2400 with zero net gain). NOSE_UP is applied in bounded pulses
        # and re-applied only when the telemetry climb RATE decays — the
        # eject dive controller's pattern, inverted. AFTERBURNER stays held.
        self._climb_pulse_s = float(_cl_cfg.get("pitch_pulse_s", 1.5))
        self._climb_observe_s = float(_cl_cfg.get("pulse_observe_s", 2.5))
        self._climb_min_rate = float(_cl_cfg.get("min_climb_rate", 30.0))
        # ADR 076 d3: over-rotation ceiling. The spawn guard can hand the
        # climb an aircraft that is ALREADY pitching up, so the pulse
        # controller must be able to rotate back down, not just decline to
        # add more nose-up. None disables (unset in config).
        self._climb_max_rate = _cl_cfg.get("max_climb_rate")
        # ADR 081 d1: pitch ceiling. Near vertical the climb RATE decays
        # (speed bleeds), so the rate floor pulses more nose-up exactly when
        # over-rotated — the angle is the direct variable and outranks the
        # rate logic. ~10° of forward margin below vertical keeps a forward
        # velocity component (no trading through vertical into reversed
        # flight). None disables.
        self._climb_max_pitch_deg = _cl_cfg.get("max_pitch_deg")
        # ADR 086 d1 / SAF-010: the climb must hand the airframe back inside a
        # flyable pitch band. Releasing NOSE_UP, NOSE_DOWN and AFTERBURNER
        # together leaves it ballistic at the exit attitude — measured
        # 2026-08-21, a climb ending at +73 deg coasted 1500 m further, stalled
        # at 24 KPH and hit the ground with missiles still racked. None
        # disables the push (pre-ADR-086 behaviour).
        self._climb_pitch_lead_s = float(_cl_cfg.get("pitch_lead_s", 3.0))
        # ADR 088: abort a dive whose premise (empty rack) has expired.
        self._eject_abort_on_rearm = bool(_ecl.get("abort_on_rearm", True))
        self._climb_exit_pitch_deg = _cl_cfg.get("exit_pitch_deg")
        self._climb_exit_pulse_s = float(_cl_cfg.get("exit_push_pulse_s", 1.0))
        self._climb_exit_max_pulses = int(_cl_cfg.get("exit_push_max_pulses", 3))
        self._climbing = threading.Event()
        self._climb_thread: "threading.Thread | None" = None
        self._climb_stop = threading.Event()
        # ADR 076 d1/d2 — spawn-attitude guard: hold NOSE_UP from death
        # detection through the respawn screen so the aircraft's first frames
        # of life are already pitching up (spawn-into-terrain anomaly).
        _sg_cfg = _cl_cfg.get("spawn_guard", {}) or {}
        self._spawn_guard_enabled = bool(_sg_cfg.get("enabled", False))
        self._sg_max_hold_s = float(_sg_cfg.get("max_hold_s", 90.0))
        self._sg_release_overlap_s = float(_sg_cfg.get("release_overlap_s", 2.5))
        # ADR 078: pulsed application — a continuous hold looped the live
        # aircraft at spawn (2026-08-17 14:22, 180-and-out-of-map).
        self._sg_pulse_s = float(_sg_cfg.get("pulse_s", 1.5))
        self._sg_observe_s = float(_sg_cfg.get("observe_s", 1.0))
        self._spawn_guarding = threading.Event()
        self._sg_stop = threading.Event()
        self._sg_thread: "threading.Thread | None" = None
        # Stamped by notify_spawn_alive(); read by the guard thread (float
        # store is atomic under the GIL, same pattern as the cooldown stamps).
        self._sg_alive_deadline: "float | None" = None

        # Tracks how many programmatic presses are in flight, per key.
        # keyboard.KeyboardEvent has no is_injected attribute, so the getattr guard
        # in the maneuver hooks always falls back to False and cannot distinguish
        # machine-generated from human-generated key events. Incrementing a key's
        # count before keyboard.press() and decrementing after keyboard.release()
        # lets the hooks skip cancel logic for keys the mission pressed itself --
        # keyed per-key (not one global count) so wingman holding NOSE_DOWN during
        # an eject correction doesn't also swallow a player pressing ROLL_RIGHT to
        # manually take over (observed in production: the player's manual-takeover
        # keys could be silently ignored for the whole multi-second nose-down hold,
        # exactly the moments they'd most want to grab control).
        self._programmatic_key_counts: dict = {}
        self._programmatic_key_lock = threading.Lock()
        # Per-key deadline until which a maneuver-key press is still treated as
        # ours. Covers auto-repeat KeyPress events emitted while we held the key
        # but delivered by the XRecord listener AFTER we released it
        # (see _eject_key / _arm_release_grace).
        #
        # 1.0 s, not "a few ms": XRecord delivery lag scales with X-server load,
        # not with our release timing. In the 2026-08-14 02:35 soak session a
        # wingman roll_left release took 1.1 s to execute and a queued 'j'
        # repeat was delivered 391 ms AFTER the release — past the old 0.15 s
        # window — cancelling the mission into GAME_BATTLE_MANUAL mid-flight
        # with auto-restart suppressed. The cost is bounded and per-key: only
        # THE key wingman itself just released is deaf for 1 s; a genuine
        # takeover still lands via any other maneuver key, repeated presses
        # (the observed human pattern is 3+ taps over ~1.3 s), or the same key
        # after 1 s. SAF-001's 2.0 s cessation bound is still met.
        self._prog_release_grace_until: dict = {}
        self._prog_release_grace_s = 1.0

        # Optional callback fired immediately when Good Luck OCR succeeds, with the
        # captured frame.  Used by live capture mode to record the fixture at the
        # moment of detection rather than 13s later when the FSM trigger fires.
        self._on_good_luck_frame = None

        # Optional callback fired immediately when the player presses a maneuver key
        # to trigger manual takeover (GAME_BATTLE → GAME_BATTLE_MANUAL).  The frame
        # is captured BEFORE the FSM transition so the screenshot still shows the
        # GAME_BATTLE HUD — used by live capture mode for P2_020.
        self._on_manual_takeover_frame = None

        # Exit script hotkey (Backspace).
        # Honor disable_hotkeys so replay/capture automation is not interrupted by
        # ambient keyboard events from the host environment.
        # Probe keyboard access on the first registration; if ImportError (Linux not in
        # 'input' group), emit one warning and skip all remaining hotkeys.
        _kbd_ok = True
        if keyboard_module and not self._disable_hotkeys:
            try:
                def exit_script_hotkey(_e):
                    # Debounced: X auto-repeats a held key at ~25 Hz, and an
                    # undebounced handler would read one long press as both
                    # stages and close the game the operator meant to keep.
                    now = time.time()
                    if now - self._last_exit_press < 0.5:
                        return
                    self._last_exit_press = now
                    if self._operator_stop_event.is_set():
                        # Second press, during standby.
                        self._close_all_event.set()
                        logger.info("\033[93mController: Backspace again — closing "
                                    "MetalStorm and the nested display\033[0m")
                        return
                    self._operator_stop_event.set()
                    logger.info("\033[93mController: Backspace — ending wingman; "
                                "MetalStorm stays up for manual control. Press "
                                "Backspace again to close everything.\033[0m")
                    if self._exit_event:
                        self._exit_event.set()
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
        if keyboard_module and not self._disable_hotkeys and _kbd_ok:
            # Cancel mission hotkey (End)
            try:
                self._last_cancel_key_ts = 0.0
                def cancel_mission_hotkey(_e):
                    now = time.time()
                    if now - self._last_cancel_key_ts < 0.5:  # debounce: ignore key-repeat
                        return
                    self._last_cancel_key_ts = now
                    logger.info("Controller: '%s' key pressed - cancelling mission and disabling auto-respawn restart", CANCEL_MISSION_KEY)
                    self._auto_respawn_restart = False
                    self._eject_stop_reason = "manual_cancel_key"
                    self._eject_stop.set()
                    self.cancel_mission()
                keyboard_module.on_press_key(CANCEL_MISSION_KEY, cancel_mission_hotkey, suppress=False)
                logger.info("Controller: registered hotkey '%s' to cancel mission", CANCEL_MISSION_KEY)
            except Exception:
                logger.exception("Controller: failed to register cancel mission hotkey")

            # Maneuver keys cancel mission when pressed during GAME_BATTLE (manual takeover)
            try:
                def maneuver_key_pressed(e):
                    self._handle_maneuver_key_press(
                        key_name=getattr(e, 'name', str(e)),
                        is_injected=getattr(e, 'is_injected', False),
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
                keyboard_module.add_hotkey(TOGGLE_WEAPON_LOOP_KEY, self.toggle_weapon_loop)
                logger.info("Controller: registered hotkey '%s' to toggle weapon loop", TOGGLE_WEAPON_LOOP_KEY)
            except Exception:
                logger.exception("Controller: failed to register weapon loop hotkey")

            try:
                self._last_j20_key_ts = 0.0
                def start_j20_mission(_e):
                    # Our own game_starting-loop presses echo back through
                    # XRecord — recognize them by the programmatic bracket +
                    # release grace, NOT by FSM state.
                    with self._programmatic_key_lock:
                        if (self._programmatic_key_counts.get(MISSION_J20_KEY, 0) > 0
                                or time.time() < self._prog_release_grace_until.get(
                                    MISSION_J20_KEY, 0.0)):
                            logger.debug(
                                "Controller: '%s' key is wingman's own injected press (echo), ignoring",
                                MISSION_J20_KEY)
                            return
                    now = time.time()
                    if now - self._last_j20_key_ts < 0.5:  # debounce: ignore key-repeat
                        return
                    self._last_j20_key_ts = now
                    self._auto_respawn_restart = True
                    current_state = self._analyzer.game_state if self._analyzer is not None else None
                    if current_state == GameState.GAME_BATTLE_MANUAL:
                        # Only force FSM back to GAME_BATTLE when resuming from manual takeover.
                        logger.info(
                            "Controller: '%s' key pressed — resuming auto mode from GAME_BATTLE_MANUAL",
                            MISSION_J20_KEY,
                        )
                        if not self._analyzer.trigger_event("manual_force_battle"):
                            logger.warning("Controller: unable to force GAME_BATTLE via FSM trigger")
                    else:
                        # NOTE: there is deliberately no GAME_STARTING special
                        # case anymore. Echoes of wingman's own presses are
                        # filtered by the programmatic bracket above; a genuine
                        # 'u' here is the player asking for the mission NOW
                        # (e.g. after taking over during the Good-Luck wait) and
                        # must work — the old state-based echo check ate those.
                        logger.info("Controller: '%s' key pressed - starting J20 mission (state=%s)",
                                    MISSION_J20_KEY,
                                    current_state.name if current_state is not None and hasattr(current_state, 'name') else current_state)
                        # Force FSM into GAME_BATTLE so lobby-only background loops (quick-scan
                        # stall-ESC, GAME_LOBBY escape loop) stop treating this as an idle lobby.
                        if self._analyzer is not None and current_state != GameState.GAME_BATTLE:
                            if not self._analyzer.trigger_event("manual_force_battle"):
                                logger.warning("Controller: unable to force GAME_BATTLE via FSM trigger")
                    self._set_last_mission("j20")
                    threading.Thread(target=self.mission_j20, daemon=True).start()
                keyboard_module.on_press_key(MISSION_J20_KEY, start_j20_mission, suppress=False)
                logger.info("Controller: registered hotkey '%s' to start J20 mission", MISSION_J20_KEY)
            except Exception:
                logger.exception("Controller: failed to register J20 mission hotkey")

            try:
                def start_loiter_mission(_e):
                    logger.info("Controller: '%s' key pressed - starting loiter mission", MISSION_LOITER_KEY)
                    self._set_last_mission("loiter")
                    threading.Thread(target=self.mission_loiter, daemon=True).start()
                keyboard_module.on_press_key(MISSION_LOITER_KEY, start_loiter_mission, suppress=False)
                logger.info("Controller: registered hotkey '%s' to start loiter mission", MISSION_LOITER_KEY)
            except Exception:
                logger.exception("Controller: failed to register loiter mission hotkey")

            # ADR 094: finish the round, then exit. Deferred, and reversible.
            try:
                self._last_finish_round_press = 0.0
                def finish_round_then_exit(_e):
                    now = time.time()
                    if now - self._last_finish_round_press < 0.5:
                        return                      # debounce key-repeat
                    self._last_finish_round_press = now
                    if self._finish_round_event.is_set():
                        # A deferred action that cannot be recalled is a trap:
                        # the operator waits minutes with no way back except
                        # killing the process (ADR 094).
                        self._finish_round_event.clear()
                        logger.info("\033[93m🏁 FINISH ROUND: cancelled — the "
                                    "session continues\033[0m")
                        return
                    self._finish_round_event.set()
                    # Pressed in the lobby the stop is immediate: the main loop's
                    # safe point is already true, and the quick-scan is now barred
                    # from starting another round. Say which one is happening -
                    # "at the next lobby" while sitting IN the lobby reads as a
                    # long wait and invites a second press that cancels it.
                    _st = self._analyzer.game_state if self._analyzer is not None else None
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
                self._simulate_respawn_flag = threading.Event()
                self._last_b_press_time = 0.0
                def simulate_respawn(_e):
                    now = time.time()
                    if now - self._last_b_press_time < 0.5:  # debounce: ignore key-repeat
                        return
                    self._last_b_press_time = now
                    logger.info("Controller: '%s' key pressed - simulating respawn detected (as if OCR detected 'RESPAWN')", SIMULATE_RESPAWN_KEY)
                    if self._analyzer is not None:
                        self._analyzer.inject_respawn_ocr_result(True, 1.0, "ocr")
                        logger.info("Controller: Injected fake OCR respawn result into analyzer cache.")
                    else:
                        logger.warning("Controller: No analyzer reference to inject fake OCR respawn result.")
                    self._simulate_respawn_flag.set()
                keyboard_module.on_press_key(SIMULATE_RESPAWN_KEY, simulate_respawn, suppress=False)
                logger.info("Controller: registered hotkey '%s' to simulate respawn detected", SIMULATE_RESPAWN_KEY)
            except Exception:
                logger.exception("Controller: failed to register simulate respawn hotkey")

            # Register hotkey for capturing screenshots (for testing/debugging)
            try:
                def capture_screenshot(e):
                    logger.info("Controller: '%s' key pressed - capturing screenshot", CAPTURE_SCREEN_SHOT)
                    if self._capture is not None and self._analyzer is not None:
                        try:
                            frame = self._capture.grab_from_thread()

                            # Create output directory if it doesn't exist
                            output_dir = Path("tests/test-output")
                            output_dir.mkdir(parents=True, exist_ok=True)

                            # Generate timestamp filename
                            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                            filename = output_dir / f"screenshot_{timestamp}.png"

                            if self._capture_with_overlay:
                                # Draw only state-relevant crop overlays when enabled.
                                crops = self._analyzer.crops_for_state()
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
                    with self._programmatic_key_lock:
                        if self._programmatic_key_counts.get(PADLOCK_CAMERA, 0) > 0:
                            return
                        if time.time() < self._prog_release_grace_until.get(PADLOCK_CAMERA, 0.0):
                            return
                    cooldown = 10.0
                    self._padlock_cooldown_until = time.time() + cooldown
                    logger.info("Controller: '%s' key pressed manually - padlock loop cooldown set for %.0fs", PADLOCK_CAMERA, cooldown)
                keyboard_module.on_press_key(PADLOCK_CAMERA, padlock_key_pressed, suppress=False)
                logger.info("Controller: registered hotkey '%s' to set padlock loop cooldown", PADLOCK_CAMERA)
            except Exception:
                logger.exception("Controller: failed to register padlock camera cooldown hotkey")

            # Auto-mission hotkey: force GAME_LOBBY state, then click PLAY/READY
            try:
                keyboard_module.on_press_key(AUTO_MISSION_KEY, self._on_auto_mission_hotkey, suppress=False)
                logger.info("Controller: registered hotkey '%s' to click PLAY/READY in GAME_LOBBY", AUTO_MISSION_KEY)
            except Exception:
                logger.exception("Controller: failed to register auto mission hotkey")

    def _release_manual_if_active(self) -> bool:
        """Operator hands the aircraft back (SAF-001). True if it was in manual.

        Manual now persists through respawn, so there has to be a deliberate way
        out or the session is stuck in manual until the round ends. The
        auto-mission key is that way out: it already means "wingman, take it".
        """
        try:
            if not self._manual_takeover_active():
                return False
            logger.info("\033[93mController: auto-mission key — returning control "
                        "to wingman (leaving GAME_BATTLE_MANUAL)\033[0m")
            self._analyzer.trigger_event("manual_release")
            self.set_auto_respawn_restart(True)
            return True
        except Exception:
            logger.exception("Controller: manual release failed")
            return False

    def _on_auto_mission_hotkey(self, _e=None):
        """AUTO_MISSION_KEY handler: force GAME_LOBBY, then click PLAY/READY.

        From a battle state (GAME_BATTLE, GAME_BATTLE_MANUAL,
        GAME_BATTLE_EJECT) a single press is REFUSED: 'm' can be hit as a game
        binding mid-flight, and forcing the FSM to GAME_LOBBY from a live
        battle clicks PLAY into the battlefield and sets the lobby quick-scan
        pressing ESC against the running game (2026-08-17 04:15 incident). A
        second press within 2 s still forces it — the deliberate stuck-state
        recovery stays available.
        """
        now = time.time()
        if now - self._last_auto_mission_key_ts < 0.5:  # debounce: ignore key-repeat
            return
        self._last_auto_mission_key_ts = now
        if self._analyzer is None:
            return
        current_state = self._analyzer.game_state
        # SAF-001: in manual this key means "wingman, take it back" — a single
        # press, because the operator is deliberately flying and asking. The
        # double-press guard below exists for the OTHER battle states, where 'm'
        # can be an accidental game binding mid-flight.
        if current_state == GameState.GAME_BATTLE_MANUAL:
            self._release_manual_if_active()
            return
        if current_state in (GameState.GAME_BATTLE, GameState.GAME_BATTLE_MANUAL,
                             GameState.GAME_BATTLE_EJECT):
            if now - self._auto_mission_force_armed_ts > 2.0:
                self._auto_mission_force_armed_ts = now
                logger.info(
                    "Controller: '%s' pressed during %s — ignored (press again "
                    "within 2s to force GAME_LOBBY)",
                    AUTO_MISSION_KEY, current_state.name)
                return
            logger.info(
                "Controller: '%s' pressed twice during %s — forcing GAME_LOBBY",
                AUTO_MISSION_KEY, current_state.name)
        if current_state != GameState.GAME_LOBBY:
            logger.info(
                "Controller: '%s' key pressed — forcing GAME_LOBBY (was %s)",
                AUTO_MISSION_KEY, current_state.name if hasattr(current_state, 'name') else current_state,
            )
            self._analyzer.trigger_event("manual_reset")
        if self._on_auto_mission_key is not None:
            self._on_auto_mission_key()
        crop = next(
            (c for c in ("PLAY", "READY") if c in self._crops),
            None,
        )
        if crop is None:
            logger.warning("Controller: '%s' pressed but no PLAY/READY crop configured", AUTO_MISSION_KEY)
            return
        logger.info("Controller: '%s' pressed in GAME_LOBBY - clicking %s (waiting for CANCEL)", AUTO_MISSION_KEY, crop)
        self.click_crop(self._crops[crop], block=False, count=1, region_name=crop)
        # Stamp the same cooldown the lobby quick-scan thread checks before it
        # clicks PLAY/READY on its own (analyzer.py _last_lobby_play_click_ts).
        # Without this, GAME_LOBBY entry resets that timestamp to 0, and the
        # quick-scan thread re-clicks the same button ~1s later, undoing this click.
        self._analyzer._last_lobby_play_click_ts = time.time()

    def _record_action_intent(self, action_type: str, **payload):
        intent = {
            "timestamp": time.time(),
            "action_type": action_type,
            **payload,
        }
        with self._action_intents_lock:
            self._action_intents.append(intent)

    def get_action_intents(self) -> list[dict]:
        with self._action_intents_lock:
            return list(self._action_intents)

    def set_on_good_luck_frame(self, callback) -> None:
        """Register callback fired when Good Luck OCR is detected with frame payload."""
        self._on_good_luck_frame = callback

    def set_on_manual_takeover_frame(self, callback) -> None:
        """Register callback fired before manual takeover FSM transition with frame payload."""
        self._on_manual_takeover_frame = callback

    def _handle_maneuver_key_press(self, key_name: str, is_injected: bool = False) -> bool:
        """Handle manual maneuver-key takeover logic.

        Returns True when the key press triggered mission cancel/manual takeover,
        otherwise False.

        @relation(SAF-001, scope=function)
        @relation(SAF-001.1, scope=function)
        @relation(SAF-001.2, scope=function)
        """
        if is_injected:
            return False
        # _programmatic_key_counts guards against is_injected being unreliable for keys
        # wingman actually injects (i/j/k/l). Keyed per-key, not one global flag: wingman
        # holding NOSE_DOWN during an eject correction must not also swallow the player
        # pressing a *different* maneuver key (e.g. ROLL_RIGHT) to take over. Arrow keys
        # are never injected, so skipping this check for them lets the user trigger manual
        # takeover during continuous key holds (afterburner, roll) without needing to find
        # a gap between mission key presses.
        # Arrow keys and the dedicated takeover key are never injected, so the
        # echo guard has nothing to protect against — and running them through
        # it would let a stale count swallow a deliberate takeover.
        if key_name not in ALT_FLIGHT_KEYS and key_name != MANUAL_TAKEOVER_KEY:
            with self._programmatic_key_lock:
                if self._programmatic_key_counts.get(key_name, 0) > 0:
                    return False
                # Trailing window after our own release: the X server auto-repeats
                # XTest-injected keys (measured ~25 Hz), and a repeat emitted while
                # we still held the key can be delivered by the XRecord listener a
                # few ms after the release drops the count to zero.
                if time.time() < self._prog_release_grace_until.get(key_name, 0.0):
                    logger.debug(
                        "Controller: maneuver key '%s' ignored — within post-release "
                        "grace of wingman's own key release (stale auto-repeat)",
                        key_name,
                    )
                    return False
        if self._game_battle_since and time.time() - self._game_battle_since < 2.0:
            logger.debug(
                "Controller: Maneuver key '%s' ignored — within 2s grace period of battle or eject entry",
                key_name,
            )
            return False
        # A climb, evade, or spawn-guard hold is commanded flight even with no
        # mission thread (the tree selects them with mission=False after a
        # respawn cancels the mission) — SAF-001's takeover must fire for
        # them too.
        if not (self.is_mission_running() or self._ejecting.is_set()
                or self._climbing.is_set() or self._missile_evading.is_set()
                or self._spawn_guarding.is_set()):
            return False

        logger.info("Controller: maneuver key '%s' pressed - entering GAME_BATTLE_MANUAL (manual takeover)", key_name)
        self._auto_respawn_restart = False
        self._eject_stop_reason = "manual_takeover"
        self._eject_stop.set()
        # SAF-001: cease ALL commanded flight — the FSM transition alone does
        # not stop the tactic hold threads (2026-08-17 session: the climb hold
        # kept pulsing nose-up 45 s into GAME_BATTLE_MANUAL). The events are
        # re-cleared by the next climb_mode/missile_evade_mode/spawn-guard
        # start.
        self._climb_stop.set()
        self._me_stop.set()
        self._sg_stop.set()
        self.cancel_mission()
        if self._analyzer is not None:
            try:
                if self._analyzer.game_state in (GameState.GAME_BATTLE, GameState.GAME_BATTLE_EJECT):
                    # Capture the pre-transition frame for live capture (P2_020).
                    # Frame is grabbed BEFORE trigger_event so the screenshot still
                    # shows the GAME_BATTLE HUD.
                    _mt_frame = None
                    if self._on_manual_takeover_frame is not None and self._capture is not None:
                        try:
                            _mt_frame = self._capture.grab_from_thread()
                        except Exception:
                            logger.exception("Controller: failed to capture manual takeover frame")
                    self._analyzer.trigger_event("manual_takeover")
                    if _mt_frame is not None and self._on_manual_takeover_frame is not None:
                        try:
                            self._on_manual_takeover_frame(_mt_frame)
                        except Exception:
                            logger.exception("Controller: _on_manual_takeover_frame callback failed")
            except Exception:
                # SAF-001: takeover is a safety transition. The flight-input
                # stop above has already run, so swallowing keeps the aircraft
                # safe — but a silent swallow here would hide a failed
                # trigger_event("manual_takeover"), leaving the FSM in
                # GAME_BATTLE while the operator believes they have control.
                logger.exception(
                    "Controller: manual-takeover FSM transition failed after flight input was stopped")
        return True


    def nose_up(self, hold_seconds: float = 2.5, block: bool = True):
        """Nose-up maneuver: presses and holds the configured nose-up key.

        Args:
            hold_seconds: How long to hold the key (default 2.5 seconds)
        """
        # Use generic executor to perform the key press
        self._execute_key_press(NOSE_UP_KEY, hold_seconds=hold_seconds, block=block, action_name='nose_up')

    def nose_down(self, hold_seconds: float = 2.5, block: bool = True):
        """Nose-down maneuver: presses and holds the configured nose-down key.

        Args:
            hold_seconds: How long to hold the key (default 2.5 seconds)
        """
        # Use generic executor to perform the key press
        self._execute_key_press(NOSE_DOWN_KEY, hold_seconds=hold_seconds, block=block, action_name='nose_down')

    def afterburner(self, hold_seconds: float = 2.5, block: bool = True):
        """Afterburner: presses and holds the configured afterburner key.

        Args:
            hold_seconds: How long to hold the key (default 2.5 seconds)
        """
        # Use generic executor to perform the key press
        self._execute_key_press(AFTERBURNER_KEY, hold_seconds=hold_seconds, block=block, action_name='afterburner')

    def _inc_programmatic_key(self, key: str) -> None:
        with self._programmatic_key_lock:
            self._programmatic_key_counts[key] = self._programmatic_key_counts.get(key, 0) + 1

    def _dec_programmatic_key(self, key: str) -> None:
        with self._programmatic_key_lock:
            remaining = self._programmatic_key_counts.get(key, 0) - 1
            if remaining <= 0:
                self._programmatic_key_counts.pop(key, None)
            else:
                self._programmatic_key_counts[key] = remaining

    def _execute_key_press(self, key: str, hold_seconds: float = 2.5, block: bool = True, action_name: str | None = None, ignore_cancel: bool = False):
        """Generic key press executor used by maneuvers.

        Args:
            key: key name to press/release
            hold_seconds: duration to hold the key
            block: if True, run in current thread; otherwise spawn a daemon thread
            action_name: optional label for logging
        """
        label = action_name or key

        # SAF-001: in GAME_BATTLE_MANUAL the operator owns the aircraft. Flares
        # are the sole exception — they are a defensive reflex the operator
        # cannot reasonably win, and they command no flight axis.
        #
        # Enforced HERE rather than at each caller because the callers are many
        # (mission threads, tactic holds, weapon and padlock loops, recovery
        # paths) and a missed one leaves wingman holding a control surface.
        # Observed 2026-08-30: after takeover the aircraft climbed 550 m to
        # 7655 m on its own with 'e', 'p' and 'k' still held down, because the
        # transition stopped the SELECTION but not the presses already in
        # flight. The operator could not fly.
        if key != DEPLOY_FLARES_KEY and self._manual_takeover_active():
            logger.debug("Controller: %s suppressed — GAME_BATTLE_MANUAL", label)
            return

        # Add color coding for specific actions
        color_start = ""
        color_end = ""
        if action_name == "deploy_flares":
            color_start = "\033[93m"  # Yellow
            color_end = "\033[0m"
        elif action_name == "padlock_camera":
            color_start = "\033[94m"  # Blue
            color_end = "\033[0m"
        elif action_name == "fire_active_weapon":
            color_start = "\033[95m"  # Magenta
            color_end = "\033[0m"

        complete_color_start = color_start
        complete_color_end = color_end
        if action_name == "fire_active_weapon":
            complete_color_start = ""
            complete_color_end = ""

        logger.debug("%sController: %s - pressing '%s' key for %s seconds%s", color_start, label, key, hold_seconds, color_end)

        def _do_press():
            try:
                if self._simulate_os_input:
                    self._record_action_intent("key_press", key=key, hold_seconds=float(hold_seconds), action=label)
                    start = time.time()
                    while (time.time() - start) < hold_seconds:
                        if not ignore_cancel:
                            if self._mission_cancel.wait(timeout=0.05):
                                logger.debug("Controller: %s cancelled", label)
                                break
                        else:
                            time.sleep(0.05)
                    self._record_action_intent("key_release", key=key, action=label)
                    logger.debug("%sController: %s complete%s", complete_color_start, label, complete_color_end)
                    return
                if not keyboard_module:
                    logger.error("Controller: keyboard library not available for %s", label)
                    return
                logger.debug("Controller: using keyboard library for '%s' press", key)
                self._inc_programmatic_key(key)
                release_span = 0.0  # measured below; finally must not NameError
                try:
                    _press_key(key)
                    start = time.time()
                    while (time.time() - start) < hold_seconds:
                        if not ignore_cancel:
                            if self._mission_cancel.wait(timeout=0.05):
                                logger.debug("Controller: %s cancelled", label)
                                break
                        else:
                            time.sleep(0.05)
                    release_started = time.time()
                    try:
                        keyboard_module.release(key)
                    except Exception:
                        logger.exception("Controller: failed to release '%s' key", key)
                    release_span = time.time() - release_started
                    logger.debug("%sController: %s complete%s", complete_color_start, label, complete_color_end)
                finally:
                    # Same stale-auto-repeat window as _eject_key: the X server
                    # repeats XTest-held keys, and queued repeats can land
                    # SECONDS after the release under load. Arm before dropping
                    # the count, scaled by the measured release latency.
                    self._arm_release_grace(key, span_s=release_span)
                    self._dec_programmatic_key(key)
            except Exception:
                logger.exception("Controller: %s failed", label)

        if block:
            _do_press()
        else:
            t = threading.Thread(target=_do_press, daemon=True)
            t.start()

    def airbrake(self, hold_seconds: float = 1.0, block: bool = True):
        """Apply airbrake by holding the configured airbrake key."""
        self._execute_key_press(AIRBRAKE_KEY, hold_seconds=hold_seconds, block=block, action_name='airbrake')

    def roll_left(self, hold_seconds: float = 0.3, block: bool = True):
        """Roll left by holding the configured roll-left key."""
        self._execute_key_press(ROLL_LEFT_KEY, hold_seconds=hold_seconds, block=block, action_name='roll_left')

    def roll_right(self, hold_seconds: float = 0.3, block: bool = True):
        """Roll right by holding the configured roll-right key."""
        self._execute_key_press(ROLL_RIGHT_KEY, hold_seconds=hold_seconds, block=block, action_name='roll_right')

    def orient_nose_to_target(
        self,
        error_norm: float,
        *,
        deadband: float = 0.05,
        kp: float = 0.30,
        min_hold_sec: float = 0.08,
        max_hold_sec: float = 0.35,
        cooldown_sec: float = 0.15,
    ) -> "str | None":
        """Apply proportional roll correction toward a target.

        Args:
            error_norm: Normalized horizontal error in [-1, 1].
                        Negative = target left of center → roll left.
                        Positive = target right of center → roll right.
            deadband:   No-action zone around zero.
            kp:         Proportional gain; hold_sec = kp * abs(error_norm).
            min_hold_sec / max_hold_sec: Clamp bounds on the roll hold duration.
            cooldown_sec: Minimum interval between consecutive roll commands.

        Returns:
            'left', 'right', or None if suppressed by deadband or cooldown.
        """
        if abs(error_norm) <= deadband:
            return None
        now = time.time()
        if now - self._last_orient_ts < cooldown_sec:
            return None
        hold = float(min(max(kp * abs(error_norm), min_hold_sec), max_hold_sec))
        self._last_orient_ts = now
        if error_norm < 0:
            self.roll_left(hold_seconds=hold, block=False)
            return "left"
        self.roll_right(hold_seconds=hold, block=False)
        return "right"

    def deploy_flares(self, hold_seconds: float = 0.05, block: bool = True, ignore_cancel: bool = False):
        """Deploy flares (short press of the configured flares key)."""
        self._execute_key_press(DEPLOY_FLARES_KEY, hold_seconds=hold_seconds, block=block, action_name='deploy_flares', ignore_cancel=ignore_cancel)

    def wingsweep(self, hold_seconds: float = 0.5, block: bool = True):
        """Perform a wingsweep maneuver by pressing the configured wingsweep key."""
        self._execute_key_press(WINGSWEEP_KEY, hold_seconds=hold_seconds, block=block, action_name='wingsweep')

    def press_escape(self, hold_seconds: float = 0.05, block: bool = False):
        """Press Escape once, used by safety-recovery handlers."""
        self._execute_key_press(
            'escape',
            hold_seconds=hold_seconds,
            block=block,
            action_name='escape_recovery',
            ignore_cancel=True,
        )

    def padlock_camera(self, hold_seconds: float = 0.1, block: bool = True):
        """Toggle padlock camera by pressing the configured padlock camera key."""
        self._execute_key_press(PADLOCK_CAMERA, hold_seconds=hold_seconds, block=block, action_name='padlock_camera')

    def padlock_target_switch(self, presses: int = 2, delay_between: float = 0.35) -> None:
        """Press padlock N times to cycle to a new target, then pause the auto-padlock loop briefly.

        Called after every 2 missiles fired to spread shots across enemy jets rather than
        concentrating all missiles on one target.
        """
        def _run():
            for i in range(presses):
                if i > 0:
                    time.sleep(delay_between)
                self.padlock_camera(hold_seconds=0.1, block=True)
            # Give the padlock loop a short rest so it doesn't immediately re-lock the old target
            self._padlock_cooldown_until = max(self._padlock_cooldown_until, time.time() + 2.0)
        threading.Thread(target=_run, daemon=True).start()

    def fire_machine_gun(self, hold_seconds: float = 1.0, block: bool = True):
        """Fire machine gun by holding the configured machine-gun key."""
        self._execute_key_press(FIRE_MACHINE_GUN, hold_seconds=hold_seconds, block=block, action_name='fire_machine_gun')

    def fire_active_weapon(self, hold_seconds: float = 0.1, block: bool = True):
        """Activate the currently selected weapon (short press)."""
        self._execute_key_press(FIRE_ACTIVE_WEAPON, hold_seconds=hold_seconds, block=block, action_name='fire_active_weapon')

    def reload_flares(self, block: bool = False):
        """Press SPECIAL_ABILITY to reload flares (triggered when flare count == 2)."""
        logger.info("\033[93m🔥 Reloading flares via SPECIAL_ABILITY key\033[0m")
        self._execute_key_press(SPECIAL_ABILITY, hold_seconds=0.1, block=block, action_name='reload_flares')

    def _eject_key(self, press: bool, key: str, note: str = "eject_and_dive") -> None:
        """Press or release a key inside the eject sequence, honoring replay simulation.

        NOSE_DOWN_KEY/NOSE_UP_KEY are also watched by the maneuver-key hotkey
        listener as a manual-takeover signal, and is_injected is unreliable for
        them (see _programmatic_key_counts comment in __init__). Without this,
        a corrective re-press issued past the 2s grace period is indistinguishable
        from the player taking over and self-cancels the eject (observed in
        production logs: every closed-loop correction triggered a spurious
        "manual takeover" ~0.2-0.5s after the re-press). Held-state is tracked
        locally so redundant calls (e.g. the cleanup path releasing a key a
        correction already released) don't double up the per-key counter.
        AFTERBURNER_KEY is not a watched maneuver key, so it bypasses this.

        Release ordering is load-bearing. Measured on this host (3.0 s XTest-held
        key, XRecord listening exactly as _LinuxXTestKeyboard does): the X server
        auto-repeats XTest-injected keys and emits 60 KeyPress events, one every
        ~40 ms, every one with send_event=False -- indistinguishable from human
        input. So the guard must not drop while the key is still physically down.
        Dropping the counter before the release (as this did) left the guard at
        zero for the whole duration of keyboard_module.release(), which opens a
        fresh X Display connection per call (_linux_key_event) -- a multi-ms
        window against a 40 ms repeat period, and the observed cause of three
        spurious "manual takeover" self-cancels in the 2026-07-30 16:27 session
        (log 1464/2739/7711, each 2-5 ms after a phase-end release).
        Physical release first, then a short drain before the guard drops, so
        repeats already queued in the XRecord pipeline cannot be misread as the
        player. This covers every release site by construction, including the
        phase-end releases that sit outside _eject_guard_hold().
        """
        # Accounted before the simulate/no-keyboard early returns so the budget
        # behaves identically in replay and unit tests.
        if key == NOSE_DOWN_KEY:
            self._account_nose_hold(press)
        if self._simulate_os_input:
            self._record_action_intent("key_press" if press else "key_release", key=key, action=note)
            return
        if not keyboard_module:
            return
        guarded = key in (NOSE_DOWN_KEY, NOSE_UP_KEY)
        if not guarded:
            try:
                (keyboard_module.press if press else keyboard_module.release)(key)
            except Exception:
                logger.error("Controller: %s of %r failed during %s%s",
                             "press" if press else "release", key, note,
                             "" if press else f" — {_LATCH_NOTE}")
            return

        if press == (key in self._eject_held_keys):
            return  # already in that state -- avoid double counting
        if press:
            self._eject_held_keys.add(key)
            self._inc_programmatic_key(key)
            try:
                _press_key(key)
            except Exception:
                logger.error("Controller: press of %r failed during %s", key, note)
            return

        self._eject_held_keys.discard(key)
        _release_started = time.time()
        try:
            keyboard_module.release(key)
        except Exception:
            logger.error("Controller: release of %r failed during %s — %s",
                         key, note, _LATCH_NOTE)
        finally:
            # Arm the suppression window BEFORE dropping the count so there is no
            # instant where neither guard is active. Scaled by release latency —
            # queued repeats drain for seconds under X load (2026-08-14 03:35).
            self._arm_release_grace(key, span_s=time.time() - _release_started)
            self._dec_programmatic_key(key)

    def _account_nose_hold(self, press: bool) -> None:
        """Accumulate real NOSE_DOWN hold time for the current eject sequence.

        Idempotent: repeated presses/releases of an already-held/already-released
        key do not double-count. No-op outside an eject (total is None).

        @relation(SAF-005, scope=function)
        """
        if self._eject_nose_held_total_s is None:
            return
        now = time.time()
        if press:
            if self._eject_nose_down_since is None:
                self._eject_nose_down_since = now
        elif self._eject_nose_down_since is not None:
            self._eject_nose_held_total_s += now - self._eject_nose_down_since
            self._eject_nose_down_since = None

    def _eject_nose_held_s(self) -> float:
        """Cumulative seconds NOSE_DOWN has actually been held this eject.

        Under ADR 069 this no longer gates actuation (the budget is counted in
        pulses); it feeds the over-rotation guard's "held long enough to have
        rotated past vertical" test.
        """
        if self._eject_nose_held_total_s is None:
            return 0.0
        held = self._eject_nose_held_total_s
        if self._eject_nose_down_since is not None:
            held += time.time() - self._eject_nose_down_since
        return held

    def _arm_release_grace(self, key: str, span_s: float = 0.0) -> None:
        """Suppress maneuver-key takeover for this key after our own release.

        The X server stops auto-repeating the moment the release lands, but
        DELIVERY of repeats already queued in the XRecord pipeline scales with
        X-server load, not with our timing. The 2026-08-14 03:35 soak session
        is the sizing case: a 0.15 s roll_left hold took 2.7 s to release, and
        its queued 'j' repeats were still being delivered 3.2 s AFTER the
        release — three of them fired manual takeover in a row.

        `span_s` is the measured duration of the physical release call — the
        cheapest live proxy for X-server load. The window is the fixed floor
        (fast healthy releases) or 3x the release latency (loaded server, e.g.
        3 x 2.56 s = 7.7 s covers the observed 3.2 s straggler with margin).
        Per-key cost only: a human takeover still lands via any other maneuver
        key or repeated presses.
        """
        window = max(self._prog_release_grace_s, 3.0 * span_s)
        with self._programmatic_key_lock:
            self._prog_release_grace_until[key] = time.time() + window

    @contextlib.contextmanager
    def _eject_guard_hold(self):
        """Keep the maneuver-key hotkey guard up across a release/re-press dance.

        _eject_key's own bracketing now covers each individual press/release
        (including a post-release drain), but the dance briefly has NO key held
        at all between the release and the re-press. This keeps the guard raised
        across that whole gap, without holding it up for the full multi-
        correction phase (which would also block a genuine manual takeover for
        many seconds). Reserves both NOSE_DOWN_KEY and NOSE_UP_KEY -- the
        straight-reissue and reversal branches touch different keys and either
        can run once inside.
        """
        for key in (NOSE_DOWN_KEY, NOSE_UP_KEY):
            self._inc_programmatic_key(key)
        try:
            yield
        finally:
            for key in (NOSE_DOWN_KEY, NOSE_UP_KEY):
                self._arm_release_grace(key)
                self._dec_programmatic_key(key)

    def _eject_telemetry(self):
        """One atomic telemetry snapshot for eject verification, or None."""
        if self._analyzer is None:
            return None
        get_snapshot = getattr(self._analyzer, "get_telemetry", None)
        if get_snapshot is None:
            return None
        try:
            return get_snapshot()
        except Exception:
            logger.exception("Controller: eject telemetry snapshot failed")
            return None

    def _eject_descent_control(self) -> bool:
        """Impulse rotation and ballistic descent (ADR 069).

        Returns True when cancelled (respawn or ADR 088 d1 rearm). The outcome is left in
        self._eject_phase_exit_reason.

        Two alternating regimes, one criterion:

        - **Rotate** — command a bounded NOSE_DOWN pulse, release it, then wait
          a full observation gap before judging. The controller never holds the
          key while waiting to see what the last input did. Continuous holding
          rotates the airframe past its velocity vector into a high-drag mush:
          measured 2026-08-10, held descents averaged -59 m/s against -130 m/s
          hands-off over the same eject (ADR 069 Fault B).
        - **Ballistic** — once the descent RATE holds at target across distinct
          samples, nose-down stays released and the aircraft is left to convert
          altitude into speed. This is the phase that actually descends.

        The criterion is the raw altitude rate, never the flight-path angle:
        the angle ratio saturates at 90 degrees exactly when the aircraft is
        accelerating hardest, which is precisely during a good dive (Fault A).

        @relation(SAF-012, scope=function)
        """
        start = time.time()
        pulses = 0
        established = False
        last_sample_ts: "float | None" = None
        last_fresh_wall = time.time()
        at_target_streak = 0
        below_floor_streak = 0
        climb_streak = 0
        self._eject_descended_since_press = False

        while True:
            if self._eject_stop.wait(timeout=self._eject_cl_check_interval_s):
                self._eject_phase_exit_reason = "cancelled"
                return True

            if time.time() - start >= self._eject_cl_max_s:
                logger.warning(
                    "Controller: eject_and_dive — descent control timeout (%.0fs) "
                    "— releasing", self._eject_cl_max_s)
                self._eject_phase_exit_reason = "timeout"
                return False

            # ADR 088: the eject fires on an EMPTY rack, but the game rearms on
            # a timer and the dive outlives that timer. Observed 2026-08-22
            # 01:51:33 — missiles went 0 -> 1 thirteen seconds into a dive
            # started because the count was 0, and the aircraft flew a usable
            # missile into the ground. Re-check the premise while acting on it.
            if self._eject_abort_on_rearm and self._analyzer is not None:
                try:
                    _mis = self._analyzer.get_ammo_missiles()
                except Exception:
                    _mis = None
                if isinstance(_mis, int) and _mis > 0:
                    logger.warning(
                        "Controller: eject_and_dive — ABORT, %d missile(s) "
                        "rearmed mid-descent (ADR 088)", _mis)
                    self._eject_phase_exit_reason = "rearmed"
                    self._eject_stop_reason = "rearmed"
                    return True

            snap = self._eject_telemetry()
            rate = None
            sample_ts = None
            angle = None
            if snap is not None:
                angle = snap.pitch_angle_deg()
                if snap.altitude_fresh():
                    rate = snap.altitude.rate
                    sample_ts = snap.altitude.ts

            # No NEW evidence: tolerate a gap, never act on missing data
            # (ADR 038). Past the telemetry staleness horizon, stop flying
            # blind — release and let the sequence proceed on its own.
            if rate is None or sample_ts is None or sample_ts == last_sample_ts:
                if time.time() - last_fresh_wall >= self._eject_tel_stale_after_s:
                    logger.info(
                        "Controller: eject_and_dive — telemetry lost during descent "
                        "— releasing nose-down")
                    self._eject_phase_exit_reason = (
                        "established" if established else "no_telemetry")
                    return False
                continue
            last_sample_ts = sample_ts
            last_fresh_wall = time.time()
            if rate < 0:
                # Evidence the flight path actually rotated downward this
                # attempt — the precondition for reading a later climb as
                # over-rotation (ADR 068 d1, carried forward).
                self._eject_descended_since_press = True

            # ADR 069 d8: burner is gated on STEEP descending flight. Engaged
            # while shallow it accelerates the aircraft ACROSS the map, which
            # is the arena-exit failure (Roadmap 001 M1).
            self._eject_manage_afterburner(rate, angle)

            # ADR 069 d1 (revised): the criterion is the ANGLE, with the rate
            # as a fallback only when the angle is unavailable.
            #
            # Rate alone is satisfied by SPEED, not by attitude: measured
            # 2026-08-10 18:36, a -47 degree dive accelerating to 1576 KPH held
            # -187 to -309 m/s — three times the 100 m/s target — while the
            # flight path stayed shallow. That descends fast and flies 7 km
            # ACROSS the arena doing it; the same altitude at -75 degrees costs
            # 2 km. The original decision made rate the criterion to escape the
            # saturated angle, but d6 in this same ADR fixed the angle, so d1
            # was compensating for a defect that no longer exists.
            if angle is not None:
                at_target = angle <= -self._eject_cl_target_dive_angle_deg
                shallow = angle > -self._eject_cl_dive_angle_floor_deg
            else:
                at_target = rate <= -self._eject_cl_descent_target_mps
                shallow = rate > -self._eject_cl_descent_floor_mps

            if at_target:
                below_floor_streak = 0
                climb_streak = 0
                at_target_streak += 1
                if not established and at_target_streak >= self._eject_cl_confirm_consecutive:
                    established = True
                    self._eject_phase_exit_reason = "established"
                    self._eject_key(False, NOSE_DOWN_KEY)
                    logger.info(
                        "Controller: eject_and_dive — dive established "
                        "(nose %s, %.0f m/s, %d pulse(s), %.1fs) — ballistic, "
                        "nose-down released",
                        f"{angle:+.0f}deg" if angle is not None else "?",
                        rate, pulses, time.time() - start)
                continue

            at_target_streak = 0

            if established:
                # Between the target and the floor is a deliberate deadband:
                # the aircraft is steep enough to be left alone, and pulsing at
                # every degree of sag is what produced the ADR 068 limit cycle.
                # Only a SUSTAINED shallow reading resumes rotation.
                if shallow:
                    below_floor_streak += 1
                    if below_floor_streak < self._eject_cl_confirm_consecutive:
                        continue
                    logger.info(
                        "Controller: eject_and_dive — dive shallow (nose %s, "
                        "%.0f m/s, %d consecutive) — resuming rotation",
                        f"{angle:+.0f}deg" if angle is not None else "?",
                        rate, below_floor_streak)
                    established = False
                    below_floor_streak = 0
                else:
                    below_floor_streak = 0
                    continue

            # --- rotation needed ------------------------------------------
            # Over-rotation guard (ADR 068 d1/d5, carried forward): a climb
            # AFTER an observed descent, with the key held long enough to have
            # rotated past vertical, means further nose-down deepens the error.
            if (rate > 0
                    and self._eject_descended_since_press
                    and self._eject_cl_over_rotation_after_s > 0
                    and self._eject_nose_held_s() >= self._eject_cl_over_rotation_after_s):
                climb_streak += 1
                if climb_streak >= self._eject_cl_confirm_consecutive:
                    logger.warning(
                        "Controller: eject_and_dive — climbing (%.0f m/s, %d consecutive) "
                        "after a prior descent — over-rotated, releasing", rate, climb_streak)
                    self._eject_phase_exit_reason = "over_rotation"
                    return False
                continue
            climb_streak = 0

            if pulses >= self._eject_cl_max_rotation_pulses:
                logger.warning(
                    "Controller: eject_and_dive — rotation pulses exhausted (%d) "
                    "at %.0f m/s — proceeding ballistic", pulses, rate)
                self._eject_phase_exit_reason = "pulses_exhausted"
                return False

            pulses += 1
            logger.info(
                "Controller: eject_and_dive — rotation pulse %d/%d "
                "(rate %.0f m/s, nose %s)",
                pulses, self._eject_cl_max_rotation_pulses, rate,
                f"{angle:+.0f}deg" if angle is not None else "?")
            if self._eject_pulse_nose_down():
                self._eject_phase_exit_reason = "cancelled"
                return True
            # The pulse consumed real time; the next sample must be a fresh one.
            last_fresh_wall = time.time()

    def _eject_pulse_nose_down(self) -> bool:
        """One bounded NOSE_DOWN impulse plus its observation gap (ADR 069 d2).

        Returns True if the eject was cancelled during the pulse. The gap is
        mandatory: acting again before the aircraft has had a full telemetry
        refresh to respond is what produced the 18 s limit cycle.
        """
        with self._eject_guard_hold():
            self._eject_key(True, NOSE_DOWN_KEY)
            cancelled = self._eject_stop.wait(timeout=self._eject_cl_rotation_pulse_s)
            self._eject_key(False, NOSE_DOWN_KEY)
        if cancelled:
            return True
        return self._eject_stop.wait(timeout=self._eject_cl_observe_after_pulse_s)

    def _eject_manage_afterburner(self, rate: "float | None",
                                  angle: "float | None" = None) -> None:
        """Engage AFTERBURNER only while STEEPLY descending (ADR 069 d8).

        Burner during shallow or climbing flight is what carries the aircraft
        out of the arena; during a steep dive it accelerates the descent
        (speed climbed 481 to 1286 KPH across the 2026-08-10 ballistic phase).
        Gated on the angle when available for the same reason the dive
        criterion is: a shallow dive at 1500 KPH satisfies any rate test while
        crossing the map. Missing telemetry changes nothing — never act on
        absent data.
        """
        if rate is None:
            return
        if angle is not None:
            descending = angle <= -self._eject_cl_dive_angle_floor_deg
        else:
            descending = rate <= -self._eject_cl_descent_floor_mps
        if descending and not self._eject_ab_engaged:
            self._eject_key(True, AFTERBURNER_KEY)
            self._eject_ab_engaged = True
            logger.info("Controller: eject_and_dive — descending (%.0f m/s) — "
                        "afterburner engaged", rate)
        elif not descending and self._eject_ab_engaged:
            self._eject_key(False, AFTERBURNER_KEY)
            self._eject_ab_engaged = False
            logger.info("Controller: eject_and_dive — descent shallow (%.0f m/s) — "
                        "afterburner released to avoid crossing the arena", rate)

    def eject_and_dive(self, on_complete=None):
        """Cancel mission, hold NOSE_DOWN + AFTERBURNER simultaneously.

        NOSE_DOWN is held until telemetry confirms a steep dive and then kept
        held through the descent (ADR 068 — the game auto-levels on release);
        the hold ends on respawn, the over-rotation guard, the nose budget, or
        the legacy timer when telemetry never arrives.
        AFTERBURNER is held until respawn is detected (or a 120s safety timeout);
        a speed trend that fails to rise after engagement triggers a bounded re-press.
        on_complete: optional callable invoked in the finally block after all keys are released.

        No-ops (with a debug log) if an eject sequence is already in progress —
        callers should not start a second _run() thread racing the first over the
        same NOSE_DOWN/AFTERBURNER key state.
        """
        if self._ejecting.is_set():
            logger.debug("Controller: eject_and_dive already in progress — ignoring duplicate trigger")
            return
        logger.info("\033[91m🚀 MISSILES EMPTY — cancelling mission and ejecting\033[0m")
        self.cancel_mission()
        self._eject_stop_reason = ""
        self._eject_stop.clear()
        self._eject_held_keys.clear()
        self._eject_phase_exit_reason = ""
        # Opens nose-hold accounting for this sequence (None = not in an eject).
        # The rotation-evidence flag is NOT reset here — the descent controller
        # owns its scoping (CR-014-13).
        self._eject_nose_held_total_s = 0.0
        self._eject_nose_down_since = None
        # ADR 069 d8: burner starts DISENGAGED and is gated on descending
        # flight; pressing it while still climbing is what crosses the arena.
        self._eject_ab_engaged = False
        # Reset the grace-period timestamp so buffered/held flight keys (e.g. 'k' on key-repeat
        # from normal gameplay) cannot cancel the eject within the first 2 seconds of starting it.
        self._game_battle_since = time.time()
        # Force health state to dead so the False→True transition fires when
        # health is detected again after respawn, triggering mission restart.
        # Synthetic reset (ADR 061): must not count as an observed death.
        if self._analyzer is not None:
            self._analyzer.mark_health_dead_synthetic()

        def _run():
            self._ejecting.set()
            try:
                if not self._simulate_os_input and not keyboard_module:
                    logger.error("Controller: keyboard library not available for eject_and_dive")
                    return
                # Wait for the mission thread to fully exit before touching
                # flight keys so its _execute_key_press finally block cannot
                # release a key we just pressed.
                mission_exit_deadline = time.time() + 2.0
                while self.is_mission_running() and time.time() < mission_exit_deadline:
                    time.sleep(0.05)
                logger.info(
                    "Controller: eject_and_dive — descent control engaged "
                    "(impulse rotation, target %.0f m/s)",
                    self._eject_cl_descent_target_mps)

                # ADR 069: one controller owns the whole descent — rotation
                # pulses, the ballistic phase, afterburner gating, and the
                # wall-clock backstop. The old post-release watcher (re-entry
                # bookkeeping, separate afterburner verification, a second
                # 120 s deadline) is subsumed: there is no "post-release"
                # regime any more, because release IS the descent.
                if self._eject_cl_enabled:
                    cancelled = self._eject_descent_control()
                else:
                    self._eject_key(True, NOSE_DOWN_KEY)
                    self._eject_key(True, AFTERBURNER_KEY)
                    self._eject_ab_engaged = True
                    cancelled = self._eject_stop.wait(timeout=self._eject_nose_hold_s)

                if cancelled:
                    # A respawn-triggered stop is a successful eject, not an
                    # anomaly — the reason lets the ADR044/045 validators tell
                    # them apart.
                    logger.info(
                        "Controller: eject_and_dive — cancelled during descent (reason=%s)",
                        self._eject_stop_reason or "unknown")
                else:
                    # Descent control finished on its own terms (established and
                    # telemetry lost, pulses spent, over-rotation, or timeout).
                    # Keep the burner on if we are still descending and simply
                    # wait out the remaining respawn window.
                    logger.info(
                        "Controller: eject_and_dive — descent control ended (%s) "
                        "— holding until respawn", self._eject_phase_exit_reason or "unknown")
                    # ADR 088 d1: the hold outlives the rearm timer just as the
                    # descent does, so it needs the same re-check. Polled rather
                    # than a single wait — with one wait, 4 of 85 dives still
                    # completed carrying missiles (2026-08-22 02:18 session),
                    # because the rack refilled AFTER descent control ended.
                    _hold_deadline = time.time() + self._eject_cl_max_s
                    while not self._eject_stop.wait(
                            timeout=self._eject_cl_check_interval_s):
                        if time.time() >= _hold_deadline:
                            break
                        if not (self._eject_abort_on_rearm
                                and self._analyzer is not None):
                            continue
                        try:
                            _mis = self._analyzer.get_ammo_missiles()
                        except Exception:
                            _mis = None
                        if isinstance(_mis, int) and _mis > 0:
                            logger.warning(
                                "Controller: eject_and_dive — ABORT during hold, "
                                "%d missile(s) rearmed (ADR 088 d1)", _mis)
                            self._eject_stop_reason = "rearmed"
                            break
            finally:
                self._ejecting.clear()
                self._eject_nose_held_total_s = None
                self._eject_nose_down_since = None
                if self._simulate_os_input:
                    self._record_action_intent("key_release", key=AFTERBURNER_KEY, action="eject_and_dive")
                    self._record_action_intent("key_release", key=NOSE_DOWN_KEY, action="eject_and_dive")
                else:
                    self._eject_key(False, AFTERBURNER_KEY)
                    self._eject_key(False, NOSE_DOWN_KEY)
                    # A measure-correct-measure reversal may have been
                    # mid-tap when the sequence was cancelled; _eject_key is a
                    # no-op if NOSE_UP was already released.
                    self._eject_key(False, NOSE_UP_KEY)
                logger.info("Controller: eject_and_dive complete")
                if on_complete is not None:
                    try:
                        on_complete()
                    except Exception:
                        logger.exception("Controller: eject_and_dive on_complete callback failed")

        self._eject_thread = threading.Thread(target=_run, daemon=True)
        self._eject_thread.start()

    def start_search_and_destroy_loop(self):
        """Start background padlock + weapon-fire loops.

        Loops stop when either _sdl_stop is set (explicit stop) or
        _mission_cancel is set (any cancellation signal), whichever comes first.
        """
        padlock_alive = (self._sdl_padlock_thread is not None
                         and self._sdl_padlock_thread.is_alive())
        weapon_alive = (self._sdl_weapon_thread is not None
                        and self._sdl_weapon_thread.is_alive())
        if self._sdl_stop is not None and not self._sdl_stop.is_set() and (padlock_alive or weapon_alive):
            logger.debug("Controller: search_and_destroy_loop already running")
            return

        self._sdl_stop = threading.Event()
        stop = self._sdl_stop

        def _padlock_loop():
            logger.info("Controller: search_and_destroy padlock loop started")
            try:
                while not stop.is_set() and not self._mission_cancel.is_set():
                    if time.time() >= self._padlock_cooldown_until:
                        self.padlock_camera(hold_seconds=0.1, block=True)
                    for _ in range(60):  # 6 s interruptible
                        if stop.wait(timeout=0.1) or self._mission_cancel.is_set():
                            break
            finally:
                logger.info("Controller: search_and_destroy padlock loop stopped")

        def _weapon_loop():
            logger.info("Controller: search_and_destroy weapon loop started")
            try:
                while not stop.is_set() and not self._mission_cancel.is_set():
                    should_fire = True
                    if self._target_painting_mode and self._analyzer is not None:
                        ammo_lock = self._analyzer._ammo_lock
                        if not ammo_lock.acquire(timeout=0.5):
                            logger.debug("Controller: target_painting ammo lock timeout — firing")
                        else:
                            try:
                                missiles = self._analyzer._ammo_missiles
                            finally:
                                if ammo_lock.locked():
                                    ammo_lock.release()
                            if missiles == 1 and self._analyzer.game_state != GameState.GAME_BATTLE_MANUAL:
                                logger.debug("Controller: target_painting suppressing fire (ammo_missiles=1)")
                                should_fire = False
                    if should_fire:
                        self.fire_active_weapon(hold_seconds=0.1, block=True)
                    steps = max(1, int(self._weapon_loop_interval / 0.1))
                    for _ in range(steps):
                        if stop.wait(timeout=0.1) or self._mission_cancel.is_set():
                            break
            finally:
                logger.info("Controller: search_and_destroy weapon loop stopped")

        self._sdl_padlock_thread = threading.Thread(target=_padlock_loop, daemon=True)
        self._sdl_weapon_thread = threading.Thread(target=_weapon_loop, daemon=True)
        self._sdl_padlock_thread.start()
        self._sdl_weapon_thread.start()
        logger.info("Controller: search_and_destroy_loop started")

    def stop_search_and_destroy_loop(self):
        """Stop the search-and-destroy padlock + weapon-fire loops."""
        if self._sdl_stop is None or self._sdl_stop.is_set():
            logger.debug("Controller: search_and_destroy_loop not running")
            return
        self._sdl_stop.set()
        if self._sdl_padlock_thread:
            self._sdl_padlock_thread.join(timeout=1.0)
            self._sdl_padlock_thread = None
        if self._sdl_weapon_thread:
            self._sdl_weapon_thread.join(timeout=1.0)
            self._sdl_weapon_thread = None
        logger.info("Controller: search_and_destroy_loop stopped")

    def disengage_roll_right(self, duration: float = 10.0):
        """Cancel mission maneuvers then hold ROLL_RIGHT_KEY for `duration` seconds.

        search_and_destroy_loop() keeps running during the roll so the aircraft
        continues tracking and firing at any enemy that comes into view.
        Called when no enemy is detected in ENEMY_CLOSE_BY for 30+ seconds.
        """
        logger.info("\033[93m↩ No enemy for 30s — cancelling mission and rolling right for %.0fs\033[0m", duration)
        self.cancel_mission()

        def _run():
            if not keyboard_module:
                logger.error("Controller: keyboard library not available for disengage_roll_right")
                return
            self.start_search_and_destroy_loop()
            # ROLL_RIGHT is a watched maneuver key: without the programmatic
            # bracket + release grace, its auto-repeats (and the trailing
            # repeats delivered just after release) read as the PLAYER pressing
            # 'l' — and the mission this method restarts at the end gets
            # immediately self-cancelled into manual takeover.
            self._inc_programmatic_key(ROLL_RIGHT_KEY)
            try:
                _press_key(ROLL_RIGHT_KEY)
                # NOT _interruptible_sleep: cancel_mission() above set
                # _mission_cancel, which would abort the roll after
                # milliseconds and leave the aircraft flying straight out of
                # the arena (observed 2026-07-28 20:40:03, an 8 ms "roll").
                # The roll must outlive the cancel it issued; only program
                # exit interrupts it.
                deadline = time.time() + duration
                while time.time() < deadline:
                    if self._exit_event is not None and self._exit_event.is_set():
                        break
                    time.sleep(0.1)
            finally:
                _release_started = time.time()
                try:
                    keyboard_module.release(ROLL_RIGHT_KEY)
                except Exception:
                    logger.error("Controller: release of %r failed ending disengage roll — %s",
                                 ROLL_RIGHT_KEY, _LATCH_NOTE)
                self._arm_release_grace(ROLL_RIGHT_KEY,
                                        span_s=time.time() - _release_started)
                self._dec_programmatic_key(ROLL_RIGHT_KEY)
                if not self.is_mission_running():
                    self.stop_search_and_destroy_loop()
            logger.info("Controller: disengage_roll_right complete")
            # The cancelled mission thread may still be tearing down — its
            # lock releases a few ms after cancel. Wait for it (bounded), or
            # the not-running check races and the restart is silently skipped,
            # leaving no mission, no loops, and an uncommanded aircraft.
            teardown_deadline = time.time() + 5.0
            while self.is_mission_running() and time.time() < teardown_deadline:
                time.sleep(0.1)
            with self._last_mission_lock:
                last_mission = self._last_mission
            if self._auto_respawn_restart and last_mission and not self.is_mission_running():
                logger.info("Controller: restarting mission after disengage")
                self.restart_last_mission()
            elif self.is_mission_running():
                logger.warning(
                    "Controller: disengage restart skipped — mission still running "
                    "after %.0fs teardown wait", 5.0)

        self._disengage_thread = threading.Thread(target=_run, daemon=True)
        self._disengage_thread.start()

    def is_disengage_running(self) -> bool:
        """True while a disengage_roll_right maneuver thread is alive
        (ADR 024 3.1b — the Disengage leaf's is_running_fn)."""
        thread = self._disengage_thread
        return thread is not None and thread.is_alive()

    def is_ejecting(self) -> bool:
        """True while an eject_and_dive sequence is in progress
        (ADR 024 3.1b — the Eject leaf's is_running_fn)."""
        return self._ejecting.is_set()

    def missile_evade_mode(self):
        """Hold AFTERBURNER + ROLL_RIGHT + YAW_LEFT until incoming clears (ADR 070).

        Non-blocking: performs the duplicate check, sets _missile_evading, spawns
        the daemon thread, and returns. Idempotent while the thread is alive — a
        second detection during an active evade extends it via the clear timer
        rather than starting a second thread (d8).

        Termination (d5): incoming absent for _me_clear_s wall-clock seconds AND
        at least _me_min_clear_samples FRESH negative cache updates since the
        last positive — a negative carrying a timestamp already counted is the
        same stale cache entry read twice and is ignored, so a stalled analyzer
        cannot end the evade early. Unconditional release at _me_max_hold_s (d6).
        The mission is NOT cancelled (d7): engage-geometry suppression comes from
        selection priority, and the padlock/weapon loops keep running.

        @relation(FR-006, scope=function)
        """
        if self._missile_evading.is_set():
            logger.debug("Controller: missile_evade_mode already in progress — extending")
            return
        # ADR 070 d11: eject owns the airframe. Selection priority (d1) only
        # stops an evade STARTING when Eject is already selected; this covers
        # the same instant from the Controller side.
        if self._ejecting.is_set():
            logger.info("Controller: missile_evade suppressed — eject in progress")
            return
        # d8: flag set in the caller's thread, before any concurrency exists,
        # so is_missile_evading() never under-reports after a start.
        self._missile_evading.set()
        self._me_stop.clear()
        logger.info("\033[95m🌀 MISSILE EVADE — holding %s\033[0m",
                    " + ".join(self._missile_evade_key_labels()))

        def _run():
            try:
                if not self._simulate_os_input and not keyboard_module:
                    logger.error("Controller: keyboard library not available for missile_evade_mode")
                    return
                self._run_missile_evade_hold()
            finally:
                self._missile_evading.clear()

        self._me_thread = threading.Thread(target=_run, daemon=True)
        self._me_thread.start()

    def _missile_evade_keys(self) -> tuple:
        """Keys held for the duration of an evade, in press order (ADR 070 d3/d13).

        NOSE_DOWN joins only under the d13 pitch_down variant. Both it and
        ROLL_RIGHT are watched maneuver keys and get the d4 bracket.
        """
        keys = [AFTERBURNER_KEY, ROLL_RIGHT_KEY, YAW_LEFT]
        if self._me_pitch_down:
            keys.append(NOSE_DOWN_KEY)
        return tuple(keys)

    def _missile_evade_key_labels(self) -> list:
        labels = ["afterburner", "roll right", "yaw left"]
        if self._me_pitch_down:
            labels.append("nose down")
        return labels

    def _run_missile_evade_hold(self):
        """Thread body for missile_evade_mode: press, poll, release (ADR 070).

        @relation(SAF-001, scope=function)
        @relation(SAF-006, scope=function)
        """
        entry_ts = time.time()
        # d5: seed the last positive with the detection timestamp that
        # triggered the tactic, so the timer is well-defined from the first
        # poll. Analyzer absent (unit tests) → seed with entry time; no cache
        # means no fresh samples, so only the cap or a stop ends the hold —
        # "no perception" is never read as "clear".
        seed_ts = entry_ts
        if self._analyzer is not None:
            try:
                seed_ts = self._analyzer.get_incoming_cache_timestamp() or entry_ts
            except Exception:
                logger.exception("Controller: missile_evade seed read failed")
        last_positive_ts = seed_ts
        last_counted_ts = seed_ts
        fresh_negatives = 0
        exit_reason = "stopped"

        # d4: ROLL_RIGHT (and NOSE_DOWN under d13) are watched maneuver keys —
        # held via XTest they auto-repeat ~40 ms with send_event=False, and each
        # repeat would read as the player pressing the key and cancel the
        # mission into manual takeover. Same bracket as disengage_roll_right.
        # 'e' and ';' are unwatched and need none.
        hold_keys = self._missile_evade_keys()
        guarded_keys = tuple(k for k in hold_keys if k in _WATCHED_MANEUVER_KEYS)
        ab_held = AFTERBURNER_KEY in hold_keys

        def _set_burner(pressed: bool):
            # ADR 075: burner-only press/release inside the hold. 'e' is not a
            # watched maneuver key, so no programmatic bracket is needed.
            if self._simulate_os_input:
                self._record_action_intent(
                    "key_press" if pressed else "key_release",
                    key=AFTERBURNER_KEY, action="missile_evade")
            elif keyboard_module:
                try:
                    (keyboard_module.press if pressed
                     else keyboard_module.release)(AFTERBURNER_KEY)
                except Exception:
                    logger.exception(
                        "Controller: missile_evade burner %s failed",
                        "press" if pressed else "release")

        for _key in guarded_keys:
            self._inc_programmatic_key(_key)
        try:
            for _key in hold_keys:
                if self._simulate_os_input:
                    self._record_action_intent("key_press", key=_key, action="missile_evade")
                else:
                    try:
                        _press_key(_key)
                    except Exception:
                        logger.exception("Controller: missile_evade press failed for '%s'", _key)

            # NOT _interruptible_sleep: the hold must be independent of mission
            # cancellation (d7 — the tactic never touches mission state).
            # _me_stop is the shutdown path; _exit_event covers program exit.
            while not self._me_stop.wait(timeout=0.1):
                if self._exit_event is not None and self._exit_event.is_set():
                    break
                # ADR 070 d11: yield the airframe the instant an eject begins.
                # Selection priority is NOT symmetric in time — it prevents an
                # evade STARTING under a selected Eject, but ConditionTactic.
                # terminate is a no-op and this thread self-terminates on its
                # own clear timer, so an eject that starts AFTER the evade had
                # nothing to stop it. Observed 2026-08-12 05:34:50: the evade
                # held roll-right + yaw-left + burner for 4.8 s INTO an eject,
                # which climbed to +55deg while its descent controller pulsed
                # nose-down against it (alt 7596 -> 9347 m) and its burner gate,
                # which only engages while descending, stayed shut for 32 s.
                # Releasing here also prevents the reverse corruption: this
                # thread's finally releasing AFTERBURNER out from under a
                # running eject, whose _eject_ab_engaged flag would still read
                # True and never re-press it.
                if self._ejecting.is_set():
                    logger.info("Controller: missile_evade — eject started, "
                                "releasing keys to the eject sequence")
                    exit_reason = "eject_preempt"
                    break
                # SAF-001 backstop (the climb-hold analogue): release when the
                # operator owns the airframe (GAME_BATTLE_MANUAL) or the FSM
                # has left battle altogether. GAME_BATTLE_EJECT stays with the
                # d11 _ejecting handoff above, which owns that transition.
                if self._analyzer is not None:
                    _st = getattr(self._analyzer, "game_state", None)
                    if isinstance(_st, GameState) and _st not in (
                            GameState.GAME_BATTLE, GameState.GAME_BATTLE_EJECT):
                        logger.info("Controller: missile_evade — game state %s, "
                                    "releasing keys", _st.name)
                        exit_reason = "state_exit"
                        break
                # ADR 075: at 0% the burner is off AND a held key blocks the
                # game's recharge — release it (manoeuvre keys stay held) and
                # re-press once the rearm margin refills. The evade may burn
                # the climb tactic's reserve; only empty forces a release.
                fuel = self._read_fuel_pct()
                if fuel is not None:
                    if ab_held and fuel <= 0:
                        _set_burner(False)
                        ab_held = False
                        logger.info(
                            "Controller: missile_evade — fuel empty, releasing "
                            "afterburner to allow recharge (manoeuvre keys held)")
                    elif not ab_held and fuel >= self._fuel_rearm_margin:
                        _set_burner(True)
                        ab_held = True
                        logger.info(
                            "Controller: missile_evade — fuel %d%% — "
                            "afterburner re-engaged", fuel)
                now = time.time()
                if now - entry_ts >= self._me_max_hold_s:
                    logger.warning(
                        "Controller: missile_evade max hold (%.0fs) reached — "
                        "releasing (last incoming ts %.3f). Detector fault, "
                        "not a normal exit.",
                        self._me_max_hold_s, last_positive_ts)
                    exit_reason = "max_hold"
                    break
                # ADR 070 d12: the manoeuvre has run its useful course. A NORMAL
                # exit at INFO — distinct from the max_hold backstop above,
                # which means the detector is stuck. Conflating the two would
                # log "detector fault" on every genuinely long engagement and
                # poison the logs the effectiveness work reads.
                if now - entry_ts >= self._me_max_manoeuvre_s:
                    logger.info(
                        "Controller: missile_evade — manoeuvre limit (%.1fs) "
                        "reached, releasing while incoming is still present",
                        self._me_max_manoeuvre_s)
                    exit_reason = "manoeuvre_limit"
                    break
                if self._analyzer is None:
                    continue
                try:
                    detected, _, _ = self._analyzer.get_incoming_cache_result()
                    cache_ts = self._analyzer.get_incoming_cache_timestamp()
                except Exception:
                    logger.exception("Controller: missile_evade cache poll failed")
                    continue
                if detected:
                    # A fresh positive extends the evade (d8): the clear timer
                    # is measured from the last positive and simply moves on.
                    if cache_ts > last_positive_ts:
                        last_positive_ts = cache_ts
                        last_counted_ts = max(last_counted_ts, cache_ts)
                    fresh_negatives = 0
                elif cache_ts > last_counted_ts:
                    # Fresh negative — the cache TIMESTAMP advanced, not merely
                    # the result. An unchanged timestamp is a stale entry read
                    # twice and must not count (d5).
                    last_counted_ts = cache_ts
                    fresh_negatives += 1
                if (fresh_negatives >= self._me_min_clear_samples
                        and (now - last_positive_ts) >= self._me_clear_s):
                    exit_reason = "clear"
                    break
        finally:
            _release_started = time.time()
            for _key in reversed(hold_keys):
                if self._simulate_os_input:
                    self._record_action_intent("key_release", key=_key, action="missile_evade")
                elif keyboard_module:
                    try:
                        keyboard_module.release(_key)
                    except Exception:
                        logger.error("Controller: release of %r failed ending missile evade — %s",
                                     _key, _LATCH_NOTE)
            _release_span = time.time() - _release_started
            # Physical release first, THEN the grace + counter drop, so repeats
            # already queued in the XRecord pipeline cannot be misread as the
            # player (the _eject_key release-ordering finding). Grace scaled by
            # release latency (2026-08-14 03:35 delayed-echo finding).
            for _key in guarded_keys:
                self._arm_release_grace(_key, span_s=_release_span)
                self._dec_programmatic_key(_key)
        logger.info("Controller: missile_evade complete (%s, %.1fs)",
                    exit_reason, time.time() - entry_ts)

    def is_missile_evading(self) -> bool:
        """True while a missile_evade_mode hold is in progress
        (ADR 070 — the MissileEvade leaf's is_running_fn)."""
        return self._missile_evading.is_set()

    def climb_mode(self, target_alt: "float | None" = None,
                   max_s: "float | None" = None,
                   fuel_floor_pct: float = 0.0,
                   exit_lead_s: float = 0.0):
        """Hold NOSE_UP + AFTERBURNER until altitude recovers (ADR 073 3.2b).

        Non-blocking and idempotent while the thread is alive (the ADR 070
        d8 pattern). Suppressed while an eject or missile evade owns the
        airframe — selection priority prevents most overlaps, but this covers
        the same instant from the Controller side (d11 analogue).

        ``target_alt``/``max_s`` default to the emergency band's
        ``exit_above_alt``/``max_climb_s``; the mission-start prologue passes
        its own operating-altitude target (3.2c).

        Termination: ``confirm_reads`` consecutive FRESH telemetry reads at or
        above the target (a fresh read = the stable value's timestamp
        advanced; a stalled analyzer can never end the climb early — the d5
        lesson), an eject or evade starting mid-climb, or the unconditional
        duration backstop. The mission is never touched.

        @relation(FR-007, scope=function)
        """
        if self._climbing.is_set():
            logger.debug("Controller: climb_mode already in progress")
            return
        if self._ejecting.is_set():
            logger.info("Controller: climb suppressed — eject in progress")
            return
        if self._missile_evading.is_set():
            logger.info("Controller: climb suppressed — missile evade in progress")
            return
        exit_alt = target_alt if target_alt is not None else self._climb_exit_alt
        if exit_alt is None:
            logger.warning("Controller: climb_mode disabled — exit_above_alt unset")
            return
        cap_s = float(max_s) if max_s is not None else self._climb_max_s
        self._climbing.set()
        self._climb_stop.clear()
        logger.info("\033[95m⬆️  CLIMB — holding nose up + afterburner "
                    "(target alt %.0f, cap %.0fs)\033[0m", float(exit_alt), cap_s)

        def _run():
            try:
                if not self._simulate_os_input and not keyboard_module:
                    logger.error("Controller: keyboard library not available for climb_mode")
                    return
                self._run_climb_hold(float(exit_alt), cap_s,
                                     fuel_floor_pct=float(fuel_floor_pct),
                                     exit_lead_s=float(exit_lead_s))
            finally:
                self._climbing.clear()

        self._climb_thread = threading.Thread(target=_run, daemon=True)
        self._climb_thread.start()

    def _climb_key(self, key: str, press: bool, action: str = "climb"):
        """Press/release one climb-family key, honoring simulate mode."""
        if self._simulate_os_input:
            self._record_action_intent(
                "key_press" if press else "key_release", key=key, action=action)
            return
        if not keyboard_module:
            return
        try:
            (keyboard_module.press if press else keyboard_module.release)(key)
        except Exception:
            if press:
                logger.exception("Controller: %s press failed for '%s'", action, key)

    def start_spawn_guard(self):
        """ADR 076 d1: hold NOSE_UP from death detection until the spawn
        hands off — the aircraft's first frames of life are already pitching
        up, closing the reaction gap no perception-gated tactic can close.

        Called by the respawn flow when it latches a death. Non-blocking and
        idempotent while the thread is alive (the ADR 070 d8 pattern). Inert
        by construction while the respawn screen is up: flight keys do
        nothing while the aircraft does not exist.
        """
        if not self._spawn_guard_enabled:
            return
        if self._spawn_guarding.is_set():
            logger.debug("Controller: spawn guard already in progress")
            return
        self._spawn_guarding.set()
        self._sg_stop.clear()
        self._sg_alive_deadline = None
        logger.info("\033[95m🛫 SPAWN GUARD — holding nose up until the "
                    "respawn hands off (cap %.0fs)\033[0m", self._sg_max_hold_s)

        def _run():
            try:
                if not self._simulate_os_input and not keyboard_module:
                    logger.error("Controller: keyboard library not available for spawn guard")
                    return
                self._run_spawn_guard()
            finally:
                self._spawn_guarding.clear()

        self._sg_thread = threading.Thread(target=_run, daemon=True)
        self._sg_thread.start()

    def is_spawn_guarding(self) -> bool:
        return self._spawn_guarding.is_set()

    def notify_spawn_alive(self):
        """ADR 076 d2: health returned in battle — start the guard's
        release-overlap window (covers the tree's re-selection latency so
        pitch input never gaps between guard and Climb tactic)."""
        if self._spawn_guarding.is_set() and self._sg_alive_deadline is None:
            self._sg_alive_deadline = time.time() + self._sg_release_overlap_s
            logger.info("Controller: spawn guard — alive, releasing in %.1fs",
                        self._sg_release_overlap_s)

    def _run_spawn_guard(self):
        """Thread body for the spawn-attitude guard (ADR 076 d1/d2, revised
        ADR 078: pulsed application + telemetry handoff).

        Applies NOSE_UP in ``pulse_s``/``observe_s`` pulses under the
        programmatic bracket ('i' is a watched manual-takeover key) — a
        continuous hold looped the live aircraft at spawn (2026-08-17 14:22:
        alive detection lags the spawn instant by seconds, and held nose-up
        at spawn speed rolled a 180 and flew out of the map). Pulses bound
        the rotation; on the inert respawn screen the duty cycle costs
        nothing.

        Releases on the first of: a FRESH telemetry sample (advancing
        stable-value timestamp = the HUD is rendering = the aircraft exists
        — beats health-confirm by ~1.5-2 s, ADR 078 d2), the alive handoff
        (notify_spawn_alive + overlap, the fallback when telemetry never
        freshens), an eject or evade starting, the FSM leaving GAME_BATTLE,
        manual takeover (the SAF-001 handler sets _sg_stop), program exit,
        or the unconditional max-hold backstop.

        The physical key-up is ownership-aware: if a climb hold is active at
        release time the OS-level release is skipped — the climb thread owns
        the key state and its own finally block releases it; releasing here
        would yank a pulse in progress (the ADR 076 d2 rule).

        @relation(SAF-001, scope=function)
        @relation(SAF-009, scope=function)
        """
        entry_ts = time.time()
        exit_reason = "stopped"
        bracketed = NOSE_UP_KEY in _WATCHED_MANEUVER_KEYS
        nose_held = False
        pulse_until = 0.0
        observe_until = 0.0
        # ADR 078 d2 baseline: any telemetry timestamp NEWER than this means
        # the HUD came back — the aircraft spawned.
        baseline_ts = self._telemetry_stable_ts()
        if bracketed:
            self._inc_programmatic_key(NOSE_UP_KEY)
        try:
            while True:
                now = time.time()
                # Pulse state machine first, so the very first iteration
                # presses immediately (observe_until starts at 0).
                if nose_held and now >= pulse_until:
                    self._climb_key(NOSE_UP_KEY, press=False, action="spawn_guard")
                    nose_held = False
                    observe_until = now + self._sg_observe_s
                elif not nose_held and now >= observe_until:
                    self._climb_key(NOSE_UP_KEY, press=True, action="spawn_guard")
                    nose_held = True
                    pulse_until = now + self._sg_pulse_s
                if self._sg_stop.wait(timeout=0.25):
                    break
                if self._exit_event is not None and self._exit_event.is_set():
                    exit_reason = "exit"
                    break
                # ADR 076 d4: eject and evade own the airframe outright.
                if self._ejecting.is_set() or self._missile_evading.is_set():
                    exit_reason = "tactic_preempt"
                    break
                if self._analyzer is not None:
                    _st = getattr(self._analyzer, "game_state", None)
                    if isinstance(_st, GameState) and _st != GameState.GAME_BATTLE:
                        exit_reason = "state_exit"
                        break
                # ADR 078 d2: fresh telemetry = the aircraft exists. Release
                # now; the Climb tactic takes pitch with its rate ceiling.
                fresh_ts = self._telemetry_stable_ts()
                if (fresh_ts is not None
                        and (baseline_ts is None or fresh_ts > baseline_ts)):
                    exit_reason = "telemetry_handoff"
                    break
                now = time.time()
                deadline = self._sg_alive_deadline
                if deadline is not None and now >= deadline:
                    exit_reason = "alive_handoff"
                    break
                if now - entry_ts >= self._sg_max_hold_s:
                    # SAF-009: the cap firing means no alive/state signal ever
                    # arrived — a perception fault, not a normal exit.
                    logger.warning(
                        "Controller: spawn guard max hold (%.0fs) reached — "
                        "releasing without handoff", self._sg_max_hold_s)
                    exit_reason = "max_hold"
                    break
        finally:
            _release_started = time.time()
            if not self._climbing.is_set():
                self._climb_key(NOSE_UP_KEY, press=False, action="spawn_guard")
            else:
                logger.info("Controller: spawn guard — climb hold owns the "
                            "pitch key, skipping OS-level release")
            _release_span = time.time() - _release_started
            if bracketed:
                self._arm_release_grace(NOSE_UP_KEY, span_s=_release_span)
                self._dec_programmatic_key(NOSE_UP_KEY)
        logger.info("Controller: spawn guard complete (%s, %.1fs)",
                    exit_reason, time.time() - entry_ts)

    def _telemetry_stable_ts(self) -> "float | None":
        """Timestamp of the analyzer's current telemetry stable value, or
        None when unavailable (no analyzer, no snapshot, stale double)."""
        if self._analyzer is None:
            return None
        get_telemetry = getattr(self._analyzer, "get_telemetry", None)
        if get_telemetry is None:
            return None
        try:
            snap = get_telemetry()
        except Exception:
            return None
        if snap is None or not snap.altitude_fresh():
            return None
        alt_signal = snap.altitude
        if alt_signal is None or alt_signal.stable_value is None:
            return None
        return alt_signal.ts

    def _run_climb_hold(self, exit_alt: float, cap_s: float,
                        fuel_floor_pct: float = 0.0,
                        exit_lead_s: float = 0.0):
        """Thread body for climb_mode: pulse-and-observe pitch, poll altitude.

        AFTERBURNER is held while fuel stays above ``fuel_floor_pct``
        (ADR 075): the sustain climb passes the evade reserve (so a missile
        alert always finds burner fuel waiting), the emergency climb passes 0
        (terrain outranks the reserve). At the floor the key is released —
        holding at 0% blocks the game's recharge — and re-pressed only after
        the rearm margin refills. Unknown fuel changes nothing (freeze
        policy). NOSE_UP is applied in ``pitch_pulse_s`` pulses and re-applied
        only after ``pulse_observe_s`` AND only while the telemetry climb rate
        is below ``min_climb_rate`` or unknown — a continuously held nose-up
        LOOPS the aircraft instead of climbing it (2026-08-15 20:24 evidence:
        60 s held, altitude oscillated 1650-2400 with zero net gain). The
        eject dive controller's pulse/observe pattern, inverted.

        @relation(SAF-001, scope=function)
        @relation(SAF-008, scope=function)
        """
        entry_ts = time.time()
        exit_reason = "stopped"
        last_counted_ts = 0.0
        confirm_streak = 0
        pitch_held: "str | None" = None
        pulse_until = 0.0
        observe_until = 0.0
        last_rate = None
        last_angle = None   # ADR 081: flight-path angle from the same sample
        prev_angle = None   # ADR 086 d7: previous sample, for the pitch rate
        prev_angle_ts = None
        pitch_rate = None   # deg/s between the last two angle samples
        ab_held = False
        above_target = False   # ADR 083 d3: latches on the first at-target read

        # NOSE_UP and NOSE_DOWN (ADR 076 d3 ceiling) are watched maneuver
        # keys — same programmatic bracket as the evade hold (d4), held
        # across all pulses so XTest auto-repeats are never read as a manual
        # takeover.
        def _at_pitch_ceiling() -> bool:
            """True when the nose is at, or is predicted to reach, the ceiling.

            ADR 086 d7. Telemetry lands roughly every 3s while a lit burner
            rotates the airframe at ~11deg/s, so testing the CURRENT angle
            against the ceiling always reacts a full sample too late: a
            relight at +57deg was at +90deg before the next read (2026-08-21
            09:40:53). Same lead-prediction pattern ADR 083 d1 applies to
            altitude, for the same reason.

            Falls back to the current angle when no rate is known yet.
            """
            if self._climb_max_pitch_deg is None or last_angle is None:
                return False
            ceiling = float(self._climb_max_pitch_deg)
            if last_angle >= ceiling:
                return True
            if pitch_rate is None or pitch_rate <= 0:
                return False
            return last_angle + pitch_rate * self._climb_pitch_lead_s >= ceiling

        guarded_keys = tuple(k for k in (NOSE_UP_KEY, NOSE_DOWN_KEY, AFTERBURNER_KEY)
                             if k in _WATCHED_MANEUVER_KEYS)
        for _key in guarded_keys:
            self._inc_programmatic_key(_key)
        try:
            fuel = self._read_fuel_pct()
            if fuel is None or fuel > fuel_floor_pct:
                self._climb_key(AFTERBURNER_KEY, press=True)
                ab_held = True
            else:
                logger.info(
                    "Controller: climb — fuel %d%% at/below floor %.0f%% — "
                    "starting without afterburner", fuel, fuel_floor_pct)

            while not self._climb_stop.wait(timeout=0.25):
                if self._exit_event is not None and self._exit_event.is_set():
                    break
                # ADR 075 burner gate: release at the floor (a held key at 0%
                # blocks recharge; the sustain floor keeps the evade reserve),
                # re-press only after the rearm margin refills.
                fuel = self._read_fuel_pct()
                if fuel is not None:
                    _incoming = self._incoming_now()
                    if ab_held and fuel <= fuel_floor_pct and not _incoming:
                        self._climb_key(AFTERBURNER_KEY, press=False)
                        ab_held = False
                        logger.info(
                            "Controller: climb — fuel %d%% reached floor %.0f%% "
                            "— afterburner released (recharge/reserve)",
                            fuel, fuel_floor_pct)
                    elif not ab_held and _incoming and fuel > 0:
                        # ADR 088: outrunning a missile outranks every reserve
                        # policy. Overrides the ADR 075 floor and the ADR 083 d3
                        # above-target cut, down to (not including) empty —
                        # at 0% the burner produces no thrust and a held key
                        # blocks recharge, so that ADR 075 limit still stands.
                        self._climb_key(AFTERBURNER_KEY, press=True)
                        ab_held = True
                        logger.info(
                            "Controller: climb — INCOMING, afterburner forced on "
                            "(fuel %d%%, reserve floor %.0f%% overridden)",
                            fuel, fuel_floor_pct)
                    elif (not ab_held
                            and not above_target   # ADR 083 d3: never relight above target
                            # ADR 086 d6: nor while over-angled. ADR 083 d3
                            # found "the pitch ceiling fighting a lit burner"
                            # is what strands high-angle stretches; the target
                            # gate only closes that for the above-target case.
                            # Below target and over the ceiling is the same
                            # trap: relighting at +64deg drove 64->90deg and a
                            # speed collapse 1241->392 (2026-08-21 08:44:50).
                            and not _at_pitch_ceiling()   # ADR 086 d7
                            and fuel >= fuel_floor_pct + self._fuel_rearm_margin):
                        self._climb_key(AFTERBURNER_KEY, press=True)
                        ab_held = True
                        logger.info(
                            "Controller: climb — fuel recovered to %d%% — "
                            "afterburner re-engaged", fuel)
                # Yield the airframe to higher-priority tactics that started
                # after this hold (the ADR 070 d11 time-asymmetry).
                if self._ejecting.is_set():
                    logger.info("Controller: climb — eject started, releasing keys")
                    exit_reason = "eject_preempt"
                    break
                if self._missile_evading.is_set():
                    logger.info("Controller: climb — missile evade started, releasing keys")
                    exit_reason = "evade_preempt"
                    break
                # SAF-001 backstop: the takeover handler stops this hold, but
                # the FSM can leave GAME_BATTLE without it (a forced state
                # reset, a match end) — a climb must never keep flying an
                # airframe the state machine says wingman no longer owns.
                if self._analyzer is not None:
                    _st = getattr(self._analyzer, "game_state", None)
                    if isinstance(_st, GameState) and _st != GameState.GAME_BATTLE:
                        logger.info("Controller: climb — game state %s, releasing keys",
                                    _st.name)
                        exit_reason = "state_exit"
                        break
                now = time.time()
                if now - entry_ts >= cap_s:
                    logger.warning(
                        "Controller: climb max duration (%.0fs) reached — "
                        "releasing without altitude confirmation", cap_s)
                    exit_reason = "max_climb"
                    break

                # Pitch pulse state machine (never blocks the poll cadence).
                # ADR 081 d1 first: at or above max_pitch_deg the aircraft is
                # over-angled and the RATE floor gives the wrong answer (near
                # vertical, speed bleeds, the rate decays, and the floor
                # would pulse MORE nose-up — the fly-backwards-out-of-map
                # mechanism, stall observed at speed 26 / 9250 m). Then
                # ADR 076 d3 two-sided rate authority: below min_climb_rate
                # → nose-up; above max_climb_rate → nose-down; between the
                # bands no input. Unknown angle/rate keeps legacy behavior.
                if pitch_held is not None and now >= pulse_until:
                    self._climb_key(pitch_held, press=False)
                    pitch_held = None
                    observe_until = now + self._climb_observe_s
                elif pitch_held is None and now >= observe_until:
                    if _at_pitch_ceiling():   # ADR 086 d7 (was: current angle only)
                        pitch_held = NOSE_DOWN_KEY
                    elif last_rate is None or last_rate < self._climb_min_rate:
                        pitch_held = NOSE_UP_KEY
                    elif (self._climb_max_rate is not None
                            and last_rate > float(self._climb_max_rate)):
                        pitch_held = NOSE_DOWN_KEY
                    if pitch_held is not None:
                        self._climb_key(pitch_held, press=True)
                        pulse_until = now + self._climb_pulse_s
                        logger.debug(
                            "Controller: climb pitch pulse (%s, rate=%s, angle=%s)",
                            "up" if pitch_held == NOSE_UP_KEY else "down",
                            last_rate, last_angle)

                if self._analyzer is None:
                    continue
                try:
                    snap = self._analyzer.get_telemetry()
                except Exception:
                    logger.exception("Controller: climb telemetry poll failed")
                    continue
                if snap is None or not snap.altitude_fresh():
                    continue   # blind: neither counts nor resets (freeze)
                alt_signal = snap.altitude
                alt = alt_signal.stable_value
                sig_ts = alt_signal.ts
                if alt is None or sig_ts is None or sig_ts <= last_counted_ts:
                    continue   # stale sample already counted (d5)
                last_counted_ts = sig_ts
                last_rate = getattr(alt_signal, "rate", None)
                _angle_fn = getattr(snap, "pitch_angle_deg", None)
                _new_angle = _angle_fn() if callable(_angle_fn) else None
                # ADR 086 d7: pitch rate across consecutive angle samples.
                if _new_angle is not None:
                    if prev_angle is not None and prev_angle_ts is not None:
                        _dt = sig_ts - prev_angle_ts
                        if _dt > 0:
                            pitch_rate = (_new_angle - prev_angle) / _dt
                    prev_angle, prev_angle_ts = _new_angle, sig_ts
                last_angle = _new_angle
                # ADR 083 d3: at or above target, thrust stops — one fresh
                # read, no debounce. Removing the energy source is the
                # physical fix for a zoom climb; the pitch ceiling fighting
                # a lit burner is what left 59% of high-angle stretches
                # stalled (2026-08-19). Independent of the ADR 075 fuel
                # gate, which keeps its floor/rearm behaviour below target.
                if alt >= exit_alt and not above_target:
                    above_target = True
                    if ab_held and self._incoming_now():
                        # ADR 088: this is the cut observed stranding an evade
                        # (2026-08-22 01:47:39, burner cut 1.7s after the
                        # manoeuvre limit released with incoming still present).
                        logger.info(
                            "Controller: climb — reached target alt %.0f but "
                            "INCOMING — holding afterburner", exit_alt)
                    elif ab_held:
                        self._climb_key(AFTERBURNER_KEY, press=False)
                        ab_held = False
                        logger.info(
                            "Controller: climb — reached target alt %.0f "
                            "— afterburner cut", exit_alt)
                # ADR 083 d1: compare the PREDICTED altitude at the next
                # sample. Telemetry lands every ~3 s and a burner climb
                # covers ~1350 m in that time, so testing the current value
                # against the target builds ~2700 m of overshoot into the
                # exit (measured median 2401 m). Releasing a sample early
                # lets momentum carry the aircraft to the target instead of
                # past it. Unknown rate contributes nothing (freeze policy)
                # and the confirm_reads debounce still applies.
                predicted = alt
                if exit_lead_s > 0.0 and last_rate is not None:
                    predicted = alt + (last_rate * exit_lead_s)
                if predicted >= exit_alt:
                    confirm_streak += 1
                    if confirm_streak >= self._climb_confirm_reads:
                        exit_reason = "altitude_recovered"
                        break
                else:
                    confirm_streak = 0
        finally:
            _release_started = time.time()
            self._climb_key(NOSE_UP_KEY, press=False)
            self._climb_key(AFTERBURNER_KEY, press=False)
            # ADR 086 d1 / SAF-010: nose down into the flyable band BEFORE
            # going neutral. Burner off first (above), so the push is not
            # fighting thrust — the ADR 083 d3 finding.
            self._climb_exit_push()
            self._climb_key(NOSE_DOWN_KEY, press=False)
            _release_span = time.time() - _release_started
            for _key in guarded_keys:
                self._arm_release_grace(_key, span_s=_release_span)
                self._dec_programmatic_key(_key)
        logger.info("Controller: climb complete (%s, %.1fs)",
                    exit_reason, time.time() - entry_ts)

    def is_climbing(self) -> bool:
        """True while a climb_mode hold is in progress
        (ADR 073 3.2b — the Climb leaf's is_running_fn)."""
        return self._climbing.is_set()

    def _incoming_now(self) -> bool:
        """True while an incoming-missile alert is on screen (ADR 088).

        Cheap cache read — the analyzer already maintains this for the evade
        tactic; this is a second consumer, not a second detector.

        @relation(SAF-013, scope=function)
        """
        if self._analyzer is None:
            return False
        try:
            detected, _, _ = self._analyzer.get_incoming_cache_result()
            return bool(detected)
        except Exception:
            return False   # a diagnostic read must never break the climb loop

    def _climb_exit_push(self) -> str:
        """Nose down into the flyable band before the climb releases (ADR 086 d1).

        Bounded exactly as ADR 069 bounds the eject rotation — impulse plus
        observation gap, a pulse budget — because that ADR established that a
        continuously held pitch input mushes the airframe rather than rotating
        it. Stops on the first sample showing the angle inside the band.

        Blind operation is deliberate: with no telemetry this runs ONE pulse
        and returns. An unverified small nose-down is safer than an unverified
        ballistic climb, which is what the pre-ADR-086 exit left behind.

        Returns the exit reason, for the caller's log line.

        @relation(SAF-010, scope=function)
        """
        target = self._climb_exit_pitch_deg
        if target is None:
            return "disabled"
        target = float(target)
        pulses = 0
        while pulses < max(1, self._climb_exit_max_pulses):
            snap = self._eject_telemetry()
            angle = None
            if snap is not None:
                fn = getattr(snap, "pitch_angle_deg", None)
                angle = fn() if callable(fn) else None
            if angle is not None and angle <= target:
                if pulses:
                    logger.info("Controller: climb exit — nose at %+.0fdeg "
                                "(band %+.0f) after %d pulse(s)",
                                angle, target, pulses)
                return "in_band"
            self._climb_key(NOSE_DOWN_KEY, press=True)
            interrupted = self._climb_stop.wait(timeout=self._climb_exit_pulse_s)
            self._climb_key(NOSE_DOWN_KEY, press=False)
            pulses += 1
            if interrupted or (self._exit_event is not None
                               and self._exit_event.is_set()):
                return "interrupted"
            if angle is None:
                # Blind: one pulse only, then hand back.
                logger.info("Controller: climb exit — no telemetry, "
                            "single %.1fs nose-down pulse applied",
                            self._climb_exit_pulse_s)
                return "blind_single_pulse"
            # Observation gap: never act again before the airframe has had a
            # telemetry refresh to respond (ADR 069 d2).
            if self._climb_stop.wait(timeout=self._climb_exit_pulse_s):
                return "interrupted"
        logger.warning("Controller: climb exit — pitch budget (%d pulses) "
                       "exhausted, releasing anyway", self._climb_exit_max_pulses)
        return "budget_exhausted"

    def _read_stable_altitude(self) -> "float | None":
        """Fresh telemetry stable altitude, or None when unreadable."""
        if self._analyzer is None:
            return None
        try:
            snap = self._analyzer.get_telemetry()
        except Exception:
            return None
        if snap is None or not snap.altitude_fresh():
            return None
        return snap.altitude.stable_value

    def _read_fuel_pct(self) -> "int | None":
        """Fresh afterburner fuel percentage, or None when unreadable (ADR 075)."""
        if self._analyzer is None:
            return None
        try:
            return self._analyzer.get_afterburner_fuel_pct()
        except Exception:
            return None

    def start_weapon_loop(self, interval: float | None = None):
        """Start continuously firing the active weapon in a loop.

        Args:
            interval: Time between shots in seconds (default 0.2)
        """
        if self._weapon_loop_active:
            logger.debug("Controller: weapon loop already running")
            return

        if interval is not None:
            self._weapon_loop_interval = float(interval)

        self._weapon_loop_active = True
        self._weapon_loop_stop.clear()

        def _loop():
            logger.info("Controller: weapon loop started (interval=%.2fs)", self._weapon_loop_interval)
            try:
                while True:
                    try:
                        self._execute_key_press(
                            FIRE_ACTIVE_WEAPON,
                            hold_seconds=0.1,
                            block=True,
                            action_name="fire_active_weapon",
                            ignore_cancel=True,
                        )
                    except Exception as e:
                        logger.warning("Controller: weapon loop fire failed: %s", e)
                    if self._weapon_loop_stop.wait(timeout=self._weapon_loop_interval):
                        break
            except Exception:
                logger.exception("Controller: weapon loop error")
            finally:
                self._weapon_loop_active = False
                logger.info("Controller: weapon loop stopped")

        self._weapon_loop_thread = threading.Thread(target=_loop, daemon=True)
        self._weapon_loop_thread.start()

    def stop_weapon_loop(self):
        """Stop the continuous weapon firing loop."""
        if not self._weapon_loop_active:
            logger.debug("Controller: weapon loop not running")
            return

        logger.info("Controller: stopping weapon loop")
        self._weapon_loop_stop.set()
        self._weapon_loop_active = False
        if self._weapon_loop_thread:
            self._weapon_loop_thread.join(timeout=2.0)
            self._weapon_loop_thread = None

    def toggle_weapon_loop(self, _event=None):
        """Toggle the weapon loop on/off. Bound to hotkey 'x'.

        Accepts the key event the listener passes. On Linux, add_hotkey routes
        to on_press_key, whose callbacks are invoked as cb(event) — so the
        no-argument form raised "takes 1 positional argument but 2 were given"
        on every press and the toggle never fired (2026-08-07 log). Kept
        optional so direct calls still work.
        """
        logger.debug("Controller: toggle_weapon_loop called (current state: %s)", self._weapon_loop_active)
        if self._weapon_loop_active:
            logger.info("Controller: toggling weapon loop OFF")
            self.stop_weapon_loop()
        else:
            logger.info("Controller: toggling weapon loop ON")
            self.start_weapon_loop()

    def _interruptible_sleep(self, seconds: float, check_interval: float = 1.0) -> bool:
        """Sleep in intervals and exit early when mission cancellation is requested.

        Returns:
            True if full duration elapsed, False if interrupted by cancellation.
        """
        remaining = float(seconds)
        while remaining > 0:
            interval = min(check_interval, remaining)
            if self._mission_cancel.wait(timeout=interval):
                return False
            remaining -= interval
        return True

    def mission_loiter(self):
        """This mission sequence performs a predefined set of maneuvers for the Aaarvark, it flies up and tries to stay up
        Compatible Jets: F111, F-14, Mig-23, J20
        """
        # Check if mission is already running
        acquired = self._mission_lock.acquire(blocking=False)
        if not acquired:
            logger.debug("Controller: mission already in progress, skipping")
            return

        logger.info("\033[92mController: mission_loiter - starting mission sequence\033[0m")
        self._mission_complete.clear()
        self._mission_cancel.clear()

        def _mission_runner():
            try:
                # Execute mission maneuvers (maneuvers log their own activity)
                self.nose_up(2.0)
                if self._mission_cancel.is_set():
                    logger.info("Controller: mission cancelled after nose_up")
                    return
                self.wingsweep()
                if self._mission_cancel.is_set():
                    logger.info("Controller: mission cancelled after wingsweep")
                    return
                self.afterburner(10.0)
                if self._mission_cancel.is_set():
                    logger.info("Controller: mission cancelled after afterburner")
                    return
                self.afterburner(10.0)
                if self._mission_cancel.is_set():
                    logger.info("Controller: mission cancelled after afterburner")
                    return
                self.wingsweep()
                if self._mission_cancel.is_set():
                    logger.info("Controller: mission cancelled after wingsweep")
                    return
                self.roll_right(4)
                if self._mission_cancel.is_set():
                    logger.info("Controller: mission cancelled after roll_right")
                    return
                self.afterburner(10)
                if self._mission_cancel.is_set():
                    logger.info("Controller: mission cancelled after afterburner")
                    return
                self.deploy_flares()
                if self._mission_cancel.is_set():
                    logger.info("Controller: mission cancelled after deploy_flares")
                    return
                self.roll_left(10)
                if self._mission_cancel.is_set():
                    logger.info("Controller: mission cancelled after roll_left")
                    return
                self.deploy_flares()
                if self._mission_cancel.is_set():
                    logger.info("Controller: mission cancelled after deploy_flares")
                    return
                self.roll_right(30)
                if self._mission_cancel.is_set():
                    logger.info("Controller: mission cancelled after roll_right")
                    return
                self.roll_left(30)
                if self._mission_cancel.is_set():
                    logger.info("Controller: mission cancelled")
                    return
                #self.nose_down(4.0)
                #time.sleep(10.0)  # additional wait time to stabilize
                logger.info("\033[91mController: mission_loiter - sequence complete\033[0m")
            except Exception:
                logger.exception("Controller: mission_loiter failed")
            finally:
                self._mission_complete.set()
                if self._mission_lock.locked():
                    self._mission_lock.release()

        mission_a = threading.Thread(target=_mission_runner, daemon=True)
        mission_a.start()

        # Wait for mission to complete or exit requested
        while not self._mission_complete.wait(timeout=0.05):
            if self._exit_event and self._exit_event.is_set():
                logger.info("Controller: exit requested, aborting mission wait")
                self.cancel_mission()
                break

    def mission_j20(self):
        """Fully adaptive J20 mission (ADR 075): the behavior tree owns every
        in-battle decision — sustained climb to operating altitude while armed,
        engage geometry, missile evade, eject. The mission thread contributes
        only the search-and-destroy loops (padlock + weapon fire, which keep
        running through climbs and evades) and holds the mission-running state
        until cancelled. No scripted maneuver, afterburner schedule, or fixed
        mission window remains; the mission ends via cancel — respawn restart,
        eject, manual takeover, or match end.
        Compatible Jets: J20
        """
        # Check if mission is already running
        acquired = self._mission_lock.acquire(blocking=False)
        if not acquired:
            logger.warning("\033[91mController: mission_j20 already in progress, skipping (lock held)\033[0m")
            return

        logger.info("\033[92mController: mission_j20 - starting mission sequence (lock acquired)\033[0m")
        self._mission_complete.clear()
        self._mission_cancel.clear()

        def _mission_runner():
            try:
                self.start_search_and_destroy_loop()
                logger.info(
                    "Controller: mission_j20 - adaptive mission running "
                    "(S&D loops up, behavior tree owns tactics)")
                while not self._mission_cancel.wait(timeout=0.5):
                    if self._exit_event is not None and self._exit_event.is_set():
                        logger.info("Controller: mission_j20 - exit requested")
                        break
                logger.info("Controller: mission_j20 - cancelled, stopping loops")
                self.stop_search_and_destroy_loop()
            except Exception:
                logger.exception("Controller: mission_j20 failed")
                self.stop_search_and_destroy_loop()
            finally:
                self._mission_complete.set()
                if self._mission_lock.locked():
                    self._mission_lock.release()
                    logger.info("\033[91mController: mission_j20 - lock released\033[0m")

        mission_a = threading.Thread(target=_mission_runner, daemon=True)
        mission_a.start()

        # Wait for mission to complete or exit requested
        while not self._mission_complete.wait(timeout=0.05):
            if self._exit_event and self._exit_event.is_set():
                logger.info("Controller: exit requested, aborting mission wait")
                self.cancel_mission()
                break

        # Wait for the mission runner thread to fully exit
        mission_a.join(timeout=2.0)

        # Small delay to let keyboard library settle after key presses
        time.sleep(0.2)

        logger.info("\033[91mController: mission_j20 - method exiting\033[0m")

    def click_grid_region(self, region_num: int, grid_rows: int = 8, grid_cols: int = 8, block: bool = False, count: int = 6, region_name: str = None):
        """Move the mouse to the center of a grid region and left-click it.

        Args:
            region_num: 1-based region number (row-major, left-to-right top-to-bottom).
            grid_rows: Number of grid rows (default 8).
            grid_cols: Number of grid columns (default 8).
            block: If True run in the calling thread; otherwise spawn a daemon thread.
            count: Number of times to click the region. When count > 1 a final click on
                   the ready button (lobby/continue button) is also performed.
            region_name: Human-readable name for the region, used in log messages.
        """
        def _do_click():
            if self._simulate_os_input:
                label = region_name if region_name else str(region_num)
                self._record_action_intent(
                    "click_grid_region",
                    region_num=int(region_num),
                    region_name=label,
                    count=int(count),
                    grid_rows=int(grid_rows),
                    grid_cols=int(grid_cols),
                )
                return
            try:
                if self._capture is None:
                    logger.error("Controller: click_grid_region - no capture reference")
                    return
                region = self._capture.region
                cap_w, cap_h = region[2], region[3]
                cell_w = cap_w / grid_cols
                cell_h = cap_h / grid_rows
                row_idx = (region_num - 1) // grid_cols
                col_idx = (region_num - 1) % grid_cols
                label = region_name if region_name else str(region_num)

                if sys.platform != "win32":
                    # Linux: compute absolute coords from game window offset
                    offset = None
                    for _attempt in range(3):
                        offset = self._capture.game_screen_offset
                        if offset is not None:
                            break
                        time.sleep(0.05)
                    if offset is None:
                        logger.error("click_grid_region: game window offset not known yet (3 retries)")
                        return
                    game_ox, game_oy = offset
                    abs_x = int(game_ox + (col_idx + 0.5) * cell_w)
                    abs_y = int(game_oy + (row_idx + 0.5) * cell_h)
                    logger.info("\033[93m📋 Clicking %s at (%d, %d) [game offset %d,%d] x%d\033[0m",
                                label, abs_x, abs_y, game_ox, game_oy, count)
                    if _may_inject("click"):
                        _linux_click(abs_x, abs_y, count)
                    if count > 1 and self._ready_button_region:
                        rbn = self._ready_button_region
                        row_rb = (rbn - 1) // grid_cols
                        col_rb = (rbn - 1) % grid_cols
                        x_rb = int(game_ox + (col_rb + 0.5) * cell_w)
                        y_rb = int(game_oy + (row_rb + 0.5) * cell_h)
                        logger.info("\033[93m📋 Clicking ready_button at (%d, %d)\033[0m", x_rb, y_rb)
                        _linux_click(x_rb, y_rb)
                        if self._analyzer is not None:
                            self._analyzer.trigger_event("manual_reset")
                            logger.info("\033[93m📋 Ready button (region %d) clicked → GAME_LOBBY\033[0m", self._ready_button_region)
                    return

                # Windows: use win32api
                with mss() as sct:
                    monitors = sct.monitors
                    monitor_index = self._capture.monitor_index
                    if monitor_index < 1 or monitor_index >= len(monitors):
                        logger.error("Controller: click_grid_region - monitor index %d out of range", monitor_index)
                        return
                    mon = monitors[monitor_index]
                    abs_left = mon["left"] + region[0]
                    abs_top = mon["top"] + region[1]
                abs_x = int(abs_left + (col_idx + 0.5) * cell_w)
                abs_y = int(abs_top + (row_idx + 0.5) * cell_h)
                logger.info("\033[93m📋 Clicking %s at (%d, %d) [monitor %d offset %d,%d] x%d\033[0m",
                            label, abs_x, abs_y, monitor_index, mon["left"], mon["top"], count)
                def _raw_click(x, y):
                    ctypes.windll.user32.SetCursorPos(x, y)
                    time.sleep(0.05)
                    ctypes.windll.user32.mouse_event(0x0002, 0, 0, 0, 0)  # MOUSEEVENTF_LEFTDOWN
                    time.sleep(0.05)
                    ctypes.windll.user32.mouse_event(0x0004, 0, 0, 0, 0)  # MOUSEEVENTF_LEFTUP

                for i in range(count):
                    _raw_click(abs_x, abs_y)
                    if i < count - 1:
                        time.sleep(0.5)

                if count > 1 and self._ready_button_region:
                    rbn = self._ready_button_region
                    row_rb = (rbn - 1) // grid_cols
                    col_rb = (rbn - 1) % grid_cols
                    x_rb = int(abs_left + (col_rb + 0.5) * cell_w)
                    y_rb = int(abs_top + (row_rb + 0.5) * cell_h)
                    logger.info("\033[93m📋 Clicking ready_button at (%d, %d)\033[0m", x_rb, y_rb)
                    _raw_click(x_rb, y_rb)
                    if self._analyzer is not None:
                        self._analyzer.trigger_event("manual_reset")
                        logger.info("\033[93m📋 Ready button (region %d) clicked → GAME_LOBBY\033[0m", self._ready_button_region)
            except Exception:
                logger.exception("Controller: click_grid_region failed")

        if block:
            _do_click()
        else:
            threading.Thread(target=_do_click, daemon=True).start()

    def popup_click_allowed(self, popup: str, cooldown: float = 30.0) -> bool:
        """Return True if `popup` has not been clicked within `cooldown` seconds."""
        last = self._popup_last_clicked.get(popup, 0.0)
        return time.time() - last >= cooldown

    def record_popup_click(self, popup: str) -> None:
        """Record that `popup` was just clicked (starts its cooldown)."""
        self._popup_last_clicked[popup] = time.time()

    def click_crop(self, coords: "CropCoords", block: bool = False, count: int = 1, region_name: str = None):
        """Move the mouse to the centre of a named crop region and left-click it.

        Uses percentage-coordinate CropCoords (from crop_region.py) to derive
        the absolute screen position via crop_centre().

        Args:
            coords: CropCoords percentage-coordinate bounding box for the target region.
            block: If True run in the calling thread; otherwise spawn a daemon thread.
            count: Number of times to click the region (0.5s apart when count > 1).
            region_name: Human-readable label used in log messages.
        """
        def _do_click():
            if self._simulate_os_input:
                label = region_name or f"({coords.x1:.2f},{coords.y1:.2f})"
                self._record_action_intent(
                    "click_crop",
                    region_name=label,
                    count=int(count),
                    coords={"x1": coords.x1, "y1": coords.y1, "x2": coords.x2, "y2": coords.y2},
                )
                return
            try:
                if self._capture is None:
                    logger.error("Controller: click_crop - no capture reference")
                    return
                region = self._capture.region
                cap_w, cap_h = region[2], region[3]
                label = region_name or f"({coords.x1:.2f},{coords.y1:.2f})"

                if sys.platform != "win32":
                    # Linux: compute absolute coords from game window offset
                    offset = None
                    for _attempt in range(3):
                        offset = self._capture.game_screen_offset
                        if offset is not None:
                            break
                        time.sleep(0.05)
                    if offset is None:
                        logger.error("click_crop: game window offset not known yet (3 retries)")
                        return
                    game_ox, game_oy = offset
                    abs_x, abs_y = crop_centre(coords, cap_w, cap_h, game_ox, game_oy)
                    logger.info("\033[93m📋 Clicking %s at (%d, %d) [game offset %d,%d] x%d\033[0m",
                                label, abs_x, abs_y, game_ox, game_oy, count)
                    if _may_inject("click"):
                        _linux_click(abs_x, abs_y, count)
                    return

                # Windows: use win32api
                with mss() as sct:
                    monitors = sct.monitors
                    monitor_index = self._capture.monitor_index
                    if monitor_index < 1 or monitor_index >= len(monitors):
                        logger.error("Controller: click_crop - monitor index %d out of range", monitor_index)
                        return
                    mon = monitors[monitor_index]
                    abs_left = mon["left"] + region[0]
                    abs_top = mon["top"] + region[1]
                abs_x, abs_y = crop_centre(coords, cap_w, cap_h, abs_left, abs_top)
                logger.info("\033[93m📋 Clicking %s at (%d, %d) [monitor %d offset %d,%d] x%d\033[0m",
                            label, abs_x, abs_y, monitor_index, mon["left"], mon["top"], count)

                def _raw_click(x, y):
                    ctypes.windll.user32.SetCursorPos(x, y)
                    time.sleep(0.05)
                    ctypes.windll.user32.mouse_event(0x0002, 0, 0, 0, 0)  # MOUSEEVENTF_LEFTDOWN
                    time.sleep(0.05)
                    ctypes.windll.user32.mouse_event(0x0004, 0, 0, 0, 0)  # MOUSEEVENTF_LEFTUP

                for i in range(count):
                    _raw_click(abs_x, abs_y)
                    if i < count - 1:
                        time.sleep(0.5)
            except Exception:
                logger.exception("Controller: click_crop failed")

        if block:
            _do_click()
        else:
            threading.Thread(target=_do_click, daemon=True).start()

    def cancel_mission(self):
        """Request cancellation of any running mission.

        Sets the cancel flag which maneuvers poll and stops the standalone
        weapon loop. search_and_destroy_loop self-terminates when it sees
        _mission_cancel. Mission completion/lock release are finalized by
        the mission runner thread.
        """
        logger.info("\033[91mController: cancel_mission called\033[0m")
        self._mission_cancel.set()
        self.stop_weapon_loop()

    def close_all_requested(self) -> bool:
        """True once the operator's SECOND Backspace has arrived (ADR 099)."""
        return self._close_all_event.is_set()

    def wait_for_close_all(self, timeout=None) -> bool:
        """Block until the second Backspace, or `timeout`. True if it arrived."""
        return self._close_all_event.wait(timeout=timeout)

    def release_hotkeys(self) -> None:
        """Deregister keyboard hooks. Split out of cleanup() so standby can keep
        listening for the second Backspace after the session has ended."""
        if keyboard_module:
            try:
                keyboard_module.unhook_all()
                logger.info("Controller: all keyboard hooks deregistered")
            except ImportError as exc:
                logger.warning("Controller: keyboard unhook skipped — %s", exc)
            except Exception:
                logger.exception("Controller: keyboard unhook failed")

    def _manual_takeover_active(self) -> bool:
        """True while the operator holds the aircraft (SAF-001). Never raises —
        it gates every key press, so a failure here must not stop flares."""
        try:
            return (self._analyzer is not None
                    and self._analyzer.game_state == GameState.GAME_BATTLE_MANUAL)
        except Exception:
            return False

    def release_for_manual_takeover(self) -> None:
        """Hand the aircraft to the operator: stop every writer, release every
        key (SAF-001).

        The FSM transition alone is not enough. Tactic holds and loops run in
        their own threads with their own budgets, and a key already pressed
        stays pressed — the X server holds key state, not this process. Called
        from the GAME_BATTLE_MANUAL entry hook so it runs however takeover was
        reached.
        """
        self._eject_stop_reason = "manual takeover"
        self._eject_stop.set()
        self._me_stop.set()
        self._climb_stop.set()
        self._sg_stop.set()
        try:
            self.cancel_mission()
        except Exception:
            logger.exception("Controller: cancel_mission failed during takeover")
        for stop in (self.stop_search_and_destroy_loop,):
            try:
                stop()
            except Exception:
                logger.exception("Controller: loop stop failed during takeover")
        if keyboard_module and not self._simulate_os_input:
            for _key in INJECTABLE_KEYS:
                try:
                    keyboard_module.release(_key)
                except Exception:
                    logger.error("Controller: takeover release of %r failed — %s",
                                 _key, _LATCH_NOTE)
            logger.info("Controller: manual takeover — all injectable keys released")

    def operator_stop_requested(self) -> bool:
        """True when the operator stopped the session with Backspace (ADR 099).

        Distinct from `exit_requested`, which SIGTERM and the startup-stall exit
        also set. Only a deliberate operator stop tears the session down.
        """
        return self._operator_stop_event.is_set()

    def finish_round_requested(self) -> bool:
        """True while a deferred finish-round-then-exit is pending (ADR 094)."""
        return self._finish_round_event.is_set()

    def request_finish_round(self, requested: bool = True) -> None:
        """Set or clear the deferred exit. Exposed for tests and future callers."""
        self._finish_round_event.set() if requested else self._finish_round_event.clear()

    def is_mission_running(self) -> bool:
        """Return True when a mission thread currently holds the mission lock."""
        return self._mission_lock.locked()

    def is_mission_teardown_in_progress(self) -> bool:
        """True while a CANCELLED mission thread still holds the mission lock.

        Distinguishes "a mission is genuinely flying" (lock held, cancel clear)
        from "the lock is held only because a cancelled thread is unwinding"
        (lock held, cancel set — clears when the next mission starts). Callers
        deciding whether to retry a restart need the difference: retrying
        against teardown is correct; retrying against a live mission loops.
        """
        return self._mission_lock.locked() and self._mission_cancel.is_set()

    def capture_frame_age_s(self):
        """Seconds since the capture last produced a frame, or None if unknown.

        None means either no capture is wired, no frame has arrived yet, or the
        capture object does not track freshness (replay/test doubles) — callers
        must treat None as "no staleness evidence", not as stale.
        """
        age_fn = getattr(self._capture, "seconds_since_last_frame", None)
        return age_fn() if age_fn is not None else None

    def start_game_starting_loop(self):
        """Public orchestration entrypoint for the GAME_STARTING loop."""
        self._start_game_starting_loop()

    def is_auto_respawn_restart_enabled(self) -> bool:
        """Return whether automatic respawn restart is currently enabled."""
        return self._auto_respawn_restart

    def set_auto_respawn_restart(self, enabled: bool) -> None:
        """Enable or disable automatic restart after respawn."""
        self._auto_respawn_restart = bool(enabled)

    def stop_eject_sequence(self, reason: str = "respawn_detected") -> None:
        """Cancel an in-progress eject-and-dive sequence if one is active."""
        self._eject_stop_reason = reason
        self._eject_stop.set()

    def _set_last_mission(self, mission_name: str):
        with self._last_mission_lock:
            self._last_mission = mission_name
        self._auto_respawn_restart = True
        self._game_battle_since = time.time()
        if self._analyzer is not None:
            self._analyzer._last_battle_event_ts = time.time()
            logger.info("Controller: mission '%s' started → GAME_BATTLE", mission_name)

    def _start_game_starting_loop(self):
        """Background loop active in GAME_STARTING state.

        Every 5 seconds: press MISSION_J20_KEY and scan the good_luck region for 'Good Luck'.
        Once detected, wait good_luck_wait_s (interruptible on battle-alive
        evidence) then launch mission_j20.

        @relation(FR-003, scope=function)
        """
        # Clear any stale cancel from prior states (mirrors mission_j20 / mission_loiter pattern).
        # cancel_mission() is called on on_enter_GAME_LOBBY; without this clear the loop
        # would see the flag already set and exit immediately.
        self._mission_cancel.clear()

        good_luck_event = threading.Event()
        ocr_running = threading.Event()

        def _do_ocr_scan():
            """Run Good Luck OCR in background; sets good_luck_event on detection."""
            try:
                time.sleep(0.5)  # Allow 'Good Luck' screen to appear before capturing
                if self._capture is None:
                    return

                frame = self._capture.grab_from_thread()
                if self._analyzer is not None and self._analyzer.scan_region_for_good_luck(frame):
                    good_luck_event.set()
                    if self._on_good_luck_frame is not None:
                        try:
                            self._on_good_luck_frame(frame)
                        except Exception:
                            logger.exception("Controller: on_good_luck_frame callback error")
            except Exception:
                logger.exception("Controller: game_starting OCR scan error")
            finally:
                ocr_running.clear()

        def _in_starting():
            return (self._analyzer is not None
                    and self._analyzer.game_state == GameState.GAME_STARTING
                    and not self._mission_cancel.is_set())

        def _loop():
            logger.info("Controller: game_starting loop started - pressing '%s' key every 5s until 'Good Luck' detected", MISSION_J20_KEY)
            loop_start = time.time()
            max_wait = self._starting_max_wait_s  # safety timeout: GAME_STARTING → GAME_STARTING_STALLED if Good Luck never detected
            health_scan_armed = False
            try:
                while _in_starting():
                    # Press MISSION_J20_KEY every interval — unless capture has
                    # gone stale (display loss / pipeline stall): the game may no
                    # longer be on screen, so the press would land in whatever
                    # window is focused. The loop itself keeps running so the
                    # Good-Luck timeout can still move the FSM to stalled.
                    frame_age = self.capture_frame_age_s()
                    if (frame_age is not None
                            and frame_age > self._capture_stale_inject_s):
                        logger.warning(
                            "Controller: game_starting - no frame for %.1fs "
                            "(limit %.0fs) - suppressing '%s' press",
                            frame_age, self._capture_stale_inject_s, MISSION_J20_KEY)
                    elif self._simulate_os_input:
                        self._record_action_intent("key_tap", key=MISSION_J20_KEY, action="game_starting_loop")
                        logger.info("Controller: game_starting - simulated '%s' key tap", MISSION_J20_KEY)
                    elif keyboard_module:
                        # Bracket + grace so the XRecord echo of OUR OWN press is
                        # recognizable as programmatic. The 'u' hotkey used to
                        # dismiss echoes by FSM state (== GAME_STARTING), which
                        # also ate GENUINE resume presses whenever the FSM was
                        # wedged in GAME_STARTING (2026-08-01 02:55: five human
                        # presses in 3s all logged as "XTest echo ... ignoring").
                        self._inc_programmatic_key(MISSION_J20_KEY)
                        try:
                            keyboard_module.press_and_release(MISSION_J20_KEY)
                        finally:
                            self._arm_release_grace(MISSION_J20_KEY)
                            self._dec_programmatic_key(MISSION_J20_KEY)
                        logger.info("Controller: game_starting - pressed '%s' key", MISSION_J20_KEY)

                    # Start async OCR scan if one isn't already running
                    if self._capture is not None and not ocr_running.is_set():
                        ocr_running.set()
                        threading.Thread(target=_do_ocr_scan, daemon=True).start()

                    # 5-second interruptible wait; breaks early on Good Luck detection or state change.
                    # After 10 s gate: also arm health scan and check game_battle_alive each tick.
                    for _ in range(50):  # 50 * 0.1s = 5s
                        if good_luck_event.wait(timeout=0.1) or not _in_starting():
                            break
                        if not health_scan_armed and time.time() - loop_start >= 10.0:
                            health_scan_armed = True
                            if self._analyzer is not None:
                                self._analyzer.arm_starting_health_scan()
                                logger.info("Controller: game_starting health-scan fallback armed (10s gate)")
                        if health_scan_armed and self._analyzer is not None and self._analyzer.game_battle_alive:
                            logger.info(
                                "\033[92mController: game_battle_alive detected in GAME_STARTING "
                                "— launching mission immediately\033[0m")
                            self._analyzer.trigger_event("good_luck_detected")
                            self._set_last_mission("j20")
                            threading.Thread(target=self.mission_j20, daemon=True).start()
                            return

                    if not _in_starting():
                        # If cancel fired while FSM is still GAME_STARTING, push it to
                        # stalled — but not on program exit: shutdown cancels the
                        # mission too, and firing starting_timeout then only stamps a
                        # spurious STALLED warning into the log tail (observed
                        # 2026-08-17 12:52, Backspace during matchmaking).
                        if (self._mission_cancel.is_set()
                                and not (self._exit_event is not None
                                         and self._exit_event.is_set())
                                and self._analyzer is not None
                                and self._analyzer.game_state == GameState.GAME_STARTING):
                            self._analyzer.trigger_event("starting_timeout")
                        return

                    if time.time() - loop_start > max_wait:
                        logger.warning("Controller: game_starting timed out after %ds without 'Good Luck'", max_wait)
                        if self._analyzer is not None:
                            self._analyzer.trigger_event("starting_timeout")
                        return

                    if good_luck_event.is_set():
                        good_luck_wait = self._good_luck_wait_s
                        logger.info("\033[92mController: 'Good Luck' detected - waiting %ds before starting '%s' mission\033[0m", good_luck_wait, MISSION_J20_KEY)
                        # The wait is interruptible: game_battle_alive means the
                        # aircraft is already in the world, so there is nothing left
                        # to wait for. Previously this polled only _in_starting(),
                        # so no signal could shorten it — and nothing scanned the
                        # screen during the window at all (2026-08-05 review).
                        gl_start = time.time()
                        bypassed = False
                        for _ in range(int(good_luck_wait * 10)):  # N * 0.1s = Ns
                            if not _in_starting():
                                return
                            if (self._good_luck_bypass_on_alive
                                    and self._analyzer is not None
                                    and self._analyzer.game_battle_alive):
                                logger.info(
                                    "\033[92mController: game_battle_alive after %.1fs of the %ds "
                                    "Good-Luck wait — bypassing the remainder\033[0m",
                                    time.time() - gl_start, good_luck_wait)
                                bypassed = True
                                break
                            time.sleep(0.1)
                        if not bypassed:
                            logger.info(
                                "Controller: Good-Luck wait ran the full %ds without a "
                                "battle-alive signal", good_luck_wait)
                        if _in_starting():
                            logger.info("Controller: game_starting - launching J20 mission")
                            self._analyzer.trigger_event("good_luck_detected")
                            self._set_last_mission("j20")
                            threading.Thread(target=self.mission_j20, daemon=True).start()
                        return
            except Exception:
                logger.exception("Controller: game_starting loop error")
            finally:
                if self._analyzer is not None:
                    self._analyzer.disarm_starting_health_scan()
                logger.info("Controller: game_starting loop stopped")

        threading.Thread(target=_loop, daemon=True).start()

    def restart_last_mission(self):
        """Restart the most recently started mission, defaulting to J20 when none recorded.

        Returns:
            True  — mission was successfully restarted (or started as j20 default).
            False — mission is currently running (lock held); restart skipped.
        """
        if self.is_mission_running():
            logger.warning("\033[91mController: cannot restart mission - previous mission still in progress (lock held)\033[0m")
            return False

        with self._last_mission_lock:
            mission = self._last_mission

        if mission == "j20":
            logger.info("Controller: restarting last mission (J20)")
            threading.Thread(target=self.mission_j20, daemon=True).start()
            return True
        if mission == "loiter":
            logger.info("Controller: restarting last mission (loiter)")
            threading.Thread(target=self.mission_loiter, daemon=True).start()
            return True

        # No prior mission recorded — reached GAME_BATTLE via GAME_UNKNOWN (Good Luck
        # not detected, stalled start). Default to j20 rather than doing nothing.
        logger.info("Controller: no prior mission recorded — defaulting to J20")
        self._set_last_mission("j20")
        threading.Thread(target=self.mission_j20, daemon=True).start()
        return True

    def cleanup(self, keep_hotkeys: bool = False):
        """Stop injection activity, release held keys, deregister hooks.

        `keep_hotkeys=True` skips the deregistration so the process can stay in
        standby watching for a second Backspace (ADR 099). Everything else still
        runs: the writers stop and every injectable key is released, so nothing
        wingman was holding survives into the operator's manual flight.

        Order matters: XTest-injected key state lives in the X SERVER, not this
        client, so it survives process exit — and daemon threads die without
        running their finally blocks. Exiting mid-eject (NOSE_DOWN held, or the
        120s afterburner hold) therefore left 'k'/'e' logically pressed for the
        whole X session. Stop the writers first, then release every key this
        controller can inject, unconditionally (releasing an un-pressed key is
        a no-op).

        @relation(SAF-007, scope=function)
        """
        # 1. Stop the writers so nothing re-presses after our releases.
        self._eject_stop_reason = "shutdown"
        self._eject_stop.set()
        self.cancel_mission()
        try:
            self.stop_search_and_destroy_loop()
        except Exception:
            logger.exception("Controller: failed to stop search_and_destroy loops")
        eject_thread = self._eject_thread
        if eject_thread is not None and eject_thread.is_alive():
            eject_thread.join(timeout=1.5)  # let its finally release keys cleanly
        self._me_stop.set()  # ADR 070: end any evade hold via its own finally
        self._climb_stop.set()  # ADR 073 3.2b: end any climb hold via its own finally
        self._sg_stop.set()  # ADR 076: end any spawn guard via its own finally
        me_thread = self._me_thread
        if me_thread is not None and me_thread.is_alive():
            me_thread.join(timeout=1.5)

        # 2. Belt-and-braces: release every injectable key.
        if keyboard_module and not self._simulate_os_input:
            # SAF-007: every key wingman injects anywhere must be in this
            # list. 'escape' (press_escape recovery) and MISSION_J20_KEY (the
            # game_starting loop's press_and_release) were missing until the
            # 2026-08-14 audit — a key is stuck if the process dies inside
            # even a press_and_release call.
            for _key in INJECTABLE_KEYS:
                try:
                    keyboard_module.release(_key)
                except Exception:
                    # Last-chance safety net on shutdown: this is the release
                    # that stops a key surviving the process. Never silent.
                    logger.error("Controller: cleanup release of %r failed — %s", _key, _LATCH_NOTE)
            logger.info("Controller: all injectable keys released")

        # 3. Deregister hooks last so the guards above stay active meanwhile.
        if keep_hotkeys:
            logger.info("Controller: keyboard hooks kept for standby — press "
                        "Backspace again to close MetalStorm")
        elif keyboard_module:
            try:
                keyboard_module.unhook_all()
                logger.info("Controller: all keyboard hooks deregistered")
            except ImportError as exc:
                # keyboard requires root on Linux; not an error if privileges weren't granted.
                logger.warning("Controller: keyboard unhook skipped — %s", exc)
            except Exception:
                logger.exception("Controller: failed to unhook keyboard hooks")
