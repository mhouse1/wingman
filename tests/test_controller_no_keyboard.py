"""
Tests for Controller behaviour when the keyboard module is unavailable (item 5.2).

Verifies that Controller initialises without exception when the module-level
keyboard_module is None and that game-control methods degrade gracefully instead
of raising AttributeError.
"""

import threading
import time
import pytest
import yaml
import wingman.controller as controller_module
from wingman.controller_config import ControllerConfig

from constants import CONFIG_PATH
from wingman.controller import Controller
from wingman.analyzer import GameState


def _load_config():
    with CONFIG_PATH.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def _drain_mission_threads(c, timeout=5.0):
    """Cancel and WAIT OUT any mission daemon thread before monkeypatch reverts.

    mission_j20's entry does `_mission_cancel.clear()`, so a cancel that lands
    in the spawn-to-entry window is silently swallowed — the mission then runs
    its full multi-minute script. Combined with monkeypatch restoring the REAL
    keyboard_module at teardown, a surviving thread presses REAL keys; pytest
    exiting mid-hold latches the key in the X server (the 2026-08-14 stuck-'i'
    incident). Re-assert the cancel until the mission lock is actually free.
    """
    deadline = time.time() + timeout
    while c.is_mission_running() and time.time() < deadline:
        c._mission_cancel.set()
        time.sleep(0.05)
    assert not c.is_mission_running(), (
        "mission thread survived teardown — it would press REAL keys once "
        "monkeypatch restores keyboard_module"
    )


@pytest.fixture
def ctrl(monkeypatch):
    """Controller with keyboard_module patched to None."""
    monkeypatch.setattr(controller_module, "keyboard_module", None)
    cfg = _load_config()
    region = (0, 0, cfg["region"]["width"], cfg["region"]["height"])
    exit_event = threading.Event()
    c = Controller(
        region,
        analyzer=None,
        exit_event=exit_event,
        capture=None,
        on_auto_mission_key=None,
        config=ControllerConfig(
            weapon_loop_interval=0.5,
        )
    )
    yield c
    exit_event.set()
    _drain_mission_threads(c)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_init_without_keyboard(ctrl):
    """Controller must initialise without raising when keyboard_module is None."""
    assert ctrl is not None


def test_deploy_flares_without_keyboard(ctrl):
    """deploy_flares() must not raise when keyboard is None."""
    ctrl.deploy_flares(hold_seconds=0.0)


def test_cancel_mission_without_keyboard(ctrl):
    """cancel_mission() must not raise when keyboard is None."""
    ctrl.cancel_mission()


def test_is_mission_running_without_keyboard(ctrl):
    """is_mission_running() must return a bool without raising."""
    result = ctrl.is_mission_running()
    assert isinstance(result, bool)


def test_restart_last_mission_no_history(ctrl):
    """restart_last_mission() defaults to J20 and returns True when no prior mission recorded."""
    result = ctrl.restart_last_mission()
    assert result is True
    # _last_mission must now be set so future restarts also work
    with ctrl._last_mission_lock:
        assert ctrl._last_mission == "j20"
    _drain_mission_threads(ctrl)  # a bare cancel can be swallowed by the
    # mission entry's _mission_cancel.clear() — see the helper's docstring


def test_restart_last_mission_returns_false_when_running(ctrl):
    """restart_last_mission() returns False when the mission lock is held."""
    ctrl._mission_lock.acquire(blocking=False)
    try:
        result = ctrl.restart_last_mission()
        assert result is False
    finally:
        if ctrl._mission_lock.locked():
            ctrl._mission_lock.release()


class _AnalyzerStub:
    def __init__(self, state: GameState):
        self.game_state = state
        self.trigger_calls = []

    def trigger_event(self, name: str):
        self.trigger_calls.append(name)
        return True


class _KeyboardStub:
    def __init__(self):
        self.handlers = {}

    def on_press_key(self, key, callback, suppress=False):
        self.handlers[key] = callback

    def add_hotkey(self, _key, _callback):
        return None


class _ThreadStub:
    started_targets = []

    def __init__(self, target=None, daemon=None, args=None, kwargs=None):
        self._target = target
        self._daemon = daemon
        self._args = args or ()
        self._kwargs = kwargs or {}

    def start(self):
        _ThreadStub.started_targets.append(self._target)


def test_maneuver_key_triggers_manual_takeover_in_battle(monkeypatch):
    """CR-003-12: maneuver key path should cancel mission and trigger manual_takeover in battle."""
    monkeypatch.setattr(controller_module, "keyboard_module", None)
    cfg = _load_config()
    region = (0, 0, cfg["region"]["width"], cfg["region"]["height"])
    analyzer = _AnalyzerStub(GameState.GAME_BATTLE)
    ctrl = Controller(region, analyzer=analyzer)

    ctrl._mission_lock.acquire(blocking=False)
    try:
        handled = ctrl._handle_maneuver_key_press("j", is_injected=False)
        assert handled is True
        assert ctrl._mission_cancel.is_set() is True
        assert ctrl._auto_respawn_restart is False
        assert analyzer.trigger_calls == ["manual_takeover"]
    finally:
        if ctrl._mission_lock.locked():
            ctrl._mission_lock.release()


