"""ADR 157: modifier keys left held on the nested display are released."""

import pytest

from wingman import input_linux
from wingman.input_linux import StuckModifierGuard

# Keycodes as the nested Xwayland server reported them on VEDA, 2026-10-02
# (Alt_L 64 and Return 36 were read live; the rest are the evdev layout's).
_KEYCODES = {"Alt_L": 64, "Alt_R": 108, "Super_L": 133, "Super_R": 134,
             "Control_L": 37, "Control_R": 105, "Shift_L": 50, "Shift_R": 62,
             "e": 26, "Return": 36}


class _FakeDisplay:
    """A display with a key-down table, like the X server's."""

    def __init__(self):
        self.down = set()
        self.syncs = 0

    def keysym_to_keycode(self, keysym):
        from Xlib import XK
        for name, code in _KEYCODES.items():
            if XK.string_to_keysym(name) == keysym:
                return code
        return 0

    def query_keymap(self):
        keymap = [0] * 32
        for kc in self.down:
            keymap[kc >> 3] |= 1 << (kc & 7)
        return keymap

    def sync(self):
        self.syncs += 1


# X11 core protocol event code, as in Xlib.X. Xlib is imported inside the tests,
# never at module level: collection must not load the real submodules ahead of
# test_input_linux's fake ones.
_KEY_RELEASE = 3


@pytest.fixture
def rig(monkeypatch):
    import Xlib.ext.xtest
    from Xlib import X
    assert X.KeyRelease == _KEY_RELEASE
    fake = _FakeDisplay()
    sent = []
    clock = {"now": 100.0}

    def fake_input(d, event_type, detail=0, **_kw):
        sent.append((event_type, detail))
        if event_type == _KEY_RELEASE:
            d.down.discard(detail)

    monkeypatch.setattr(input_linux, "_shared_xtest_display", lambda name: fake)
    monkeypatch.setattr(Xlib.ext.xtest, "fake_input", fake_input)
    guard = StuckModifierGuard(":3", after_s=1.5, clock=lambda: clock["now"])
    return guard, fake, sent, clock


def test_alt_left_held_past_the_threshold_gets_a_real_release(rig, caplog):
    """2026-10-02 07:00:53: Alt_L sat held on :3 for 6.3 s after an Alt+Tab
    away, and the next bare Enter framed the game window."""
    guard, fake, sent, clock = rig
    fake.down.add(64)
    with caplog.at_level("INFO", logger=input_linux.logger.name):
        assert guard.check() == []          # first sighting starts the timer
        clock["now"] += 1.0
        assert guard.check() == []          # a held chord is left alone
        clock["now"] += 0.6
        assert guard.check() == ["Alt_L"]

    assert sent == [(_KEY_RELEASE, 64)]
    assert 64 not in fake.down
    assert "released Alt_L on :3 — held 1.6s" in caplog.text


def test_a_normal_chord_is_never_touched(rig):
    guard, fake, sent, clock = rig
    fake.down.add(64)
    guard.check()
    clock["now"] += 0.8
    fake.down.discard(64)                   # the operator let go themselves
    guard.check()
    clock["now"] += 5.0
    assert guard.check() == []
    assert sent == []


def test_the_timer_restarts_for_each_press(rig):
    guard, fake, sent, clock = rig
    fake.down.add(64)
    guard.check()
    clock["now"] += 1.0
    fake.down.discard(64)
    guard.check()
    fake.down.add(64)                       # pressed again
    guard.check()
    clock["now"] += 1.0                     # 1.0 s into the second press
    assert guard.check() == []
    assert sent == []


def test_keys_wingman_holds_are_not_modifiers_and_stay_down(rig):
    """Wingman holds 'e' (afterburner) for many seconds; it must survive."""
    guard, fake, sent, clock = rig
    fake.down.update({26, 36})
    guard.check()
    clock["now"] += 30.0
    assert guard.check() == []
    assert fake.down == {26, 36} and sent == []


def test_every_host_shortcut_modifier_is_covered(rig):
    """Ctrl+Alt+arrow and Super leave more than Alt behind."""
    guard, fake, sent, clock = rig
    fake.down.update({64, 37, 133, 50})
    guard.check()
    clock["now"] += 2.0
    assert sorted(guard.check()) == ["Alt_L", "Control_L", "Shift_L", "Super_L"]
    assert fake.down == set()


def test_a_failing_display_is_dropped_and_the_thread_survives(monkeypatch):
    dropped = []

    def boom(_name):
        raise ConnectionError("Display connection closed by server")

    monkeypatch.setattr(input_linux, "_shared_xtest_display", boom)
    monkeypatch.setattr(input_linux, "_drop_shared_display", lambda: dropped.append(1))
    guard = StuckModifierGuard(":3", poll_s=0.01)
    with pytest.raises(ConnectionError):
        guard.check()
    assert dropped == [1]

    guard.start()                            # the loop must swallow the same error
    import time
    time.sleep(0.05)
    assert guard._thread.is_alive()
    guard.stop()
    assert guard._thread is None


def test_stop_ends_the_thread_promptly(rig):
    guard, _fake, _sent, _clock = rig
    guard.start()
    thread = guard._thread
    guard.stop()
    assert not thread.is_alive()
