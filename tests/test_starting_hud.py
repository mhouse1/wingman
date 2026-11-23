"""ADR 189: GAME_STARTING ends on the battle HUD, read from health, speed and altitude.

The Good-Luck loop tapped 'u' (the Good Luck emote) every 5 s and OCRed the chat
reply. Its launch waited on health anyway: of 1,412 November launches, 801 were
cut short by a confirmed health read (median 10.8 s after Good Luck) and the
other 611 ran the 13 s timer out a median 0.7 s before health showed. The HUD
loop presses nothing and launches on the HUD itself.
"""

import copy
import threading
import time
import types
from pathlib import Path

import numpy as np
import pytest
import yaml

import wingman.analyzer as analyzer_module
import wingman.controller as controller_module
from constants import CONFIG_PATH
from perception_fake import PerceptionFake
from wingman.analyzer import (
    GameState,
    GameStateAnalyzer,
    STARTING_MIN_CONFIRMED_HEALTH,
    starting_telemetry_plausible,
)
from wingman.config_schema import schema_default
from wingman.controller import Controller
from wingman.controller_config import ControllerConfig


def _load_config() -> dict:
    with Path(CONFIG_PATH).open("r", encoding="utf-8") as fh:
        return copy.deepcopy(yaml.safe_load(fh))


@pytest.fixture
def analyzer():
    a = GameStateAnalyzer(_load_config())
    a.state = GameState.GAME_STARTING.name
    a.arm_starting_health_scan()
    try:
        yield a
    finally:
        a.cleanup()


def _verdicts(a, *reads):
    return [a._starting_hud_verdict(raw, spd, alt, attempt=i + 1)
            for i, (raw, spd, alt) in enumerate(reads)]


# --- the verdict ---------------------------------------------------------------

def test_the_shipped_signal_is_the_hud():
    assert schema_default("mission.starting_signal") == "hud"
    assert _load_config()["mission"]["starting_signal"] == "hud"
    assert ControllerConfig.from_config(_load_config()).starting_signal == "hud"


def test_one_frame_with_health_and_speed_confirms(analyzer):
    """The 2026-11-17 21:52:50 spawn: health 175, speed 738, altitude 940."""
    assert _verdicts(analyzer, (175, 738, 940)) == [(175, "health+telemetry")]


def test_health_with_only_an_altitude_confirms(analyzer):
    assert _verdicts(analyzer, (175, None, 940)) == [(175, "health+telemetry")]


def test_health_alone_never_confirms(analyzer):
    """ADR 189 D6: 2026-11-19 07:04:11, the match bounced to the lobby and two
    probes read '95' with no speed or altitude; 'health x2' launched the mission
    into the lobby. It confirmed 6 starts on 11-18/19 and 4 had no HUD."""
    assert _verdicts(analyzer, (95, None, None), (95, None, None), (95, None, None)) == [
        None, None, None]


def test_health_then_telemetry_confirms_on_the_telemetry_frame(analyzer):
    assert _verdicts(analyzer, (176, None, None), (176, None, 711)) == [
        None, (176, "health+telemetry")]


def test_the_lobby_seven_does_not_confirm_even_beside_a_speed(analyzer):
    """ADR 102 rev 2's lobby fragment stays below the floor whatever else reads."""
    assert STARTING_MIN_CONFIRMED_HEALTH > 7
    assert _verdicts(analyzer, (7, 738, 940), (7, 738, None)) == [None, None]


def test_a_speed_fragment_does_not_cross_confirm(analyzer):
    assert _verdicts(analyzer, (175, 7, None)) == [None]


def test_speed_and_altitude_on_two_probes_confirm_without_health(analyzer):
    """Sky behind the health digits (ADR 080): the telemetry rows carry it."""
    assert _verdicts(analyzer, (None, 738, 940), (None, 741, 935)) == [
        None, (None, "telemetry x2")]


def test_a_probe_without_telemetry_restarts_the_telemetry_count(analyzer):
    assert _verdicts(analyzer, (None, 738, 940), (None, None, None),
                     (None, 738, 940)) == [None, None, None]


def test_one_telemetry_row_alone_never_confirms(analyzer):
    assert _verdicts(analyzer, (None, 738, None), (None, 740, None),
                     (None, None, 940)) == [None, None, None]


def test_telemetry_bounds():
    assert starting_telemetry_plausible(738, 940) == (True, True)
    assert starting_telemetry_plausible(7, -206) == (False, True)
    assert starting_telemetry_plausible(None, 99999) == (False, False)