def test_maneuver_key_does_not_trigger_manual_takeover_outside_battle(monkeypatch):
    """Maneuver key still cancels mission outside battle but should not fire manual_takeover trigger."""
    monkeypatch.setattr(controller_module, "keyboard_module", None)
    cfg = _load_config()
    region = (0, 0, cfg["region"]["width"], cfg["region"]["height"])
    analyzer = _AnalyzerStub(GameState.GAME_LOBBY)
    ctrl = Controller(region, analyzer=analyzer)

    ctrl._mission_lock.acquire(blocking=False)
    try:
        handled = ctrl._handle_maneuver_key_press("j", is_injected=False)
        assert handled is True
        assert ctrl._mission_cancel.is_set() is True
        assert analyzer.trigger_calls == []
    finally:
        if ctrl._mission_lock.locked():
            ctrl._mission_lock.release()


def test_maneuver_key_ignores_injected_events(monkeypatch):
    """Injected/programmatic key events must not force manual takeover."""
    monkeypatch.setattr(controller_module, "keyboard_module", None)
    cfg = _load_config()
    region = (0, 0, cfg["region"]["width"], cfg["region"]["height"])
    analyzer = _AnalyzerStub(GameState.GAME_BATTLE)
    ctrl = Controller(region, analyzer=analyzer)

    ctrl._mission_lock.acquire(blocking=False)
    try:
        handled = ctrl._handle_maneuver_key_press("j", is_injected=True)
        assert handled is False
        assert ctrl._mission_cancel.is_set() is False
        assert analyzer.trigger_calls == []
    finally:
        if ctrl._mission_lock.locked():
            ctrl._mission_lock.release()


def test_maneuver_key_suppressed_for_key_wingman_is_holding(monkeypatch):
    """A key wingman itself is currently holding (e.g. NOSE_DOWN during an
    eject correction) must not self-trigger manual takeover."""
    monkeypatch.setattr(controller_module, "keyboard_module", None)
    cfg = _load_config()
    region = (0, 0, cfg["region"]["width"], cfg["region"]["height"])
    analyzer = _AnalyzerStub(GameState.GAME_BATTLE)
    ctrl = Controller(region, analyzer=analyzer)

    ctrl._mission_lock.acquire(blocking=False)
    ctrl._inc_programmatic_key("k")
    try:
        handled = ctrl._handle_maneuver_key_press("k", is_injected=False)
        assert handled is False
        assert ctrl._mission_cancel.is_set() is False
        assert analyzer.trigger_calls == []
    finally:
        ctrl._dec_programmatic_key("k")
        if ctrl._mission_lock.locked():
            ctrl._mission_lock.release()


def test_maneuver_key_not_suppressed_for_different_key_wingman_is_holding(monkeypatch):
    """Wingman holding one maneuver key (e.g. NOSE_DOWN mid eject-correction)
    must not swallow the player pressing a *different* maneuver key to take
    over -- production logs (2026-07-30) showed the player's manual-takeover
    presses could be silently ignored for the whole multi-second nose-down
    hold when the guard was a single global counter instead of per-key."""
    monkeypatch.setattr(controller_module, "keyboard_module", None)
    cfg = _load_config()
    region = (0, 0, cfg["region"]["width"], cfg["region"]["height"])
    analyzer = _AnalyzerStub(GameState.GAME_BATTLE)
    ctrl = Controller(region, analyzer=analyzer)

    ctrl._mission_lock.acquire(blocking=False)
    ctrl._inc_programmatic_key("k")  # wingman holding NOSE_DOWN, e.g. mid eject correction
    try:
        handled = ctrl._handle_maneuver_key_press("l", is_injected=False)  # player: ROLL_RIGHT
        assert handled is True
        assert ctrl._mission_cancel.is_set() is True
        assert analyzer.trigger_calls == ["manual_takeover"]
    finally:
        ctrl._dec_programmatic_key("k")
        if ctrl._mission_lock.locked():
            ctrl._mission_lock.release()


def test_post_release_grace_suppresses_stale_autorepeat(monkeypatch):
    """A maneuver key pressed inside the post-release grace is treated as ours.

    The X server auto-repeats XTest-injected held keys (measured ~25 Hz on this
    host, send_event=False), so repeats emitted while wingman held NOSE_DOWN can
    be delivered by the XRecord listener a few ms after wingman released it.
    Those must not read as player input — that produced three spurious
    "manual takeover" self-cancels on 2026-07-30.
    """
    monkeypatch.setattr(controller_module, "keyboard_module", None)
    cfg = _load_config()
    region = (0, 0, cfg["region"]["width"], cfg["region"]["height"])
    analyzer = _AnalyzerStub(GameState.GAME_BATTLE)
    ctrl = Controller(region, analyzer=analyzer)

    ctrl._mission_lock.acquire(blocking=False)
    try:
        # Wingman just released 'k'; the counter is already back to zero.
        ctrl._arm_release_grace("k")
        assert ctrl._programmatic_key_counts.get("k", 0) == 0
        handled = ctrl._handle_maneuver_key_press("k", is_injected=False)
        assert handled is False, "stale auto-repeat inside grace must not take over"
        assert analyzer.trigger_calls == []

        # A different key is unaffected — grace is per-key.
        assert ctrl._handle_maneuver_key_press("l", is_injected=False) is True
    finally:
        if ctrl._mission_lock.locked():
            ctrl._mission_lock.release()


