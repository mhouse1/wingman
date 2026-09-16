"""Anomaly 003 (2026-09-13 live-test correction): Controller.eject_descent_active.

Added after EjectStuckDetector false-positived on a legitimately long,
still-actively-diving eject in its first live session. This is the signal
that distinguishes "still flying the dive" from "gave up, doing nothing" —
_eject_phase_exit_reason is set the instant _eject_descent_control exits,
for any reason, which is the true moment nothing is flying the aircraft.
"""

import threading

from wingman.controller import Controller


def _ctrl(ejecting, exit_reason):
    c = Controller.__new__(Controller)
    c._ejecting = threading.Event()
    if ejecting:
        c._ejecting.set()
    c._eject_phase_exit_reason = exit_reason
    return c


def test_active_while_descent_control_has_not_exited():
    assert _ctrl(ejecting=True, exit_reason="").eject_descent_active() is True


def test_inactive_once_any_exit_reason_is_set():
    for reason in ("no_telemetry", "established", "timeout", "over_rotation",
                  "pulses_exhausted", "rearmed", "cancelled"):
        assert _ctrl(ejecting=True, exit_reason=reason).eject_descent_active() is False, reason


def test_inactive_when_no_eject_is_in_progress():
    assert _ctrl(ejecting=False, exit_reason="").eject_descent_active() is False
