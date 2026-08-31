"""
Tests for mission cancellation race condition (item 5.3) and the GAME_STARTING
stall fix (cancel mid-starting-loop must fire starting_timeout).

Verifies that:
  (a) cancel_mission() causes an in-flight mission to release the lock within 2 seconds.
  (b) No key presses occur after cancellation is set.
  (c) cancel_mission() while FSM is GAME_STARTING fires starting_timeout.
  (d) A natural FSM transition away from GAME_STARTING does NOT fire starting_timeout.
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


@pytest.fixture
def ctrl(monkeypatch):
    """Controller with keyboard patched out so no real keys are pressed."""
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
            weapon_loop_interval=0.01,  # fast weapon loop for test speed
        )
    )
    yield c
    exit_event.set()
    # Re-assert cancel until the mission lock is actually free: mission entry
    # does _mission_cancel.clear(), so a single cancel landing in the
    # spawn-to-entry window is swallowed and the thread survives into
    # monkeypatch teardown — where keyboard_module reverts to the REAL XTest
    # shim and the thread presses real keys (2026-08-14 stuck-'i' incident).
    deadline = time.time() + 5.0
    while c.is_mission_running() and time.time() < deadline:
        c._mission_cancel.set()
        time.sleep(0.05)
    assert not c.is_mission_running(), "mission thread survived teardown"


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_cancel_releases_lock_within_two_seconds(ctrl):
    """Mission lock must be released within 2 s of cancel_mission() being called."""
    started = threading.Event()

    def _run():
        # Patch deploy_flares to signal when the mission has started weapon fire
        started.set()
        ctrl.mission_j20()

    t = threading.Thread(target=_run, daemon=True)
    t.start()

    # Wait for mission to acquire the lock
    deadline = time.time() + 2.0
    while not ctrl.is_mission_running() and time.time() < deadline:
        time.sleep(0.01)

    assert ctrl.is_mission_running(), "mission_j20 never acquired the lock"

    ctrl.cancel_mission()

    # Lock must be released within 2 seconds of cancellation
    deadline = time.time() + 2.0
    while ctrl.is_mission_running() and time.time() < deadline:
        time.sleep(0.02)

    assert not ctrl.is_mission_running(), (
        "Mission lock was not released within 2 s after cancel_mission()"
    )


def test_cancel_prevents_new_missions(ctrl):
    """cancel_mission() must set cancellation flags used to stop active loops."""
    ctrl.cancel_mission()
    assert ctrl._mission_cancel.is_set() is True


def test_mission_lock_not_held_after_cancel_completion(ctrl):
    """Mission lock must be released once the adaptive mission is cancelled.

    ADR 075: mission_j20 has no natural completion any more — it runs until
    cancelled (respawn, eject, manual, match end). Lock release now happens
    on the cancel path, so that is what this guards.
    """
    t = threading.Thread(target=ctrl.mission_j20, daemon=True)
    t.start()

    deadline = time.time() + 2.0
    while not ctrl.is_mission_running() and time.time() < deadline:
        time.sleep(0.01)
    assert ctrl.is_mission_running(), "mission_j20 never acquired the lock"

    ctrl.cancel_mission()
    t.join(timeout=10.0)
    assert not t.is_alive(), "mission_j20 thread did not exit after cancel"

    assert not ctrl.is_mission_running(), (
        "Mission lock still held after mission_j20 thread exited"
    )


# ---------------------------------------------------------------------------
# GAME_STARTING loop cancellation tests (stall bug fix)
# ---------------------------------------------------------------------------

class _FrameStub:
    """Capture stand-in: grab_from_thread returns a sentinel frame."""

    def grab_from_thread(self):
        return object()


class _StartingAnalyzerStub:
    """Minimal analyzer stub that stays in GAME_STARTING until told otherwise."""

    def __init__(self):
        self.game_state = GameState.GAME_STARTING
        self.game_battle_alive = False
        self.good_luck = False
        self._game_starting_health_scan_enabled = threading.Event()
        self.trigger_calls: list[str] = []

    def scan_region_for_good_luck(self, _frame):
        return self.good_luck

    def arm_starting_health_scan(self):
        self._game_starting_health_scan_enabled.set()

    def disarm_starting_health_scan(self):
        self._game_starting_health_scan_enabled.clear()

    def trigger_event(self, name: str):
        self.trigger_calls.append(name)
        return True


def _make_starting_ctrl(monkeypatch, analyzer, starting_max_wait_s: float = 90.0) -> Controller:
    monkeypatch.setattr(controller_module, "keyboard_module", None)
    cfg = _load_config()
    region = (0, 0, cfg["region"]["width"], cfg["region"]["height"])
    return Controller(
        region,
        analyzer=analyzer,
        exit_event=threading.Event(),
        capture=None,
        config=ControllerConfig(
            starting_max_wait_s=starting_max_wait_s,
        )
    )


def test_stale_cancel_before_loop_start_does_not_prevent_loop(monkeypatch):
    """cancel set before start_game_starting_loop (e.g. from on_enter_GAME_LOBBY) must not
    cause the loop to exit immediately.

    Regression: _in_starting() checks _mission_cancel, but that flag is always set when
    entering GAME_LOBBY. Without clearing it at loop start the loop exits in < 1 ms.
    """
    analyzer = _StartingAnalyzerStub()
    ctrl = _make_starting_ctrl(monkeypatch, analyzer)

    # Simulate the stale cancel that arrives from on_enter_GAME_LOBBY
    ctrl._mission_cancel.set()

    ctrl.start_game_starting_loop()
    time.sleep(0.15)  # give the loop time to start (or immediately exit if broken)

    # If the loop cleared the stale cancel it's still running — starting_timeout not fired yet
    assert "starting_timeout" not in analyzer.trigger_calls, (
        "Loop must not fire starting_timeout immediately due to stale cancel from a prior state"
    )

    # Clean up: advance FSM away so the loop exits naturally
    analyzer.game_state = GameState.GAME_LOBBY
    time.sleep(0.2)


def test_cancel_during_game_starting_fires_starting_timeout(monkeypatch):
    """cancel_mission() while FSM is GAME_STARTING must fire starting_timeout.

    Regression: previously the loop only checked the FSM state, so pressing End
    set _mission_cancel but the loop kept running until max_wait (180 s).
    """
    analyzer = _StartingAnalyzerStub()
    ctrl = _make_starting_ctrl(monkeypatch, analyzer)

    ctrl.start_game_starting_loop()
    time.sleep(0.15)  # let the loop thread enter its first inner wait

    ctrl.cancel_mission()

    deadline = time.time() + 2.0
    while "starting_timeout" not in analyzer.trigger_calls and time.time() < deadline:
        time.sleep(0.02)

    assert "starting_timeout" in analyzer.trigger_calls, (
        "starting_timeout was not fired after cancel_mission() during GAME_STARTING"
    )


def test_exit_during_game_starting_does_not_fire_timeout(monkeypatch):
    """Program exit while FSM is GAME_STARTING must NOT fire starting_timeout.

    Shutdown cancels the mission too, and the cancel→stalled push then only
    stamps a spurious STALLED warning into the log tail (ADR 077 review,
    2026-08-17 12:52: Backspace during matchmaking)."""
    analyzer = _StartingAnalyzerStub()
    ctrl = _make_starting_ctrl(monkeypatch, analyzer)

    ctrl.start_game_starting_loop()
    time.sleep(0.15)

    ctrl._exit_event.set()
    ctrl.cancel_mission()

    time.sleep(0.3)  # loop detects the cancel within one 0.1s tick

    assert "starting_timeout" not in analyzer.trigger_calls, (
        "starting_timeout must not fire when the cancel comes from program exit"
    )


def test_fsm_transition_away_from_starting_does_not_fire_timeout(monkeypatch):
    """Natural FSM exit from GAME_STARTING (no cancel) must NOT fire starting_timeout.

    If the game moves the FSM to GAME_LOBBY (e.g. opponent disconnected), the
    starting loop exits cleanly without pushing the FSM to GAME_STARTING_STALLED.
    """
    analyzer = _StartingAnalyzerStub()
    ctrl = _make_starting_ctrl(monkeypatch, analyzer)

    ctrl.start_game_starting_loop()
    time.sleep(0.15)

    # Simulate FSM advancing without cancel (e.g. game ended early)
    analyzer.game_state = GameState.GAME_LOBBY

    time.sleep(0.3)  # loop detects the state change within one 0.1s tick

    assert "starting_timeout" not in analyzer.trigger_calls, (
        "starting_timeout must not fire when FSM exits GAME_STARTING without cancel"
    )


def test_starting_max_wait_s_stored_on_controller(monkeypatch):
    """starting_max_wait_s constructor param must be stored as _starting_max_wait_s."""
    monkeypatch.setattr(controller_module, "keyboard_module", None)
    cfg = _load_config()
    region = (0, 0, cfg["region"]["width"], cfg["region"]["height"])
    ctrl = Controller(region, config=ControllerConfig(starting_max_wait_s=42.5))
    assert ctrl._starting_max_wait_s == 42.5


def test_good_luck_wait_is_bypassed_by_battle_alive(monkeypatch):
    """The post-'Good Luck' settle must end early once the aircraft is in the world.

    Before 2026-08-05 this loop polled only _in_starting(), so no signal could
    shorten it and nothing scanned the screen during the window at all.
    """
    analyzer = _StartingAnalyzerStub()
    ctrl = _make_starting_ctrl(monkeypatch, analyzer)
    ctrl._good_luck_wait_s = 30.0        # long enough that a full wait would be obvious
    ctrl._starting_max_wait_s = 60.0
    monkeypatch.setattr(ctrl, "mission_j20", lambda: None)

    analyzer.good_luck = True             # OCR scan will set good_luck_event
    ctrl._capture = _FrameStub()
    ctrl.start_game_starting_loop()

    time.sleep(1.0)                       # past the scan's 0.5s settle
    analyzer.game_battle_alive = True     # aircraft is in the world
    deadline = time.time() + 5.0
    while time.time() < deadline and "good_luck_detected" not in analyzer.trigger_calls:
        time.sleep(0.05)

    assert "good_luck_detected" in analyzer.trigger_calls, (
        "battle-alive must cut the Good-Luck wait short instead of sleeping the full window")
    analyzer.game_state = GameState.GAME_LOBBY
    time.sleep(0.2)


def test_good_luck_bypass_can_be_disabled(monkeypatch):
    """With the bypass off the wait runs its full length (the pre-2026-08-05 behaviour)."""
    analyzer = _StartingAnalyzerStub()
    ctrl = _make_starting_ctrl(monkeypatch, analyzer)
    ctrl._good_luck_wait_s = 1.5
    ctrl._good_luck_bypass_on_alive = False
    ctrl._starting_max_wait_s = 60.0
    monkeypatch.setattr(ctrl, "mission_j20", lambda: None)

    analyzer.good_luck = True
    ctrl._capture = _FrameStub()
    ctrl.start_game_starting_loop()
    analyzer.game_battle_alive = True     # would bypass if enabled

    time.sleep(1.2)
    assert "good_luck_detected" not in analyzer.trigger_calls, (
        "bypass disabled → the wait must not be cut short")

    deadline = time.time() + 4.0
    while time.time() < deadline and "good_luck_detected" not in analyzer.trigger_calls:
        time.sleep(0.05)
    assert "good_luck_detected" in analyzer.trigger_calls, "full wait should still launch"
    analyzer.game_state = GameState.GAME_LOBBY
    time.sleep(0.2)


def test_good_luck_wait_config_defaults(monkeypatch):
    analyzer = _StartingAnalyzerStub()
    ctrl = _make_starting_ctrl(monkeypatch, analyzer)
    assert ctrl._good_luck_wait_s == 13.0
    assert ctrl._good_luck_bypass_on_alive is True


# ---------------------------------------------------------------------------
# Capture-stall injection guard (2026-08-14 display-loss incident)
# ---------------------------------------------------------------------------

class _AgedCapture:
    """Capture stand-in reporting a fixed last-frame age."""

    def __init__(self, age_s: float):
        self.age_s = age_s

    def seconds_since_last_frame(self):
        return self.age_s

    def grab_from_thread(self):
        return None


def _make_starting_ctrl_with_capture(monkeypatch, analyzer, capture) -> Controller:
    monkeypatch.setattr(controller_module, "keyboard_module", None)
    cfg = _load_config()
    region = (0, 0, cfg["region"]["width"], cfg["region"]["height"])
    return Controller(
        region,
        analyzer=analyzer,
        exit_event=threading.Event(),
        capture=capture,
        config=ControllerConfig(
            simulate_os_input=True,
            capture_stale_inject_s=10.0,
        )
    )


def _game_starting_presses(ctrl) -> list[dict]:
    return [i for i in ctrl.get_action_intents() if i.get("action") == "game_starting_loop"]


def test_stale_capture_suppresses_game_starting_press(monkeypatch):
    """No frames for longer than capture_stale_inject_s → the loop must not press
    the mission key: the game may no longer be on screen, so the press would land
    in whatever window is focused."""
    analyzer = _StartingAnalyzerStub()
    ctrl = _make_starting_ctrl_with_capture(monkeypatch, analyzer, _AgedCapture(30.0))

    ctrl.start_game_starting_loop()
    time.sleep(0.3)

    assert _game_starting_presses(ctrl) == [], (
        "mission key pressed while capture was stale (no frame for 30s, limit 10s)"
    )

    analyzer.game_state = GameState.GAME_LOBBY
    time.sleep(0.2)


def test_fresh_capture_allows_game_starting_press(monkeypatch):
    analyzer = _StartingAnalyzerStub()
    ctrl = _make_starting_ctrl_with_capture(monkeypatch, analyzer, _AgedCapture(1.0))

    ctrl.start_game_starting_loop()
    time.sleep(0.3)

    assert _game_starting_presses(ctrl), "fresh capture must not suppress the mission key"

    analyzer.game_state = GameState.GAME_LOBBY
    time.sleep(0.2)


def test_capture_without_freshness_tracking_allows_press(monkeypatch):
    """Replay/test capture doubles don't track freshness — no staleness evidence
    means the press proceeds (backward compatible with the replay gates)."""
    analyzer = _StartingAnalyzerStub()
    ctrl = _make_starting_ctrl_with_capture(monkeypatch, analyzer, _FrameStub())

    ctrl.start_game_starting_loop()
    time.sleep(0.3)

    assert _game_starting_presses(ctrl), (
        "capture without seconds_since_last_frame must not suppress the press"
    )

    analyzer.game_state = GameState.GAME_LOBBY
    time.sleep(0.2)


# --- SAF-001: manual takeover hands over the aircraft completely -------------
#
# Observed 2026-08-30: after takeover the aircraft climbed 550 m to 7655 m on
# its own, with 'e' (afterburner), 'p' (padlock) and 'k' (pitch) still held down
# on the X server. The FSM transition stopped the SELECTION but not the presses
# already in flight, and the operator could not fly.

def _ctrl_in_state(state):
    from wingman.controller import Controller
    import threading
    c = Controller.__new__(Controller)
    c._analyzer = type("A", (), {"game_state": state})()
    c._simulate_os_input = False
    c._programmatic_key_lock = threading.Lock()
    c._programmatic_key_counts = {}
    return c


def test_flight_keys_are_suppressed_during_manual_takeover():
    from wingman.analyzer import GameState
    from wingman.keybindings import NOSE_UP_KEY, AFTERBURNER_KEY
    c = _ctrl_in_state(GameState.GAME_BATTLE_MANUAL)
    assert c._manual_takeover_active() is True
    for key in (NOSE_UP_KEY, AFTERBURNER_KEY, "p"):
        assert key != "flares"


def test_flares_remain_the_sole_automation_during_takeover():
    """The one exception: a defensive reflex the operator cannot reasonably win,
    and it commands no flight axis."""
    import pathlib
    src = pathlib.Path("wingman/controller.py").read_text()
    guard = src[src.index("if key != DEPLOY_FLARES_KEY and self._manual_takeover_active():"):]
    assert "return" in guard[:200]


def test_the_guard_sits_at_the_single_key_press_choke_point():
    """Enforcing at each caller would leave wingman holding a control surface
    the first time one is missed — there are mission threads, tactic holds,
    weapon and padlock loops and recovery paths."""
    import pathlib
    src = pathlib.Path("wingman/controller.py").read_text()
    body = src[src.index("def _execute_key_press("):src.index("def _execute_key_press(") + 2000]
    assert "_manual_takeover_active()" in body


def test_takeover_is_driven_from_the_fsm_entry_hook():
    """However takeover was reached — maneuver key, arrow, or any future path."""
    import pathlib
    az = pathlib.Path("wingman/analyzer.py").read_text()
    hook = az[az.index("def on_enter_GAME_BATTLE_MANUAL"):]
    assert "MANUAL_TAKEOVER" in hook[:600]
    mn = pathlib.Path("wingman/main.py").read_text()
    assert "GameEvent.MANUAL_TAKEOVER, ctrl.release_for_manual_takeover" in mn


def test_release_covers_every_injectable_key():
    import pathlib
    src = pathlib.Path("wingman/controller.py").read_text()
    fn = src[src.index("def release_for_manual_takeover"):]
    assert "INJECTABLE_KEYS" in fn[:1400]
    for stop in ("_eject_stop", "_me_stop", "_climb_stop", "_sg_stop", "cancel_mission"):
        assert stop in fn[:1400], stop


# --- SAF-001: a respawn does not revoke the operator's takeover --------------
#
# Measured 2026-08-30: both takeover windows ended on respawn detection, at 15 s
# and 85 s. Taking control and losing the aircraft to the next death is not
# manual control in any useful sense.

def test_respawn_returns_control_to_wingman_by_default():
    """ADR 059. The operator took the aircraft to fly the life they were in;
    once it is over there is nothing left to hold, and needing a keypress after
    every death makes an unattended session need attending."""
    import pathlib
    import yaml
    cfg = yaml.safe_load(pathlib.Path("wingman/config.yaml").read_text())
    assert cfg["mission"]["manual_takeover"]["persist_through_respawn"] is False


def test_the_respawn_reset_is_gated_on_the_flag():
    import pathlib
    src = pathlib.Path("wingman/tick_handlers.py").read_text()
    blk = src[src.index("if self._manual_persists_through_respawn:"):]
    assert 'analyzer.trigger_event("respawn_reset")' in blk[:900], \
        "the reset must fire on the default path"


def test_the_resumed_mission_is_the_last_one_flown():
    """Resuming into the wrong mission would be worse than not resuming — the
    operator picked loiter or j20 for a reason."""
    import pathlib
    src = pathlib.Path("wingman/controller.py").read_text()
    assert "def restart_last_mission" in src
    fn = src[src.index("def restart_last_mission"):]
    assert "self._last_mission" in fn[:900]


def test_no_auto_restart_is_promised_while_manual():
    """The 2026-07-31 07:42 failure this design replaces was not the persistence
    itself but a restart promised and never fired — the scheduler was
    GAME_BATTLE-gated while the FSM sat in manual."""
    import pathlib
    src = pathlib.Path("wingman/tick_handlers.py").read_text()
    assert "if not (self._manual_persists_through_respawn" in src
    assert "GameState.GAME_BATTLE_MANUAL)" in src


def test_the_operator_can_hand_control_back():
    """Persistence needs a deliberate way out or the session is stuck in manual
    until the round ends."""
    import pathlib
    src = pathlib.Path("wingman/controller.py").read_text()
    assert "def _release_manual_if_active" in src
    assert 'self._analyzer.trigger_event("manual_release")' in src
    hk = src[src.index("def _on_auto_mission_hotkey"):]
    assert "GameState.GAME_BATTLE_MANUAL:" in hk[:1400]
    assert "_release_manual_if_active()" in hk[:1400]


def test_manual_release_is_a_real_fsm_transition():
    from wingman.analyzer import _FSM_TRANSITIONS as TRANSITIONS
    t = [x for x in TRANSITIONS if x["trigger"] == "manual_release"]
    assert len(t) == 1
    assert t[0]["source"] == "GAME_BATTLE_MANUAL" and t[0]["dest"] == "GAME_BATTLE"


# --- Two instances must never fly the same aircraft --------------------------
#
# Observed 2026-08-30: two wingman instances ran for over an hour, both
# injecting into the same display. A manual takeover in one left the other still
# commanding the aircraft — reported as "alternate inputs overriding my own".
# Neither log said anything, because each instance was behaving correctly on its
# own; the fault only exists between them.

def test_a_second_instance_is_refused():
    import uuid
    from wingman.main import _claim_single_instance
    name = f"wingman-test-{uuid.uuid4()}"   # never the production name
    first = _claim_single_instance(name)
    try:
        assert first is not None
        assert _claim_single_instance(name) is None, "second instance must be refused"
    finally:
        if first is not None:
            first.close()


def test_the_claim_leaves_no_stale_lock():
    """Abstract socket, not a PID file: the kernel releases the name however the
    process dies, so a SIGKILLed instance cannot block the next start."""
    import uuid
    from wingman.main import _claim_single_instance
    name = f"wingman-test-{uuid.uuid4()}"
    first = _claim_single_instance(name)
    assert first is not None
    first.close()                          # simulate death
    second = _claim_single_instance(name)  # must succeed immediately
    try:
        assert second is not None
    finally:
        second.close()


def test_the_guard_runs_before_anything_touches_the_game():
    import pathlib
    # Comments are stripped: main.py mentions these names in prose too, and
    # matching that would check the wrong thing.
    src = "\n".join(
        ln for ln in pathlib.Path("wingman/main.py").read_text().splitlines()
        if not ln.strip().startswith("#"))
    claim = src.index("_instance_lock = _claim_single_instance()")
    for later in ("set_injection_display(nested_display)", "cap = Capture(",
                  "ctrl = Controller("):
        assert src.index(later) > claim, f"{later} must come after the claim"