def test_grace_expires_so_real_presses_still_take_over(monkeypatch):
    """The grace must be a brief window, not a lasting blind spot."""
    monkeypatch.setattr(controller_module, "keyboard_module", None)
    cfg = _load_config()
    region = (0, 0, cfg["region"]["width"], cfg["region"]["height"])
    analyzer = _AnalyzerStub(GameState.GAME_BATTLE)
    ctrl = Controller(region, analyzer=analyzer)

    ctrl._mission_lock.acquire(blocking=False)
    try:
        ctrl._arm_release_grace("k")
        assert ctrl._handle_maneuver_key_press("k", is_injected=False) is False
        # Advance past the window without sleeping.
        ctrl._prog_release_grace_until["k"] = time.time() - 0.001
        assert ctrl._handle_maneuver_key_press("k", is_injected=False) is True
        assert analyzer.trigger_calls == ["manual_takeover"]
    finally:
        if ctrl._mission_lock.locked():
            ctrl._mission_lock.release()


def test_eject_key_releases_before_dropping_the_guard(monkeypatch):
    """Ordering is load-bearing: the physical release must precede the decrement.

    Dropping the counter first left the guard at zero while the key was still
    physically down and auto-repeating, across the whole duration of
    keyboard release (which opens a fresh X Display connection per call).
    """
    events = []

    class _RecordingKbd:
        def press(self, k):
            events.append(("press", k, ctrl._programmatic_key_counts.get(k, 0)))

        def release(self, k):
            events.append(("release", k, ctrl._programmatic_key_counts.get(k, 0)))

        def on_press_key(self, k, cb, suppress=False):
            pass

        def add_hotkey(self, *a, **kw):
            pass

    monkeypatch.setattr(controller_module, "keyboard_module", _RecordingKbd())
    cfg = _load_config()
    region = (0, 0, cfg["region"]["width"], cfg["region"]["height"])
    ctrl = Controller(region, analyzer=None, config=ControllerConfig(disable_hotkeys=True))

    ctrl._eject_key(True, "k")
    ctrl._eject_key(False, "k")

    assert [(e[0], e[1]) for e in events] == [("press", "k"), ("release", "k")]
    # The guard must still be held (count >= 1) at the instant of the release.
    release_count = next(c for kind, _, c in events if kind == "release")
    assert release_count >= 1, "guard was already zero during the physical release"
    # And the grace must be armed once the count finally drops.
    assert ctrl._programmatic_key_counts.get("k", 0) == 0
    assert ctrl._prog_release_grace_until.get("k", 0.0) > time.time()


def test_padlock_hotkey_ignores_wingmans_own_press(monkeypatch):
    """The padlock loop must not set its own manual-override cooldown.

    padlock_camera() presses 'p' every ~6s; without a guard those echo back
    through this hook and set a 10s cooldown each time, halving the effective
    padlock cadence (observed 2026-07-30).
    """
    keyboard_stub = _KeyboardStub()
    monkeypatch.setattr(controller_module, "keyboard_module", keyboard_stub)
    monkeypatch.setattr(controller_module.threading, "Thread", _ThreadStub)
    cfg = _load_config()
    region = (0, 0, cfg["region"]["width"], cfg["region"]["height"])
    ctrl = Controller(region, analyzer=_AnalyzerStub(GameState.GAME_BATTLE))

    handler = keyboard_stub.handlers[controller_module.PADLOCK_CAMERA]
    ctrl._padlock_cooldown_until = 0.0

    # Wingman's own press: in flight -> must not arm the cooldown.
    ctrl._inc_programmatic_key(controller_module.PADLOCK_CAMERA)
    handler(type("_E", (), {"name": controller_module.PADLOCK_CAMERA})())
    assert ctrl._padlock_cooldown_until == 0.0
    ctrl._dec_programmatic_key(controller_module.PADLOCK_CAMERA)

    # A genuine manual press still arms it.
    handler(type("_E", (), {"name": controller_module.PADLOCK_CAMERA})())
    assert ctrl._padlock_cooldown_until > time.time()


# ---------------------------------------------------------------------------
# ADR 140: padlock state — respawn / press / center-dot fusion signals
# ---------------------------------------------------------------------------

class _PressKeyboardStub(_KeyboardStub):
    """Adds press/release no-ops on top of _KeyboardStub's on_press_key
    recording, so a real padlock_camera() call can complete."""

    def press(self, key):
        pass

    def release(self, key):
        pass


def test_stop_eject_sequence_sets_padlock_state_false(monkeypatch):
    monkeypatch.setattr(controller_module, "keyboard_module", None)
    cfg = _load_config()
    region = (0, 0, cfg["region"]["width"], cfg["region"]["height"])
    ctrl = Controller(region, analyzer=_AnalyzerStub(GameState.GAME_BATTLE))
    ctrl._padlock_engaged = None

    ctrl.stop_eject_sequence()

    assert ctrl.padlock_state() is False