def test_arming_forgets_the_last_round(analyzer):
    analyzer._starting_hud_verdict(None, 738, 940)
    analyzer._starting_battle_confirmed.set()
    analyzer.arm_starting_health_scan()
    assert analyzer.starting_battle_confirmed is False
    assert analyzer._starting_hud_verdict(None, 738, 940) is None


# --- the probe -----------------------------------------------------------------

class _InlineExecutor:
    def submit(self, fn):
        fn()


def test_a_probe_on_the_spawn_frame_confirms_the_battle(analyzer, monkeypatch):
    monkeypatch.setattr(type(analyzer), "ocr_executor",
                        property(lambda self: _InlineExecutor()), raising=False)
    monkeypatch.setattr(analyzer_module, "_process_health_region",
                        lambda _f, *a, **k: (175, 0.0))
    monkeypatch.setattr(analyzer_module, "_process_telemetry_region",
                        lambda _f: (738, 940, 0.0))
    analyzer.probe_starting_frame(np.zeros((1200, 1920, 3), dtype=np.uint8))
    assert analyzer.starting_battle_confirmed
    assert analyzer.game_battle_alive
    assert analyzer.get_health() == 175


def test_the_probe_ignores_frames_unless_armed_in_game_starting(analyzer, monkeypatch):
    scheduled = []
    monkeypatch.setattr(analyzer, "_schedule_starting_health_probe", scheduled.append)
    frame = np.zeros((4, 4, 3), dtype=np.uint8)

    analyzer.probe_starting_frame(frame)
    assert len(scheduled) == 1

    analyzer.probe_starting_frame(None)
    assert len(scheduled) == 1

    analyzer.disarm_starting_health_scan()
    analyzer.probe_starting_frame(frame)
    assert len(scheduled) == 1

    analyzer.arm_starting_health_scan()
    analyzer.state = GameState.GAME_LOBBY.name
    analyzer.probe_starting_frame(frame)
    assert len(scheduled) == 1


def test_two_callers_at_once_schedule_one_probe(analyzer, monkeypatch):
    """analyze_frame and the starting loop both feed the probe (ADR 189)."""
    submitted = []
    gate = threading.Event()

    class _Holding:
        def submit(self, fn):
            submitted.append(fn)

    monkeypatch.setattr(type(analyzer), "ocr_executor",
                        property(lambda self: _Holding()), raising=False)
    frame = np.zeros((4, 4, 3), dtype=np.uint8)
    threads = [threading.Thread(target=lambda: (gate.wait(),
                                                analyzer._schedule_starting_health_probe(frame)))
               for _ in range(8)]
    for t in threads:
        t.start()
    gate.set()
    for t in threads:
        t.join()
    assert len(submitted) == 1


# --- the loop ------------------------------------------------------------------

class _HudAnalyzer(PerceptionFake):
    def __init__(self):
        self.game_state = GameState.GAME_STARTING
        self.starting_battle_confirmed = False
        self.trigger_calls = []
        self.probed = 0
        self.armed = False

    def arm_starting_health_scan(self):
        self.armed = True

    def disarm_starting_health_scan(self):
        self.armed = False

    def probe_starting_frame(self, frame):
        self.probed += 1

    def trigger_event(self, name):
        self.trigger_calls.append(name)
        return True


def _hud_ctrl(monkeypatch, analyzer, **cfg) -> Controller:
    monkeypatch.setattr(controller_module, "keyboard_module", None)
    ctrl = Controller(
        (0, 0, 1920, 1200),
        analyzer=analyzer,
        exit_event=threading.Event(),
        capture=types.SimpleNamespace(grab_from_thread=lambda: object()),
        config=ControllerConfig(simulate_os_input=True,
                                starting_health_probe_interval_s=0.1, **cfg),
    )
    return ctrl


def _wait(cond, timeout=3.0):
    deadline = time.time() + timeout
    while not cond() and time.time() < deadline:
        time.sleep(0.02)
    return cond()


def test_the_hud_loop_presses_nothing_and_launches_on_the_hud(monkeypatch):
    analyzer = _HudAnalyzer()
    ctrl = _hud_ctrl(monkeypatch, analyzer)
    launched = []
    monkeypatch.setattr(ctrl, "_start_default_mission", lambda: launched.append(1))
    ctrl.start_game_starting_loop()

    assert _wait(lambda: analyzer.probed >= 3), "the loop must feed the probe frames"
    assert analyzer.armed, "the probe is armed at once, without the 10 s gate"
    assert analyzer.trigger_calls == []

    analyzer.starting_battle_confirmed = True
    assert _wait(lambda: launched == [1])
    assert analyzer.trigger_calls == ["good_luck_detected"]
    assert not [i for i in ctrl.get_action_intents()
                if i.get("action") == "game_starting_loop"], "no 'u' press"
    assert _wait(lambda: not analyzer.armed), "disarmed on the way out"


