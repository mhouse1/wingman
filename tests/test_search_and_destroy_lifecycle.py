"""start/stop of the search-and-destroy loops must not race. ADR 118.

Measured 2026-09-05 01:05:11, in a 16-minute tail where the HUD was gone and
Disengage was cycling the loops repeatedly:

    01:05:11,423  search_and_destroy padlock loop stopped
    01:05:11,424  search_and_destroy padlock loop started
    01:05:11,424  [ERROR] Controller: mission_j20 failed
                  RuntimeError: cannot join thread before it is started
    01:05:11,426  search_and_destroy weapon loop started

The start assigns both Thread objects and only then calls .start(). A stop that
landed in that window joined a thread that had never been started, which raised
and took mission_j20 down with it.
"""

import threading

from wingman.controller import Controller


def _ctrl():
    c = Controller.__new__(Controller)
    c._sdl_lifecycle_lock = threading.Lock()
    c._sdl_lifecycle_timeout_s = 2.0
    c._sdl_stop = threading.Event()
    c._sdl_padlock_thread = None
    c._sdl_weapon_thread = None
    return c


def test_stopping_an_unstarted_thread_does_not_raise():
    """The exact live failure: a Thread object assigned but never started."""
    c = _ctrl()
    c._sdl_weapon_thread = threading.Thread(target=lambda: None, daemon=True)
    c._sdl_padlock_thread = threading.Thread(target=lambda: None, daemon=True)
    c.stop_search_and_destroy_loop()          # must not raise
    assert c._sdl_weapon_thread is None
    assert c._sdl_padlock_thread is None


def test_stopping_a_started_thread_still_joins_it():
    """The guard must not turn the stop into a no-op — a running loop still has
    to be waited for, or the next start races the old thread."""
    running = threading.Event()
    stop = threading.Event()

    def _loop():
        running.set()
        stop.wait(timeout=5.0)

    c = _ctrl()
    t = threading.Thread(target=_loop, daemon=True)
    t.start()
    running.wait(timeout=2.0)
    c._sdl_weapon_thread = t
    stop.set()
    c.stop_search_and_destroy_loop()
    assert not t.is_alive(), "returned before the loop had finished"
    assert c._sdl_weapon_thread is None


def test_a_second_stop_is_harmless():
    c = _ctrl()
    c.stop_search_and_destroy_loop()
    c.stop_search_and_destroy_loop()


def test_stop_releases_the_lifecycle_lock_on_every_path():
    """A stop that returned early must not leave the lock held — the next start
    would silently skip, and the loops would never run again."""
    c = _ctrl()
    c._sdl_stop.set()                 # the "not running" early-return path
    c.stop_search_and_destroy_loop()
    assert not c._sdl_lifecycle_lock.locked(), "lock leaked on the early return"


def test_start_and_stop_are_serialised():
    """The lock is what makes the assign-then-start sequence atomic against a
    concurrent stop. Without it the two interleave, which is the live bug."""
    c = _ctrl()
    assert c._sdl_lifecycle_lock.acquire(timeout=1.0)
    try:
        # A stop arriving while the start holds the lock must decline rather
        # than reach into half-built state.
        c._sdl_weapon_thread = threading.Thread(target=lambda: None, daemon=True)
        c.stop_search_and_destroy_loop()
        assert c._sdl_weapon_thread is not None, \
            "stop touched the threads while the start held the lock"
    finally:
        c._sdl_lifecycle_lock.release()