def test_padlock_camera_call_sets_state_unknown(monkeypatch):
    """ADR 140 D3: every programmatic press funnels through this one
    method — a press leaves the real in-game state unknown until
    re-confirmed, regardless of what it was before."""
    keyboard_stub = _PressKeyboardStub()
    monkeypatch.setattr(controller_module, "keyboard_module", keyboard_stub)
    monkeypatch.setattr(controller_module.threading, "Thread", _ThreadStub)
    cfg = _load_config()
    region = (0, 0, cfg["region"]["width"], cfg["region"]["height"])
    ctrl = Controller(region, analyzer=_AnalyzerStub(GameState.GAME_BATTLE))
    ctrl._padlock_engaged = False

    ctrl.padlock_camera(hold_seconds=0.0, block=True)

    assert ctrl.padlock_state() is None


def test_manual_padlock_press_sets_state_unknown(monkeypatch):
    """ADR 140 D3: a genuine manual press, entirely outside
    padlock_camera()'s own call graph, still flips the real toggle."""
    keyboard_stub = _KeyboardStub()
    monkeypatch.setattr(controller_module, "keyboard_module", keyboard_stub)
    monkeypatch.setattr(controller_module.threading, "Thread", _ThreadStub)
    cfg = _load_config()
    region = (0, 0, cfg["region"]["width"], cfg["region"]["height"])
    ctrl = Controller(region, analyzer=_AnalyzerStub(GameState.GAME_BATTLE))
    ctrl._padlock_engaged = False
    handler = keyboard_stub.handlers[controller_module.PADLOCK_CAMERA]

    handler(type("_E", (), {"name": controller_module.PADLOCK_CAMERA})())

    assert ctrl.padlock_state() is None


class _FakeCenterDotTracker:
    def __init__(self):
        self.dot_present = False

    def detect_padlock_center_dot(self, _frame):
        return self.dot_present


def _fusion_ctrl(monkeypatch, confirm_seconds=2.0):
    monkeypatch.setattr(controller_module, "keyboard_module", None)
    cfg = _load_config()
    region = (0, 0, cfg["region"]["width"], cfg["region"]["height"])
    ctrl = Controller(
        region, analyzer=_AnalyzerStub(GameState.GAME_BATTLE),
        config=ControllerConfig(
            padlock_center_indicator={"confirm_seconds": confirm_seconds}),
    )
    tracker = _FakeCenterDotTracker()
    ctrl.set_target_tracker(tracker)
    return ctrl, tracker


def test_center_dot_fusion_needs_both_legs(monkeypatch):
    """ADR 140 D4 (option b, 2026-09-18): the dot alone, while respawning,
    must never start (or continue) the confirm streak. mission/secondary
    weapon are deliberately NOT patched here — neither is read for gating
    any more, only logged (see test_center_dot_fusion_ignores_mission_
    and_secondary_weapon_state below)."""
    ctrl, tracker = _fusion_ctrl(monkeypatch)
    tracker.dot_present = True

    ctrl.note_padlock_center_dot(frame=None, is_respawning=True, now=1000.0)

    assert ctrl._padlock_dot_streak_since is None
    assert ctrl.padlock_state() is None


def test_center_dot_fusion_ignores_mission_and_secondary_weapon_state(monkeypatch):
    """ADR 140 D4 option b: is_mission_running()/is_secondary_weapon_active()
    both dropped as gating legs 2026-09-18 — the third live trial found
    secondary_weapon false through nearly all of ordinary combat and
    mission_running separately false for the ENTIRE duration of any eject
    dive (not just the aborted case), between them leaving almost no real
    window to ever confirm despite the raw detector reading correctly in
    every context checked. The streak must build on
    (not respawning)+dot alone, regardless of either — this is the
    exact shape of a real eject dive (mission_running=False the whole
    time, secondary_weapon=True) that the previous gate excluded despite
    two operator-verified examples of a correctly-reading dot
    (`screenshot_20260918_072721.png` onward,
    `screenshot_20260918_080219.png` onward)."""
    ctrl, tracker = _fusion_ctrl(monkeypatch, confirm_seconds=2.0)
    monkeypatch.setattr(ctrl, "is_mission_running", lambda: False)
    monkeypatch.setattr(ctrl, "is_secondary_weapon_active", lambda: True)
    tracker.dot_present = True
    t0 = 1000.0

    ctrl.note_padlock_center_dot(frame=None, is_respawning=False, now=t0)
    ctrl.note_padlock_center_dot(frame=None, is_respawning=False, now=t0 + 2.0)

    assert ctrl.padlock_state() is False


def test_center_dot_fusion_confirms_false_after_the_full_window(monkeypatch):
    ctrl, tracker = _fusion_ctrl(monkeypatch, confirm_seconds=2.0)
    tracker.dot_present = True
    t0 = 1000.0

    ctrl.note_padlock_center_dot(frame=None, now=t0)
    assert ctrl._padlock_dot_streak_since == t0
    assert ctrl.padlock_state() is None   # streak just started, not yet 2s

    ctrl.note_padlock_center_dot(frame=None, now=t0 + 1.0)
    assert ctrl.padlock_state() is None   # 1s in, still short of confirm_seconds

    ctrl.note_padlock_center_dot(frame=None, now=t0 + 2.0)
    assert ctrl.padlock_state() is False   # full 2s window held


