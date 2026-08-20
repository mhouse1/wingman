"""Unit tests for missile_evade_mode (ADR 070).

Controller-side: the hold thread's d5 clear test (fresh negative cache samples
— a stalled cache must NOT end the evade), the d6 max-hold cap, key release on
exit event, the d8 duplicate-start suppression, and the d4 programmatic-key
bracket on ROLL_RIGHT. No real keyboard, no OCR.
"""

import threading
import time

import wingman.controller as controller_module
from wingman.controller_config import ControllerConfig
from wingman.controller import (
    AFTERBURNER_KEY,
    Controller,
    ROLL_RIGHT_KEY,
    YAW_LEFT,
)

EVADE_KEYS = {AFTERBURNER_KEY, ROLL_RIGHT_KEY, YAW_LEFT}


class _FakeKeyboard:
    def __init__(self):
        self.events = []

    def press(self, key):
        self.events.append(("press", key, time.time()))

    def release(self, key):
        self.events.append(("release", key, time.time()))


class _FakeIncomingAnalyzer:
    """Settable stand-in for the analyzer's incoming cache accessors."""

    def __init__(self, detected=True, ts=None):
        self._lock = threading.Lock()
        self._detected = detected
        self._ts = time.time() if ts is None else ts

    def set(self, detected, ts):
        with self._lock:
            self._detected = detected
            self._ts = ts

    def get_incoming_cache_result(self):
        with self._lock:
            return (self._detected, 0.9 if self._detected else 0.0, "template")

    def get_incoming_cache_timestamp(self):
        with self._lock:
            return self._ts


def _make_ctrl(monkeypatch, kb, analyzer, me_cfg):
    monkeypatch.setattr(controller_module, "keyboard_module", kb)
    return Controller(
        (0, 0, 1920, 1200),
        analyzer=analyzer,
        exit_event=threading.Event(),
        capture=None,
        config=ControllerConfig(
            disable_hotkeys=True,
            missile_evade=me_cfg,
        )
    )


