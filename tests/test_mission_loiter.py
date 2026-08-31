"""mission_loiter: the survival hold (behaviour-driven).

The previous implementation was thirteen scripted manoeuvres run once, with no
feedback: it could not tell whether it had gained altitude, dropped flares on a
timer rather than on a threat, and ended after ~90 s leaving the aircraft
wherever the script had put it.
"""

import threading
import time
import unittest.mock as mock



class _Snap:
    def __init__(self, alt, fresh=True):
        self.altitude = type("A", (), {"stable": alt})()
        self._fresh = fresh

    @property
    def altitude_fresh(self):
        return self._fresh


def _loiter_ctrl(snaps):
    """Controller stub running only the loiter loop."""
    from wingman.controller import Controller
    c = Controller.__new__(Controller)
    c._mission_lock = threading.Lock()
    c._mission_complete = threading.Event()
    c._mission_cancel = threading.Event()
    c._climb_stop = threading.Event()
    c._exit_event = threading.Event()
    c._analyzer = mock.MagicMock()
    c._analyzer.get_telemetry.side_effect = list(snaps) + [snaps[-1]] * 200
    c._loiter_target_alt = 7000.0
    c._loiter_hysteresis_m = 500.0
    c._loiter_orbit_direction = "right"
    c._loiter_orbit_interval_s = 0.0     # every tick, for the test
    c._loiter_orbit_hold_s = 0.6
    c._loiter_tick_s = 0.01
    c.climb_mode = mock.MagicMock()
    c.roll_right = mock.MagicMock()
    c.roll_left = mock.MagicMock()
    return c


def _run_briefly(c, seconds=0.2):
    t = threading.Thread(target=c.mission_loiter, daemon=True)
    t.start()
    time.sleep(seconds)
    c.cancel_mission = lambda: None
    c._mission_cancel.set()
    t.join(timeout=2.0)


def test_below_the_band_it_climbs():
    c = _loiter_ctrl([_Snap(2000)])
    _run_briefly(c)
    assert c.climb_mode.called, "below the hold band the objective is altitude"
    assert not c.roll_right.called


def test_inside_the_band_it_orbits():
    c = _loiter_ctrl([_Snap(7200)])
    _run_briefly(c)
    assert c.roll_right.called, "at altitude the objective is to keep turning"
    assert not c.climb_mode.called


def test_the_hysteresis_prevents_chattering_at_the_edge():
    """6600 m is below target but inside the band — climbing there would fight
    the orbit every few seconds."""
    c = _loiter_ctrl([_Snap(6600)])
    _run_briefly(c)
    assert not c.climb_mode.called
    assert c.roll_right.called


def test_stale_telemetry_commands_nothing():
    """A climb ordered on an aged-out reading is a climb ordered blind."""
    c = _loiter_ctrl([_Snap(2000, fresh=False)])
    _run_briefly(c)
    assert not c.climb_mode.called
    assert not c.roll_right.called and not c.roll_left.called


def test_it_keeps_holding_rather_than_running_a_sequence_once():
    """The old script ended after ~90 s. A survival hold that stops holding is
    not one."""
    c = _loiter_ctrl([_Snap(7200)])
    _run_briefly(c, seconds=0.3)
    assert c.roll_right.call_count > 1


def test_cancel_stops_the_loop_and_any_climb():
    c = _loiter_ctrl([_Snap(2000)])
    _run_briefly(c)
    assert c._mission_complete.is_set()
    assert c._climb_stop.is_set(), "a climb left running would keep the pitch axis"
    assert not c._mission_lock.locked()


def test_flares_are_not_on_a_timer():
    import pathlib
    src = pathlib.Path("wingman/controller.py").read_text()
    body = src[src.index("def mission_loiter"):src.index("def mission_loiter") + 6000]
    body = body[:body.index("\n    def ", 100)]
    assert "deploy_flares" not in body, \
        "flares belong to the incoming detector, which knows when one is inbound"


# --- the pin_memory warning is silenced, and only that one ------------------

def test_the_pin_memory_warning_is_filtered_by_message():
    """EasyOCR sets pin_memory=True unconditionally and torch warns on a
    CPU-only host — correct, and unactionable, since CPU-only is the design
    (ADR 020).

    It repeated because heap_census uses warnings.catch_warnings(), and leaving
    that block bumps the global filters version, invalidating every module's
    __warningregistry__ and defeating the once-per-location dedupe.

    Filtered by MESSAGE rather than by muting UserWarning or the torch module,
    so an unrelated warning from the same place still reaches the operator."""
    import pathlib
    src = pathlib.Path("wingman/main.py").read_text()
    i = src.index("warnings.filterwarnings(")
    call = src[i:i + 240]
    assert "pin_memory" in call
    assert "category=UserWarning" in call
    assert 'module=' not in call, "muting the whole module would hide real warnings"


def test_the_filter_survives_a_catch_warnings_block():
    """The dedupe reset is what made this warning repeat, so the fix has to
    hold across one. Warnings are recorded rather than read off stderr: pytest
    installs its own capture, so redirecting stderr sees nothing."""
    import warnings
    msg = ("'pin_memory' argument is set as true but no accelerator is found, "
           "then device pinned memory won't be used.")
    with warnings.catch_warnings(record=True) as rec:
        warnings.resetwarnings()
        # First match wins and filterwarnings PREPENDS, so the catch-all is
        # registered first and the ignore second, leaving the ignore in front.
        warnings.filterwarnings("always")
        warnings.filterwarnings(
            "ignore", message=r".*pin_memory.*no accelerator is found.*",
            category=UserWarning)
        warnings.warn(msg, UserWarning, stacklevel=2)
        with warnings.catch_warnings():
            pass                      # the thing that reset the dedupe
        warnings.warn(msg, UserWarning, stacklevel=2)
        warnings.warn("an unrelated torch warning", UserWarning, stacklevel=2)
    texts = [str(w.message) for w in rec]
    assert not any("pin_memory" in t for t in texts), texts
    assert any("an unrelated torch warning" in t for t in texts), texts