def test_center_dot_fusion_resets_streak_on_a_single_missed_read(monkeypatch):
    """A single tick where either leg drops resets the streak entirely —
    no partial credit carried across the gap."""
    ctrl, tracker = _fusion_ctrl(monkeypatch, confirm_seconds=2.0)
    tracker.dot_present = True
    t0 = 1000.0

    ctrl.note_padlock_center_dot(frame=None, now=t0)
    ctrl.note_padlock_center_dot(frame=None, now=t0 + 1.5)
    assert ctrl.padlock_state() is None

    tracker.dot_present = False
    ctrl.note_padlock_center_dot(frame=None, now=t0 + 1.6)
    assert ctrl._padlock_dot_streak_since is None

    tracker.dot_present = True
    ctrl.note_padlock_center_dot(frame=None, now=t0 + 1.7)
    assert ctrl._padlock_dot_streak_since == t0 + 1.7   # restarted, not resumed
    ctrl.note_padlock_center_dot(frame=None, now=t0 + 3.6)   # only 1.9s since restart
    assert ctrl.padlock_state() is None
    ctrl.note_padlock_center_dot(frame=None, now=t0 + 3.7)   # 2.0s since restart
    assert ctrl.padlock_state() is False


def test_center_dot_fusion_resets_streak_while_respawning(monkeypatch):
    """is_respawning=True must reset the streak exactly like a missed dot
    read — the respawn overlay is not a state this detector was validated
    against (Open Question 4)."""
    ctrl, tracker = _fusion_ctrl(monkeypatch, confirm_seconds=2.0)
    tracker.dot_present = True
    t0 = 1000.0

    ctrl.note_padlock_center_dot(frame=None, is_respawning=False, now=t0)
    ctrl.note_padlock_center_dot(frame=None, is_respawning=True, now=t0 + 1.5)
    assert ctrl._padlock_dot_streak_since is None
    assert ctrl.padlock_state() is None


def test_center_dot_fusion_never_sets_state_true(monkeypatch):
    """ADR 140 Non-Goal 1: nothing in this design ever asserts True — a
    fully agreed, fully elapsed streak still only ever confirms False."""
    ctrl, tracker = _fusion_ctrl(monkeypatch, confirm_seconds=0.0)
    tracker.dot_present = True

    ctrl.note_padlock_center_dot(frame=None, now=1000.0)
    ctrl.note_padlock_center_dot(frame=None, now=1000.0)

    assert ctrl.padlock_state() is False
    assert ctrl.padlock_state() is not True


def test_a_tick_with_no_signal_holds_the_last_known_state(monkeypatch):
    """ADR 140 D5: no signal firing must not touch padlock_state() at all —
    only D2/D3/D4 change it."""
    ctrl, tracker = _fusion_ctrl(monkeypatch)
    ctrl.stop_eject_sequence()   # D2: confirmed False
    assert ctrl.padlock_state() is False

    tracker.dot_present = False
    ctrl.note_padlock_center_dot(frame=None, is_respawning=True, now=1000.0)

    assert ctrl.padlock_state() is False   # unchanged, not reset to Unknown


# ---------------------------------------------------------------------------
# ADR 140 D6: actively correct an Unknown reading for as long as secondary
# weapons stay switched in (not just once at dive start).
# ---------------------------------------------------------------------------

def test_padlock_correction_presses_when_unknown_during_secondary_weapon(monkeypatch):
    ctrl, tracker = _fusion_ctrl(monkeypatch)
    monkeypatch.setattr(ctrl, "is_secondary_weapon_active", lambda: True)
    presses = []
    monkeypatch.setattr(ctrl, "padlock_camera",
                        lambda **kw: presses.append(kw))
    tracker.dot_present = False   # fusion can't confirm this tick — stays Unknown

    ctrl.note_padlock_center_dot(frame=None, now=1000.0)

    assert ctrl.padlock_state() is None
    assert len(presses) == 1
    assert presses[0].get("ignore_cancel") is True, (
        "must ignore_cancel=True — eject_and_dive's cancel_mission() would "
        "otherwise cut this to a near-zero-duration, unreliable tap")


def test_padlock_correction_skips_when_secondary_weapon_inactive(monkeypatch):
    ctrl, tracker = _fusion_ctrl(monkeypatch)
    monkeypatch.setattr(ctrl, "is_secondary_weapon_active", lambda: False)
    presses = []
    monkeypatch.setattr(ctrl, "padlock_camera",
                        lambda **kw: presses.append(kw))
    tracker.dot_present = False

    ctrl.note_padlock_center_dot(frame=None, now=1000.0)

    assert not presses


def test_padlock_correction_skips_when_already_confirmed_false(monkeypatch):
    """Only Unknown is corrected — a confirmed False state is left alone,
    and this design never presses to try to turn padlock ON (Non-Goal 1)."""
    ctrl, tracker = _fusion_ctrl(monkeypatch)
    ctrl.stop_eject_sequence()   # D2: confirmed False
    monkeypatch.setattr(ctrl, "is_secondary_weapon_active", lambda: True)
    presses = []
    monkeypatch.setattr(ctrl, "padlock_camera",
                        lambda **kw: presses.append(kw))
    tracker.dot_present = False

    ctrl.note_padlock_center_dot(frame=None, now=1000.0)

    assert ctrl.padlock_state() is False
    assert not presses


