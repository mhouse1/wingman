"""ADR 156: detaching the nested display's host-pointer devices (Anomaly 009)."""

import ctypes

import pytest

from wingman import pointer_isolation
from wingman.pointer_isolation import Device

# The XInput device list of the nested Xwayland 24.1.10 server, as XIQueryDevice
# returned it on VEDA on 2026-10-02: id, name, use, attachment, enabled.
# use: 1 master pointer, 2 master keyboard, 3 slave pointer, 4 slave keyboard,
# 5 floating slave.
_NESTED_DEVICES = [
    Device(2, "Virtual core pointer", 1, 3, True),
    Device(3, "Virtual core keyboard", 2, 2, True),
    Device(4, "Virtual core XTEST pointer", 3, 2, True),
    Device(5, "Virtual core XTEST keyboard", 4, 3, True),
    Device(6, "xwayland-pointer:16", 3, 2, True),
    Device(7, "xwayland-relative-pointer:16", 3, 2, True),
    Device(8, "xwayland-pointer-gestures:16", 3, 2, True),
    Device(9, "xwayland-keyboard:16", 4, 3, True),
]


def _floated(devices, *ids):
    return [d._replace(use=5, attachment=0) if d.id in ids else d for d in devices]


def test_only_the_wayland_backed_pointer_devices_are_selected():
    """The XTEST pointer is how wingman clicks and the Wayland keyboard is how
    the operator's hotkeys arrive; neither may be touched."""
    assert [d.id for d in pointer_isolation.host_pointer_devices(_NESTED_DEVICES)] == [6, 7, 8]


def test_isolating_detaches_each_attached_host_pointer():
    changes = pointer_isolation.planned_changes(_NESTED_DEVICES, isolated=True)
    assert [(d.id, master) for d, master in changes] == [(6, None), (7, None), (8, None)]


def test_isolating_leaves_already_floating_devices_alone():
    devices = _floated(_NESTED_DEVICES, 6)
    changes = pointer_isolation.planned_changes(devices, isolated=True)
    assert [d.id for d, _ in changes] == [7, 8]


def test_restoring_reattaches_floating_devices_to_the_master_pointer():
    devices = _floated(_NESTED_DEVICES, 6, 7, 8)
    changes = pointer_isolation.planned_changes(devices, isolated=False)
    assert [(d.id, master) for d, master in changes] == [(6, 2), (7, 2), (8, 2)]


def test_a_disabled_device_is_never_touched():
    """Disabled is the state that crashed Xwayland on 2026-10-02 (its sprite is
    freed). This module must neither create it nor build on it."""
    devices = [d._replace(enabled=False, use=5, attachment=0) if d.id == 6 else d
               for d in _NESTED_DEVICES]
    assert 6 not in [d.id for d, _ in pointer_isolation.planned_changes(devices, True)]
    assert 6 not in [d.id for d, _ in pointer_isolation.planned_changes(devices, False)]


def test_the_module_cannot_disable_a_device():
    """ADR 156: detach, never disable. No "Device Enabled" property write exists."""
    import inspect
    source = inspect.getsource(pointer_isolation)
    assert "XIChangeProperty" not in source


def test_the_hierarchy_union_has_the_size_libXi_expects():
    """XIAnyHierarchyChangeInfo is as large as its largest member,
    XIAddMasterInfo (int, char *, Bool, Bool): 24 bytes on a 64-bit host. A
    smaller element would make XIChangeHierarchy read past each entry."""
    expected = 24 if ctypes.sizeof(ctypes.c_void_p) == 8 else 16
    assert ctypes.sizeof(pointer_isolation._XIAnyHierarchyChangeInfo) == expected


class _FakeX11:
    def __init__(self, opens=True):
        self.opens = opens
        self.closed = 0
        self.synced = 0

    def XOpenDisplay(self, name):
        return 0xD15 if self.opens else None

    def XSync(self, dpy, discard):
        self.synced += 1

    def XCloseDisplay(self, dpy):
        self.closed += 1


class _FakeXi:
    def __init__(self):
        self.calls = []

    def XIChangeHierarchy(self, dpy, array, count):
        self.calls.append([
            (array[i].type, array[i].detach.deviceid,
             array[i].attach.new_master if array[i].type == 3 else None)
            for i in range(count)])


@pytest.fixture
def fake_libs(monkeypatch):
    x11, xi = _FakeX11(), _FakeXi()
    state = {"devices": list(_NESTED_DEVICES)}
    monkeypatch.setattr(pointer_isolation.sys, "platform", "linux")
    monkeypatch.setattr(pointer_isolation, "_load", lambda: (x11, xi))
    monkeypatch.setattr(pointer_isolation, "_query", lambda _xi, _dpy: state["devices"])
    return x11, xi, state


def test_isolate_sends_one_detach_request_for_all_three(fake_libs):
    x11, xi, _ = fake_libs
    changed = pointer_isolation.set_host_pointer_isolated(":3", True)

    assert changed == ["xwayland-pointer:16", "xwayland-relative-pointer:16",
                       "xwayland-pointer-gestures:16"]
    assert xi.calls == [[(4, 6, None), (4, 7, None), (4, 8, None)]]   # XIDetachSlave
    assert x11.synced == 1 and x11.closed == 1


def test_restore_sends_attach_requests_to_the_master_pointer(fake_libs):
    _, xi, state = fake_libs
    state["devices"] = _floated(_NESTED_DEVICES, 6, 7, 8)
    changed = pointer_isolation.set_host_pointer_isolated(":3", False)

    assert len(changed) == 3
    assert xi.calls == [[(3, 6, 2), (3, 7, 2), (3, 8, 2)]]            # XIAttachSlave


def test_nothing_to_change_sends_no_request(fake_libs):
    x11, xi, state = fake_libs
    state["devices"] = _floated(_NESTED_DEVICES, 6, 7, 8)
    assert pointer_isolation.set_host_pointer_isolated(":3", True) == []
    assert xi.calls == [] and x11.closed == 1


def test_an_unreachable_display_changes_nothing_and_does_not_raise(monkeypatch):
    x11, xi = _FakeX11(opens=False), _FakeXi()
    monkeypatch.setattr(pointer_isolation.sys, "platform", "linux")
    monkeypatch.setattr(pointer_isolation, "_load", lambda: (x11, xi))
    assert pointer_isolation.set_host_pointer_isolated(":3", True) == []
    assert xi.calls == [] and x11.closed == 0


def test_missing_libraries_change_nothing_and_do_not_raise(monkeypatch):
    def _boom():
        raise OSError("libXi.so.6: cannot open shared object file")
    monkeypatch.setattr(pointer_isolation.sys, "platform", "linux")
    monkeypatch.setattr(pointer_isolation, "_load", _boom)
    assert pointer_isolation.set_host_pointer_isolated(":3", True) == []


def test_not_linux_is_a_noop(monkeypatch):
    monkeypatch.setattr(pointer_isolation.sys, "platform", "win32")
    monkeypatch.setattr(pointer_isolation, "_load",
                        lambda: pytest.fail("must not load X libraries off Linux"))
    assert pointer_isolation.set_host_pointer_isolated(":3", True) == []