def _wait_done(ctrl, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not ctrl.is_missile_evading():
            return True
        time.sleep(0.02)
    return False


def _presses(kb, key):
    return [e for e in kb.events if e[0] == "press" and e[1] == key]


def _releases(kb, key):
    return [e for e in kb.events if e[0] == "release" and e[1] == key]


def test_clear_after_fresh_negative_samples(monkeypatch):
    """d5 happy path: two fresh negatives + the wall-clock window end the hold."""
    t0 = time.time()
    analyzer = _FakeIncomingAnalyzer(detected=True, ts=t0)
    kb = _FakeKeyboard()
    ctrl = _make_ctrl(monkeypatch, kb, analyzer,
                      {"clear_seconds": 0.2, "min_clear_samples": 2, "max_hold_s": 5.0})

    ctrl.missile_evade_mode()
    assert ctrl.is_missile_evading()

    # Two cache refreshes with ADVANCING timestamps, both negative.
    time.sleep(0.15)
    analyzer.set(False, t0 + 0.05)
    time.sleep(0.15)
    analyzer.set(False, t0 + 0.10)

    assert _wait_done(ctrl), "evade did not end on the clear test"
    held = time.time() - t0
    assert held < 4.0, f"evade ran {held:.1f}s — ended by cap, not the clear test"
    for key in EVADE_KEYS:
        assert _presses(kb, key), f"'{key}' never pressed"
        assert _releases(kb, key), f"'{key}' never released"


def test_stalled_cache_does_not_end_evade(monkeypatch):
    """d5 stalled-cache rule: repeated negatives carrying an UNCHANGED
    timestamp are one stale entry read twice — only the d6 cap may end the
    hold. 'No perception' must never read as 'clear'."""
    t0 = time.time()
    analyzer = _FakeIncomingAnalyzer(detected=False, ts=t0)  # stale negative from entry
    kb = _FakeKeyboard()
    ctrl = _make_ctrl(monkeypatch, kb, analyzer,
                      {"clear_seconds": 0.1, "min_clear_samples": 1, "max_hold_s": 0.7})

    ctrl.missile_evade_mode()
    assert _wait_done(ctrl), "evade never ended"

    releases = _releases(kb, ROLL_RIGHT_KEY)
    assert releases, "roll key never released"
    held = releases[0][2] - t0
    assert held >= 0.6, (
        f"evade ended after {held * 1000:.0f} ms on a stalled cache — "
        "the clear test counted a stale negative as fresh"
    )


def test_max_hold_cap_on_stuck_positive(monkeypatch):
    """d6: a detection stuck true releases unconditionally at max_hold_s."""
    t0 = time.time()
    analyzer = _FakeIncomingAnalyzer(detected=True, ts=t0)
    kb = _FakeKeyboard()
    ctrl = _make_ctrl(monkeypatch, kb, analyzer,
                      {"clear_seconds": 0.1, "min_clear_samples": 1, "max_hold_s": 0.5})

    ctrl.missile_evade_mode()
    # Keep the detection fresh AND positive.
    for i in range(1, 5):
        time.sleep(0.1)
        analyzer.set(True, t0 + i * 0.1)

    assert _wait_done(ctrl), "evade never ended despite the cap"
    for key in EVADE_KEYS:
        assert _releases(kb, key), f"'{key}' not released by the cap"


def test_release_on_exit_event(monkeypatch):
    """d6: program exit breaks the hold loop; the finally releases all keys."""
    analyzer = _FakeIncomingAnalyzer(detected=True)
    kb = _FakeKeyboard()
    ctrl = _make_ctrl(monkeypatch, kb, analyzer,
                      {"clear_seconds": 3.0, "min_clear_samples": 2, "max_hold_s": 15.0})

    ctrl.missile_evade_mode()
    time.sleep(0.05)
    t_exit = time.time()
    ctrl._exit_event.set()

    assert _wait_done(ctrl, timeout=2.0), "evade did not end on exit event"
    assert time.time() - t_exit < 1.5
    for key in EVADE_KEYS:
        assert _releases(kb, key), f"'{key}' not released on exit"


def test_duplicate_start_suppressed(monkeypatch):
    """d8: a second trigger while running must not start a second thread or
    double-press keys; is_missile_evading() is true synchronously."""
    analyzer = _FakeIncomingAnalyzer(detected=True)
    kb = _FakeKeyboard()
    ctrl = _make_ctrl(monkeypatch, kb, analyzer,
                      {"clear_seconds": 0.1, "min_clear_samples": 1, "max_hold_s": 0.4})

    ctrl.missile_evade_mode()
    assert ctrl.is_missile_evading()  # set before spawn, no race window
    ctrl.missile_evade_mode()  # duplicate — must no-op

    assert _wait_done(ctrl)
    assert len(_presses(kb, AFTERBURNER_KEY)) == 1
    assert len(_presses(kb, ROLL_RIGHT_KEY)) == 1


def test_fresh_positive_extends_hold(monkeypatch):
    """d8: a later positive moves the clear timer forward — the hold must
    outlive last_positive + clear_seconds, not entry + clear_seconds."""
    t0 = time.time()
    analyzer = _FakeIncomingAnalyzer(detected=True, ts=t0)
    kb = _FakeKeyboard()
    ctrl = _make_ctrl(monkeypatch, kb, analyzer,
                      {"clear_seconds": 0.3, "min_clear_samples": 1, "max_hold_s": 5.0})

    ctrl.missile_evade_mode()
    # Fresh positives for ~0.4s, then fresh negatives.
    for i in range(1, 5):
        time.sleep(0.1)
        analyzer.set(True, t0 + i * 0.1)
    last_positive = time.time()
    time.sleep(0.1)
    analyzer.set(False, t0 + 0.6)
    time.sleep(0.1)
    analyzer.set(False, t0 + 0.7)

    assert _wait_done(ctrl)
    releases = _releases(kb, ROLL_RIGHT_KEY)
    assert releases
    # Generous margin: must not have released before the last positive plus
    # (most of) the clear window.
    assert releases[0][2] - last_positive >= 0.2, (
        "hold ended measured from entry, not from the last positive"
    )


def test_roll_right_bracketed_and_grace_armed(monkeypatch):
    """d4: ROLL_RIGHT auto-repeats read as the player without the programmatic
    bracket; the guard must be up during the hold and grace-armed after."""
    analyzer = _FakeIncomingAnalyzer(detected=True)
    kb = _FakeKeyboard()
    ctrl = _make_ctrl(monkeypatch, kb, analyzer,
                      {"clear_seconds": 0.1, "min_clear_samples": 1, "max_hold_s": 0.5})

    ctrl.missile_evade_mode()
    time.sleep(0.15)  # mid-hold
    with ctrl._programmatic_key_lock:
        assert ctrl._programmatic_key_counts.get(ROLL_RIGHT_KEY, 0) == 1

    assert _wait_done(ctrl)
    with ctrl._programmatic_key_lock:
        assert ctrl._programmatic_key_counts.get(ROLL_RIGHT_KEY, 0) == 0
        assert ctrl._prog_release_grace_until.get(ROLL_RIGHT_KEY, 0.0) > time.time() - 0.1


def test_simulate_mode_records_intents(monkeypatch):
    """Replay lane: intents recorded, no keyboard touched."""
    analyzer = _FakeIncomingAnalyzer(detected=True)
    monkeypatch.setattr(controller_module, "keyboard_module", None)
    ctrl = Controller(
        (0, 0, 1920, 1200),
        analyzer=analyzer,
        exit_event=threading.Event(),
        capture=None,
        config=ControllerConfig(
            disable_hotkeys=True,
            simulate_os_input=True,
            missile_evade={"clear_seconds": 0.1, "min_clear_samples": 1, "max_hold_s": 0.4},
        )
    )
    ctrl.missile_evade_mode()
    assert _wait_done(ctrl)
    intents = [i for i in ctrl.get_action_intents() if i.get("action") == "missile_evade"]
    pressed = {i["key"] for i in intents if i["action_type"] == "key_press"}
    released = {i["key"] for i in intents if i["action_type"] == "key_release"}
    assert pressed == EVADE_KEYS
    assert released == EVADE_KEYS


def test_evade_yields_when_eject_starts_mid_hold(monkeypatch):
    """ADR 070 d11 — the defect observed 2026-08-12 05:34:50.

    Selection priority (d1) is NOT symmetric in time: it stops an evade
    STARTING under a selected Eject, but ConditionTactic.terminate is a no-op
    and the hold thread self-terminates on its own clear timer, so an eject
    beginning AFTER the evade had nothing to stop it. In the live session the
    evade held roll-right + yaw-left + burner for 4.8 s into an eject, which
    climbed to +55deg while its descent controller pulsed nose-down against it.
    """
    analyzer = _FakeIncomingAnalyzer(detected=True)  # stays positive: only eject ends this
    kb = _FakeKeyboard()
    ctrl = _make_ctrl(monkeypatch, kb, analyzer,
                      {"clear_seconds": 3.0, "min_clear_samples": 2, "max_hold_s": 15.0})

    ctrl.missile_evade_mode()
    time.sleep(0.15)
    assert ctrl.is_missile_evading()

    t_eject = time.time()
    ctrl._ejecting.set()  # eject_and_dive's thread body does exactly this

    assert _wait_done(ctrl, timeout=2.0), "evade did not yield to the eject"
    yielded_in = time.time() - t_eject
    assert yielded_in < 1.0, f"evade took {yielded_in:.2f}s to yield to the eject"
    for key in EVADE_KEYS:
        assert _releases(kb, key), f"'{key}' not released to the eject sequence"


def test_evade_does_not_start_while_ejecting(monkeypatch):
    """d11: eject owns the airframe — no evade may begin under it."""
    analyzer = _FakeIncomingAnalyzer(detected=True)
    kb = _FakeKeyboard()
    ctrl = _make_ctrl(monkeypatch, kb, analyzer,
                      {"clear_seconds": 0.1, "min_clear_samples": 1, "max_hold_s": 0.5})

    ctrl._ejecting.set()
    ctrl.missile_evade_mode()

    assert not ctrl.is_missile_evading()
    assert not kb.events, "keys were pressed despite an eject in progress"


def test_evade_release_cannot_desync_eject_afterburner(monkeypatch):
    """The reverse corruption d11 also closes: if the evade's finally released
    AFTERBURNER while an eject held it, the eject's _eject_ab_engaged flag
    would still read True and it would never re-press — burner silently off
    for the rest of the dive. Yielding fast is what prevents the overlap in
    which that can happen."""
    analyzer = _FakeIncomingAnalyzer(detected=True)
    kb = _FakeKeyboard()
    ctrl = _make_ctrl(monkeypatch, kb, analyzer,
                      {"clear_seconds": 3.0, "min_clear_samples": 2, "max_hold_s": 15.0})

    ctrl.missile_evade_mode()
    time.sleep(0.15)
    ctrl._ejecting.set()
    assert _wait_done(ctrl, timeout=2.0)

    # The eject's AB gate (engages only while descending) runs on a 1.5s
    # interval; the evade must be fully out well inside one such cycle.
    ab_releases = _releases(kb, AFTERBURNER_KEY)
    assert ab_releases, "afterburner never released"
    assert not ctrl._eject_ab_engaged, (
        "evade exited with the eject's afterburner flag set — the eject would "
        "believe the burner is on and never re-press it"
    )


def test_manoeuvre_limit_is_a_normal_exit(monkeypatch):
    """ADR 070 d12: past ~5 s the manoeuvre only bleeds energy (2026-08-12: a
    14 s hold traded 620 KPH for altitude and ended slow, high and level). The
    tactical limit ends it NORMALLY — it must not masquerade as the max_hold
    detector-fault backstop, or every long engagement logs a false fault."""
    t0 = time.time()
    analyzer = _FakeIncomingAnalyzer(detected=True, ts=t0)
    kb = _FakeKeyboard()
    ctrl = _make_ctrl(monkeypatch, kb, analyzer,
                      {"clear_seconds": 3.0, "min_clear_samples": 2,
                       "max_manoeuvre_s": 0.4, "max_hold_s": 15.0})

    ctrl.missile_evade_mode()
    # Keep the detection fresh and positive — only the manoeuvre limit can end this.
    for i in range(1, 8):
        time.sleep(0.1)
        analyzer.set(True, t0 + i * 0.1)

    assert _wait_done(ctrl, timeout=3.0), "manoeuvre limit never fired"
    releases = _releases(kb, ROLL_RIGHT_KEY)
    assert releases
    held = releases[0][2] - t0
    assert held < 2.0, f"held {held:.1f}s — the 15s backstop fired instead of the limit"
    for key in EVADE_KEYS:
        assert _releases(kb, key), f"'{key}' not released at the manoeuvre limit"


def test_pitch_down_variant_holds_and_brackets_nose_down(monkeypatch):
    """d13: the descending-break variant adds NOSE_DOWN. It is a WATCHED
    maneuver key, so it needs the same d4 programmatic bracket as ROLL_RIGHT —
    without it the hold self-cancels the mission into manual takeover."""
    from wingman.controller import NOSE_DOWN_KEY

    analyzer = _FakeIncomingAnalyzer(detected=True)
    kb = _FakeKeyboard()
    ctrl = _make_ctrl(monkeypatch, kb, analyzer,
                      {"clear_seconds": 0.1, "min_clear_samples": 1,
                       "max_manoeuvre_s": 0.4, "max_hold_s": 5.0,
                       "pitch_down": True})

    ctrl.missile_evade_mode()
    time.sleep(0.15)
    with ctrl._programmatic_key_lock:
        assert ctrl._programmatic_key_counts.get(NOSE_DOWN_KEY, 0) == 1, (
            "NOSE_DOWN held without the programmatic bracket — its XTest "
            "auto-repeats would read as the player and cancel the mission"
        )

    assert _wait_done(ctrl)
    assert _presses(kb, NOSE_DOWN_KEY), "pitch_down variant never pressed NOSE_DOWN"
    assert _releases(kb, NOSE_DOWN_KEY), "NOSE_DOWN never released"
    with ctrl._programmatic_key_lock:
        assert ctrl._programmatic_key_counts.get(NOSE_DOWN_KEY, 0) == 0
        assert ctrl._prog_release_grace_until.get(NOSE_DOWN_KEY, 0.0) > time.time() - 0.5


def test_pitch_down_off_by_default_leaves_nose_alone(monkeypatch):
    """The shipped triple is unchanged: d13 is opt-in."""
    from wingman.controller import NOSE_DOWN_KEY

    analyzer = _FakeIncomingAnalyzer(detected=True)
    kb = _FakeKeyboard()
    ctrl = _make_ctrl(monkeypatch, kb, analyzer,
                      {"clear_seconds": 0.1, "min_clear_samples": 1,
                       "max_manoeuvre_s": 0.4, "max_hold_s": 5.0})

    ctrl.missile_evade_mode()
    assert _wait_done(ctrl)
    assert not _presses(kb, NOSE_DOWN_KEY), "NOSE_DOWN pressed without pitch_down"


# ---------------------------------------------------------------------------
# ADR 075: burner release at 0% fuel inside the evade hold
# ---------------------------------------------------------------------------

class _FakeFuelIncomingAnalyzer(_FakeIncomingAnalyzer):
    """Incoming cache stand-in that also serves the fuel read."""

    def __init__(self, detected=True, ts=None, fuel=None):
        super().__init__(detected=detected, ts=ts)
        self._fuel = fuel

    def set_fuel(self, fuel):
        with self._lock:
            self._fuel = fuel

    def get_afterburner_fuel_pct(self):
        with self._lock:
            return self._fuel


def test_burner_released_at_zero_fuel_manoeuvre_keys_stay_held(monkeypatch):
    """At 0% the burner is off in-game and a held key blocks recharge — the
    hold must release AFTERBURNER while keeping the manoeuvre keys, then
    re-engage once the rearm margin refills."""
    analyzer = _FakeFuelIncomingAnalyzer(detected=True, fuel=0)
    kb = _FakeKeyboard()
    ctrl = _make_ctrl(monkeypatch, kb, analyzer,
                      {"clear_seconds": 30.0, "min_clear_samples": 2,
                       "max_hold_s": 30.0, "max_manoeuvre_s": 30.0})

    ctrl.missile_evade_mode()
    time.sleep(0.4)
    assert ctrl.is_missile_evading(), "evade ended prematurely"
    assert _releases(kb, AFTERBURNER_KEY), "burner not released at 0% fuel"
    assert not _releases(kb, ROLL_RIGHT_KEY), "manoeuvre key released with the burner"
    assert not _releases(kb, YAW_LEFT), "manoeuvre key released with the burner"

    analyzer.set_fuel(20)   # >= rearm margin (default 5)
    time.sleep(0.4)
    assert len(_presses(kb, AFTERBURNER_KEY)) >= 2, "burner not re-engaged after recharge"

    ctrl._me_stop.set()
    assert _wait_done(ctrl)


def test_unknown_fuel_keeps_burner_held_in_evade(monkeypatch):
    analyzer = _FakeFuelIncomingAnalyzer(detected=True, fuel=None)
    kb = _FakeKeyboard()
    ctrl = _make_ctrl(monkeypatch, kb, analyzer,
                      {"clear_seconds": 30.0, "min_clear_samples": 2,
                       "max_hold_s": 30.0, "max_manoeuvre_s": 30.0})

    ctrl.missile_evade_mode()
    time.sleep(0.3)
    assert ctrl.is_missile_evading()
    assert _presses(kb, AFTERBURNER_KEY), "burner never pressed"
    assert not _releases(kb, AFTERBURNER_KEY), "burner released without fuel evidence"

    ctrl._me_stop.set()
    assert _wait_done(ctrl)


def test_manual_takeover_state_ends_evade(monkeypatch):
    """SAF-001 backstop: the FSM entering GAME_BATTLE_MANUAL releases the
    hold — the operator owns the airframe, a stuck-positive detection must
    not keep flying it (the climb-hold analogue of the 2026-08-17 fix)."""
    from wingman.analyzer import GameState

    t0 = time.time()
    analyzer = _FakeIncomingAnalyzer(detected=True, ts=t0)   # stuck positive
    analyzer.game_state = GameState.GAME_BATTLE
    kb = _FakeKeyboard()
    ctrl = _make_ctrl(monkeypatch, kb, analyzer,
                      {"clear_seconds": 5.0, "min_clear_samples": 2, "max_hold_s": 10.0})

    ctrl.missile_evade_mode()
    assert ctrl.is_missile_evading()
    time.sleep(0.2)
    analyzer.game_state = GameState.GAME_BATTLE_MANUAL
    assert _wait_done(ctrl), "evade did not end on GAME_BATTLE_MANUAL"
    assert time.time() - t0 < 5.0, "evade ended by cap, not the state exit"
    for key in EVADE_KEYS:
        assert _releases(kb, key), f"'{key}' never released"