def test_padlock_stays_off_for_the_rest_of_a_secondary_weapon_episode(monkeypatch):
    """Live 2026-09-18: checked a full 1h41m session and found padlock,
    once confirmed False during secondary-weapon use, stayed False for the
    rest of every single episode (39/39) — never regressed to Unknown or
    flipped back True. Prior tests only proved this for one tick after
    confirmation; this recreates a realistic multi-tick episode (dot
    detection fluctuating True/False, the way a real dive's frames do) to
    pin the property directly rather than relying on single-tick
    induction."""
    ctrl, tracker = _fusion_ctrl(monkeypatch, confirm_seconds=2.0)
    monkeypatch.setattr(ctrl, "is_secondary_weapon_active", lambda: True)
    presses = []
    monkeypatch.setattr(ctrl, "padlock_camera", lambda **kw: presses.append(kw))
    t0 = 1000.0

    # Build a streak up to confirmation.
    tracker.dot_present = True
    ctrl.note_padlock_center_dot(frame=None, now=t0)
    ctrl.note_padlock_center_dot(frame=None, now=t0 + 2.1)
    assert ctrl.padlock_state() is False
    assert not presses, "streak confirmed on its own — no correction needed"

    # A realistic tail: many more ticks, dot detection flapping both ways,
    # secondary weapons still active throughout (still mid-dive).
    for i, dot in enumerate([False, True, False, False, True, True, False]):
        tracker.dot_present = dot
        ctrl.note_padlock_center_dot(frame=None, now=t0 + 2.1 + (i + 1) * 0.5)
        assert ctrl.padlock_state() is False, (
            f"regressed off False at step {i} (dot={dot}) — padlock must "
            "stay OFF for the rest of the episode once confirmed")
        assert not presses, (
            f"D6 pressed at step {i} despite already-confirmed False — "
            "this would flip the real in-game toggle for no reason")


def test_padlock_correction_presses_every_tick_with_no_streak_building(monkeypatch):
    """No separate cooldown timer — each call to note_padlock_center_dot
    already represents exactly one fresh check, so as long as no streak
    ever starts building (the dot never reads positive), pressing again
    on the very next tick is correct, not premature."""
    ctrl, tracker = _fusion_ctrl(monkeypatch, confirm_seconds=2.0)
    monkeypatch.setattr(ctrl, "is_secondary_weapon_active", lambda: True)
    presses = []
    monkeypatch.setattr(ctrl, "padlock_camera",
                        lambda **kw: presses.append(kw))
    tracker.dot_present = False
    t0 = 1000.0

    ctrl.note_padlock_center_dot(frame=None, now=t0)
    assert len(presses) == 1

    ctrl.note_padlock_center_dot(frame=None, now=t0 + 0.1)
    assert len(presses) == 2, "no streak building — must press on the very next check too"


def test_padlock_correction_does_not_interrupt_a_building_streak(monkeypatch):
    """Live 2026-09-18 (operator-caught): the old confirm_seconds cooldown
    let D6 press again while a real streak was already accumulating toward
    its own confirmation — that press flips the real toggle, destroying a
    streak that was on track to succeed for no reason. Once the dot reads
    positive and a streak starts, D6 must back off and let it be checked,
    not press again just because state hasn't confirmed False yet."""
    ctrl, tracker = _fusion_ctrl(monkeypatch, confirm_seconds=2.0)
    monkeypatch.setattr(ctrl, "is_secondary_weapon_active", lambda: True)
    presses = []
    monkeypatch.setattr(ctrl, "padlock_camera",
                        lambda **kw: presses.append(kw))
    t0 = 1000.0

    tracker.dot_present = False
    ctrl.note_padlock_center_dot(frame=None, now=t0)
    assert len(presses) == 1
    assert ctrl._padlock_dot_streak_since is None

    # The dot starts reading positive — a real streak begins accumulating.
    # State is still Unknown (not yet confirmed), but D6 must NOT press.
    tracker.dot_present = True
    ctrl.note_padlock_center_dot(frame=None, now=t0 + 0.1)
    assert ctrl._padlock_dot_streak_since == t0 + 0.1
    assert ctrl.padlock_state() is None
    assert len(presses) == 1, "pressed mid-streak — this is the exact bug being fixed"

    ctrl.note_padlock_center_dot(frame=None, now=t0 + 0.5)
    assert len(presses) == 1, "still mid-streak, still must not press"

    # The streak completes on its own, uninterrupted.
    ctrl.note_padlock_center_dot(frame=None, now=t0 + 2.1)
    assert ctrl.padlock_state() is False
    assert len(presses) == 1, "no press was needed — the streak succeeded by itself"


