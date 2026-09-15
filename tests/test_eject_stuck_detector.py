"""Anomaly 003 — GAME_BATTLE_EJECT stuck-detector.

2026-09-13: a transient telemetry gap during an eject got classified as
death; the actual harm (BT stuck on Idle, zero actuation) ran 63.3s, almost
entirely AFTER telemetry had already recovered. Two earlier signal choices
(dwell + no-observed-death + climb; then telemetry-gap duration alone) were
tried and rejected — see docs/anomaly/003-eject-no-telemetry-false-death-
leaves-aircraft-unflown.md's "Signal — revised twice".

A THIRD signal choice — raw GAME_BATTLE_EJECT dwell, no gate on descent
control state — was implemented, gated behind --record-session, and
live-tested the same day. It false-positived in its first session: it fired
on a legitimately long, still-actively-diving eject (health critical,
visibly banking hard in the recorded video, ttg=10s, altitude falling
continuously the whole time) that had nothing wrong with it — the dive just
took longer than the threshold. The corrected signal requires descent
control to have ALREADY given up (`eject_descent_active is False`) before
the clock starts — that is the actual moment nothing is flying the
aircraft, and it is what the two tests below at the bottom of this file
pin: the false positive must never recur, and the true incident must still
be caught.
"""

from wingman.analyzer import GameState
from wingman.eject_stuck_detector import EjectStuckDetector


class _Clock:
    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, s):
        self.now += s


def _detector(**cfg):
    clock = _Clock()
    cfg.setdefault("enabled", True)
    cfg.setdefault("eject_stuck_after_s", 40.0)
    return EjectStuckDetector(cfg, clock=clock), clock


def test_quiet_outside_eject():
    d, clock = _detector()
    clock.advance(100)
    assert d.check(GameState.GAME_BATTLE, False) is False


def test_quiet_while_descent_control_is_still_active():
    """The 2026-09-13 false positive, pinned directly: however long a
    still-actively-diving eject takes, this must never fire while
    eject_descent_active is True."""
    d, clock = _detector()
    d.check(GameState.GAME_BATTLE_EJECT, True)
    for _ in range(200):
        clock.advance(1.0)
        assert d.check(GameState.GAME_BATTLE_EJECT, True) is False


def test_quiet_below_the_threshold_once_descent_control_gives_up():
    d, clock = _detector()
    d.check(GameState.GAME_BATTLE_EJECT, False)   # arms the clock
    clock.advance(39)
    assert d.check(GameState.GAME_BATTLE_EJECT, False) is False


def test_fires_past_the_threshold_once_descent_control_gives_up():
    d, clock = _detector()
    d.check(GameState.GAME_BATTLE_EJECT, False)   # arms the clock
    clock.advance(41)
    assert d.check(GameState.GAME_BATTLE_EJECT, False) is True


def test_a_long_active_dive_then_a_genuine_stall_is_still_caught():
    """The realistic shape: descent control runs active (any duration, even
    past the threshold) — must be silent throughout — THEN gives up, and
    only then does the clock start."""
    d, clock = _detector()
    d.check(GameState.GAME_BATTLE_EJECT, True)
    for _ in range(60):   # a long, legitimate, still-active dive
        clock.advance(1.0)
        assert d.check(GameState.GAME_BATTLE_EJECT, True) is False
    d.check(GameState.GAME_BATTLE_EJECT, False)   # descent control gives up; clock arms
    clock.advance(41)
    assert d.check(GameState.GAME_BATTLE_EJECT, False) is True


def test_the_real_incident_shape_is_caught_with_margin():
    """59:09.423 (descent control ended) to 59:52.630 (operator interrupt)
    = 43.2s. The 40s default must fire well before that, not merely
    eventually."""
    d, clock = _detector()
    d.check(GameState.GAME_BATTLE_EJECT, False)
    fired_at = None
    for _ in range(44):
        clock.advance(1.0)
        if d.check(GameState.GAME_BATTLE_EJECT, False):
            fired_at = clock.now - 1000.0
            break
    assert fired_at is not None
    assert fired_at < 43.2, "must fire before the operator would have had to intervene"


def test_leaving_the_state_resets_the_clock():
    d, clock = _detector()
    d.check(GameState.GAME_BATTLE_EJECT, False)
    clock.advance(35)
    assert d.check(GameState.GAME_BATTLE, False) is False   # a healthy, quick resolution
    clock.advance(1000)
    # Re-entering later must not inherit the earlier eject's clock.
    d.check(GameState.GAME_BATTLE_EJECT, False)
    clock.advance(35)
    assert d.check(GameState.GAME_BATTLE_EJECT, False) is False


def test_descent_control_becoming_active_again_resets_the_clock():
    """Not observed in practice, but the detector must not let a stray
    active-descent tick after a stall silently keep an old clock running."""
    d, clock = _detector()
    d.check(GameState.GAME_BATTLE_EJECT, False)
    clock.advance(35)
    assert d.check(GameState.GAME_BATTLE_EJECT, True) is False
    clock.advance(10)
    assert d.check(GameState.GAME_BATTLE_EJECT, False) is False, \
        "the clock must have restarted, not resumed from 35"


def test_a_second_eject_in_the_same_session_gets_its_own_clock():
    """The first eject resolves healthily well under the threshold; a LATER,
    unrelated eject must not inherit any state from the first."""
    d, clock = _detector()
    d.check(GameState.GAME_BATTLE_EJECT, False)
    clock.advance(5)
    assert d.check(GameState.GAME_BATTLE, False) is False
    clock.advance(500)
    d.check(GameState.GAME_BATTLE_EJECT, False)
    clock.advance(41)
    assert d.check(GameState.GAME_BATTLE_EJECT, False) is True


def test_fires_at_most_once():
    d, clock = _detector()
    d.check(GameState.GAME_BATTLE_EJECT, False)
    clock.advance(41)
    assert d.check(GameState.GAME_BATTLE_EJECT, False) is True
    clock.advance(1000)
    assert d.check(GameState.GAME_BATTLE_EJECT, False) is False


def test_disabled_never_fires():
    d, clock = _detector(enabled=False)
    d.check(GameState.GAME_BATTLE_EJECT, False)
    clock.advance(10_000)
    assert d.check(GameState.GAME_BATTLE_EJECT, False) is False