def test_the_hud_loop_times_out_to_stalled(monkeypatch):
    analyzer = _HudAnalyzer()
    ctrl = _hud_ctrl(monkeypatch, analyzer, starting_max_wait_s=0.3)
    ctrl.start_game_starting_loop()
    assert _wait(lambda: analyzer.trigger_calls == ["starting_timeout"])


def test_the_hud_loop_ends_quietly_when_the_state_moves_on(monkeypatch):
    analyzer = _HudAnalyzer()
    ctrl = _hud_ctrl(monkeypatch, analyzer)
    ctrl.start_game_starting_loop()
    assert _wait(lambda: analyzer.probed >= 1)
    analyzer.game_state = GameState.GAME_LOBBY
    assert _wait(lambda: not analyzer.armed)
    assert analyzer.trigger_calls == []


def test_a_claimed_round_under_the_hud_enters_manual(monkeypatch):
    """The operator's claim (2026-11-01) holds on the HUD path too."""
    from wingman.keybindings import MANUAL_TAKEOVER_KEY
    monkeypatch.setattr(controller_module, "keyboard_module", None)
    real = GameStateAnalyzer(_load_config())
    try:
        ctrl = Controller((0, 0, 1920, 1200), analyzer=real)
        assert real.trigger_event("unknown_to_lobby_detected")
        assert real.trigger_event("cancel_detected")
        assert real.game_state == GameState.GAME_STARTING
        monkeypatch.setattr(ctrl, "_starting_signal", "hud")
        monkeypatch.setattr(ctrl, "_capture",
                            types.SimpleNamespace(grab_from_thread=lambda: object()))
        monkeypatch.setattr(real, "probe_starting_frame",
                            lambda _f: real._starting_battle_confirmed.set())
        launched = []
        monkeypatch.setattr(ctrl, "_start_default_mission", lambda: launched.append(1))
        ctrl._handle_maneuver_key_press(MANUAL_TAKEOVER_KEY)
        ctrl._start_game_starting_loop()
        assert _wait(lambda: real.game_state == GameState.GAME_BATTLE_MANUAL)
        assert launched == []
    finally:
        real.cleanup()


# --- GAME_UNKNOWN calls battle on the HUD, not on a lone health digit ------------

def test_battle_hud_evidence():
    from wingman.analyzer import battle_hud_evidence
    assert battle_hud_evidence(175, 738, 940) == "health+telemetry"
    assert battle_hud_evidence(175, None, 940) == "health+telemetry"
    assert battle_hud_evidence(None, 738, 940) == "telemetry"
    assert battle_hud_evidence(12, 738, 940) == "telemetry"   # a damaged jet
    assert battle_hud_evidence(7, None, None) is None
    assert battle_hud_evidence(175, None, None) is None
    assert battle_hud_evidence(None, 738, None) is None


def _unknown_analyzer(monkeypatch, health, speed, altitude):
    a = GameStateAnalyzer(_load_config())
    monkeypatch.setattr(a, "scan_region_for_click_to", lambda _f: False)
    monkeypatch.setattr(a, "scan_region_for_play_button", lambda _f: None)
    monkeypatch.setattr(a, "_scan_region_for_health_value", lambda _f: health)
    monkeypatch.setattr(a, "_scan_region_for_telemetry", lambda _f: (speed, altitude))
    return a


@pytest.mark.parametrize("reads", [(7, None, None), (175, None, None), (None, 738, None)])
def test_unknown_does_not_call_battle_without_the_hud(monkeypatch, reads):
    """2026-11-18 10:05:13: the spawn intro's '7' twice in a row called battle and
    launched the mission 28 s before the HUD."""
    a = _unknown_analyzer(monkeypatch, *reads)
    frame = np.zeros((1200, 1920, 3), dtype=np.uint8)
    try:
        for _ in range(4):
            a.analyze_frame(frame)
        assert a.game_state == GameState.GAME_UNKNOWN
    finally:
        a.cleanup()


@pytest.mark.parametrize("reads", [(175, 738, 940), (None, 738, 940)])
def test_unknown_calls_battle_on_the_hud(monkeypatch, reads):
    a = _unknown_analyzer(monkeypatch, *reads)
    frame = np.zeros((1200, 1920, 3), dtype=np.uint8)
    try:
        for _ in range(4):
            a.analyze_frame(frame)
            if a.game_state == GameState.GAME_BATTLE:
                break
        assert a.game_state == GameState.GAME_BATTLE
    finally:
        a.cleanup()