def test_padlock_correction_resumes_after_a_streak_breaks(monkeypatch):
    """A streak that starts and then breaks (a bad read) must let D6
    resume pressing — the streak gate only holds off while something is
    actively accumulating, not forever once one has ever started."""
    ctrl, tracker = _fusion_ctrl(monkeypatch, confirm_seconds=2.0)
    monkeypatch.setattr(ctrl, "is_secondary_weapon_active", lambda: True)
    presses = []
    monkeypatch.setattr(ctrl, "padlock_camera",
                        lambda **kw: presses.append(kw))
    t0 = 1000.0

    tracker.dot_present = True
    ctrl.note_padlock_center_dot(frame=None, now=t0)
    assert ctrl._padlock_dot_streak_since == t0
    assert not presses, "streak just started — must not press"

    tracker.dot_present = False
    ctrl.note_padlock_center_dot(frame=None, now=t0 + 0.5)
    assert ctrl._padlock_dot_streak_since is None, "bad read must break the streak"
    assert len(presses) == 1, "streak broke — pressing must resume"


def test_padlock_state_defaults_to_unknown(monkeypatch):
    monkeypatch.setattr(controller_module, "keyboard_module", None)
    cfg = _load_config()
    region = (0, 0, cfg["region"]["width"], cfg["region"]["height"])
    ctrl = Controller(region, analyzer=None)

    assert ctrl.padlock_state() is None


def test_genuine_u_press_during_game_starting_starts_mission(monkeypatch):
    """A real 'u' during GAME_STARTING must start the mission, not be eaten.

    The old handler dismissed EVERY 'u' in GAME_STARTING as an XTest echo of
    the game_starting loop. When a takeover during the Good-Luck wait wedged
    the FSM in GAME_STARTING, the player's resume presses were all swallowed
    (2026-08-01 02:55: five presses in 3s, all logged as echo). Echoes are now
    identified by the programmatic-key bracket instead of FSM state.
    """
    keyboard_stub = _KeyboardStub()
    monkeypatch.setattr(controller_module, "keyboard_module", keyboard_stub)
    monkeypatch.setattr(controller_module.threading, "Thread", _ThreadStub)

    cfg = _load_config()
    region = (0, 0, cfg["region"]["width"], cfg["region"]["height"])
    analyzer = _AnalyzerStub(GameState.GAME_STARTING)
    ctrl = Controller(region, analyzer=analyzer)
    ctrl.mission_j20 = lambda: None

    _ThreadStub.started_targets = []
    handler = keyboard_stub.handlers[controller_module.MISSION_J20_KEY]
    handler(type("_Event", (), {"name": controller_module.MISSION_J20_KEY})())

    assert len(_ThreadStub.started_targets) == 1, "genuine 'u' during GAME_STARTING was eaten"
    assert analyzer.trigger_calls == ["manual_force_battle"]


def test_wingmans_own_u_press_is_ignored_as_echo(monkeypatch):
    """The game_starting loop's own injected 'u' must not re-trigger the handler."""
    keyboard_stub = _KeyboardStub()
    monkeypatch.setattr(controller_module, "keyboard_module", keyboard_stub)
    monkeypatch.setattr(controller_module.threading, "Thread", _ThreadStub)

    cfg = _load_config()
    region = (0, 0, cfg["region"]["width"], cfg["region"]["height"])
    analyzer = _AnalyzerStub(GameState.GAME_STARTING)
    ctrl = Controller(region, analyzer=analyzer)
    ctrl.mission_j20 = lambda: None
    handler = keyboard_stub.handlers[controller_module.MISSION_J20_KEY]
    event = type("_Event", (), {"name": controller_module.MISSION_J20_KEY})()

    # While the press is in flight (counter held):
    _ThreadStub.started_targets = []
    ctrl._inc_programmatic_key(controller_module.MISSION_J20_KEY)
    handler(event)
    ctrl._dec_programmatic_key(controller_module.MISSION_J20_KEY)
    assert _ThreadStub.started_targets == []

    # And within the post-release grace (late XRecord delivery):
    ctrl._arm_release_grace(controller_module.MISSION_J20_KEY)
    handler(event)
    assert _ThreadStub.started_targets == []

    # After the grace expires, a real press works again.
    ctrl._prog_release_grace_until[controller_module.MISSION_J20_KEY] = time.time() - 0.001
    handler(event)
    assert len(_ThreadStub.started_targets) == 1


def test_j20_hotkey_forces_battle_via_fsm_trigger(monkeypatch):
    """J20 hotkey should call analyzer trigger path instead of direct FSM state assignment."""
    keyboard_stub = _KeyboardStub()
    monkeypatch.setattr(controller_module, "keyboard_module", keyboard_stub)
    monkeypatch.setattr(controller_module.threading, "Thread", _ThreadStub)

    cfg = _load_config()
    region = (0, 0, cfg["region"]["width"], cfg["region"]["height"])
    analyzer = _AnalyzerStub(GameState.GAME_LOBBY)
    ctrl = Controller(region, analyzer=analyzer)
    ctrl.mission_j20 = lambda: None

    _ThreadStub.started_targets = []
    handler = keyboard_stub.handlers[controller_module.MISSION_J20_KEY]
    handler(type("_Event", (), {"name": controller_module.MISSION_J20_KEY})())

    assert analyzer.trigger_calls == ["manual_force_battle"]
    assert len(_ThreadStub.started_targets) == 1


def test_toggle_weapon_loop_accepts_a_key_event(ctrl):
    """Regression: the Linux XKey listener invokes hotkey callbacks as cb(event).

    toggle_weapon_loop took no event arg, so every 'x' press raised
    "takes 1 positional argument but 2 were given" and the toggle never fired
    (2026-08-07 session log). Both call forms must work.
    """
    from types import SimpleNamespace
    ctrl.toggle_weapon_loop()                                              # direct call
    assert ctrl._weapon_loop_active is True
    ctrl.toggle_weapon_loop(SimpleNamespace(name="x", is_injected=False))  # hotkey call
    assert ctrl._weapon_loop_active is False


def test_every_injectable_key_resolves_to_a_keysym():
    """Regression (ADR 070 V1): string_to_keysym(';') returns 0 — punctuation
    needs its X11 keysym NAME via _XKEY_ALIASES, or the injection is silently
    dropped ("unknown keysym for ';'", 2026-08-11 07:27:22 — YAW_LEFT was
    never injectable). Every key the controller can press must resolve."""
    XK = pytest.importorskip("Xlib.XK")
    injectable = [
        controller_module.NOSE_UP_KEY, controller_module.NOSE_DOWN_KEY,
        controller_module.ROLL_LEFT_KEY, controller_module.ROLL_RIGHT_KEY,
        controller_module.YAW_LEFT, controller_module.AFTERBURNER_KEY,
        controller_module.AIRBRAKE_KEY, controller_module.WINGSWEEP_KEY,
        controller_module.DEPLOY_FLARES_KEY, controller_module.FIRE_MACHINE_GUN,
        controller_module.FIRE_ACTIVE_WEAPON, controller_module.PADLOCK_CAMERA,
        controller_module.SPECIAL_ABILITY, controller_module.SWITCH_WEAPON,
        controller_module.CANCEL_MISSION_KEY,
        *controller_module.ALT_FLIGHT_KEYS,
    ]
    for key in injectable:
        xk_name = controller_module._XKEY_ALIASES.get(key.lower(), key.lower())
        assert XK.string_to_keysym(xk_name) != 0, (
            f"key {key!r} (xk name {xk_name!r}) does not resolve to a keysym — "
            "it would be silently dropped by _linux_key_event"
        )


def test_delayed_echo_within_release_grace_is_ignored(monkeypatch):
    """Regression (2026-08-14 02:35 soak): XRecord delivery lag scales with
    X-server load — a queued 'j' auto-repeat arrived 391 ms after wingman's own
    roll_left release, outlived the old 0.15 s grace window, and cancelled the
    mission into GAME_BATTLE_MANUAL mid-flight. Echoes of a key wingman just
    released must be ignored for the full grace second."""
    import time as _time
    monkeypatch.setattr(controller_module, "keyboard_module", None)
    cfg = _load_config()
    region = (0, 0, cfg["region"]["width"], cfg["region"]["height"])
    analyzer = _AnalyzerStub(GameState.GAME_BATTLE)
    ctrl = Controller(region, analyzer=analyzer)

    ctrl._mission_lock.acquire(blocking=False)
    try:
        # Wingman releases 'j' — grace armed at that moment.
        ctrl._arm_release_grace("j")

        # The observed delayed echo: 0.4 s after release.
        _time.sleep(0.4)
        handled = ctrl._handle_maneuver_key_press("j", is_injected=False)
        assert handled is False, "delayed echo cancelled the mission"
        assert ctrl._mission_cancel.is_set() is False
        assert analyzer.trigger_calls == []

        # A genuine press after the grace expires still takes over.
        ctrl._prog_release_grace_until["j"] = _time.time() - 0.01
        handled = ctrl._handle_maneuver_key_press("j", is_injected=False)
        assert handled is True
        assert analyzer.trigger_calls == ["manual_takeover"]
    finally:
        if ctrl._mission_lock.locked():
            ctrl._mission_lock.release()


def test_release_grace_scales_with_release_latency(monkeypatch):
    """2026-08-14 03:35 sizing case: a 0.15 s roll_left took 2.7 s to release
    and queued repeats were still delivered 3.2 s after the release — past any
    fixed window. The grace must scale with the measured release latency
    (3x span), while fast releases keep the 1.0 s floor."""
    import time as _time
    monkeypatch.setattr(controller_module, "keyboard_module", None)
    cfg = _load_config()
    region = (0, 0, cfg["region"]["width"], cfg["region"]["height"])
    analyzer = _AnalyzerStub(GameState.GAME_BATTLE)
    ctrl = Controller(region, analyzer=analyzer)

    # Loaded server: release took 2.56 s -> window must cover the 3.2 s echo.
    now = _time.time()
    ctrl._arm_release_grace("j", span_s=2.56)
    window = ctrl._prog_release_grace_until["j"] - now
    assert window >= 3.2 + 0.5, f"window {window:.1f}s does not cover the observed 3.2s echo"

    # Healthy server: ~5 ms release keeps the fixed floor, not less.
    now = _time.time()
    ctrl._arm_release_grace("k", span_s=0.005)
    window = ctrl._prog_release_grace_until["k"] - now
    assert abs(window - ctrl._prog_release_grace_s) < 0.05
