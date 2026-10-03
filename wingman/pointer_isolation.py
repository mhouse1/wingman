"""Keep the operator's real mouse off the nested display's core pointer (ADR 156).

Xwayland 24.1.10 delivers XTest pointer events to a window only while the host
pointer is physically over the nested server's window. Its
`sprite_check_lost_focus` (hw/xwayland/xwayland-input.c) returns
`!pointer_crossing` for any event whose last slave is not the Wayland pointer,
and `xwl_xy_to_window` then reports the root window: every click reaches no
client (Anomaly 009). The check only applies when a Wayland pointer device
shares the core pointer's sprite, so DETACHING `xwayland-pointer` from the
Virtual core pointer (a floating slave gets a sprite of its own) switches it off.

Detach, never disable. `DisableDevice` frees the device's sprite, and Xwayland's
`pointer_handle_enter` dereferences it the next time the operator's mouse
crosses the window: the nested server segfaulted that way on 2026-10-02 05:21,
taking the game with it. A floating device stays enabled and every Xwayland
handler resolves it with `GetMaster(dev, POINTER_OR_FLOAT)`.

The nested display exists so the operator can use the machine while a session
runs (ADR 099), so the mouse having no effect on the game while wingman flies
is the isolation that lane already intends. `main` reattaches at shutdown.

python-xlib 0.15 has no XInput extension, so this talks to libXi through
ctypes. It opens its own short-lived connection per call and never touches the
shared XTest connection (ADR 091).
"""

import ctypes
import logging
import re
import sys
from typing import NamedTuple

logger = logging.getLogger(__name__)

# xwayland-pointer:N, xwayland-relative-pointer:N and xwayland-pointer-gestures:N.
# Only the first shares the sprite check, but all three steer the core pointer.
# Not the keyboard: the operator's hotkeys on the nested display come through it.
_HOST_POINTER_NAME = re.compile(r"^xwayland-(relative-)?pointer")

# XInput2 constants (X11/extensions/XI2.h).
_XI_ALL_DEVICES = 0
_XI_MASTER_POINTER = 1
_XI_SLAVE_POINTER = 3
_XI_FLOATING_SLAVE = 5
_XI_ATTACH_SLAVE = 3
_XI_DETACH_SLAVE = 4


class Device(NamedTuple):
    id: int
    name: str
    use: int
    attachment: int
    enabled: bool


class _XIDeviceInfo(ctypes.Structure):
    _fields_ = [
        ("deviceid", ctypes.c_int),
        ("name", ctypes.c_char_p),
        ("use", ctypes.c_int),
        ("attachment", ctypes.c_int),
        ("enabled", ctypes.c_int),
        ("num_classes", ctypes.c_int),
        ("classes", ctypes.c_void_p),
    ]


class _XIAddMasterInfo(ctypes.Structure):
    # Only here to give the union its real size (the largest member).
    _fields_ = [("type", ctypes.c_int), ("name", ctypes.c_char_p),
                ("send_core", ctypes.c_int), ("enable", ctypes.c_int)]


class _XIAttachSlaveInfo(ctypes.Structure):
    _fields_ = [("type", ctypes.c_int), ("deviceid", ctypes.c_int),
                ("new_master", ctypes.c_int)]


class _XIDetachSlaveInfo(ctypes.Structure):
    _fields_ = [("type", ctypes.c_int), ("deviceid", ctypes.c_int)]


class _XIAnyHierarchyChangeInfo(ctypes.Union):
    _fields_ = [("type", ctypes.c_int), ("add", _XIAddMasterInfo),
                ("attach", _XIAttachSlaveInfo), ("detach", _XIDetachSlaveInfo)]


def host_pointer_devices(devices):
    """The entries of `devices` that are Wayland-backed pointer devices."""
    return [d for d in devices if _HOST_POINTER_NAME.match(d.name)]


def planned_changes(devices, isolated: bool):
    """The hierarchy changes that take `devices` to the wanted state.

    Returns [(device, new_master_or_None)]: None detaches, an id attaches.
    Devices already in the wanted state, and disabled ones, are left alone.
    """
    master = next((d.id for d in devices if d.use == _XI_MASTER_POINTER), None)
    changes = []
    for d in host_pointer_devices(devices):
        if not d.enabled:
            continue
        if isolated and d.use == _XI_SLAVE_POINTER:
            changes.append((d, None))
        elif not isolated and d.use == _XI_FLOATING_SLAVE and master is not None:
            changes.append((d, master))
    return changes


def _load():
    x11 = ctypes.CDLL("libX11.so.6")
    xi = ctypes.CDLL("libXi.so.6")
    x11.XOpenDisplay.restype = ctypes.c_void_p
    x11.XOpenDisplay.argtypes = [ctypes.c_char_p]
    x11.XSync.argtypes = [ctypes.c_void_p, ctypes.c_int]
    x11.XCloseDisplay.argtypes = [ctypes.c_void_p]
    xi.XIQueryDevice.restype = ctypes.POINTER(_XIDeviceInfo)
    xi.XIQueryDevice.argtypes = [ctypes.c_void_p, ctypes.c_int,
                                 ctypes.POINTER(ctypes.c_int)]
    xi.XIFreeDeviceInfo.argtypes = [ctypes.POINTER(_XIDeviceInfo)]
    xi.XIChangeHierarchy.argtypes = [
        ctypes.c_void_p, ctypes.POINTER(_XIAnyHierarchyChangeInfo), ctypes.c_int]
    return x11, xi


def _query(xi, dpy):
    count = ctypes.c_int()
    info = xi.XIQueryDevice(dpy, _XI_ALL_DEVICES, ctypes.byref(count))
    if not info:
        return []
    try:
        return [Device(info[i].deviceid,
                       (info[i].name or b"").decode("utf-8", "replace"),
                       info[i].use, info[i].attachment, bool(info[i].enabled))
                for i in range(count.value)]
    finally:
        xi.XIFreeDeviceInfo(info)


def _apply(xi, dpy, changes):
    array = (_XIAnyHierarchyChangeInfo * len(changes))()
    for slot, (device, new_master) in zip(array, changes, strict=True):
        if new_master is None:
            slot.detach.type = _XI_DETACH_SLAVE
            slot.detach.deviceid = device.id
        else:
            slot.attach.type = _XI_ATTACH_SLAVE
            slot.attach.deviceid = device.id
            slot.attach.new_master = new_master
    xi.XIChangeHierarchy(dpy, array, len(changes))


def set_host_pointer_isolated(display_name: str, isolated: bool) -> list:
    """Detach (isolated) or reattach the Wayland-backed pointer devices.

    Returns the names of the devices it changed; empty when there was nothing
    to change or the display could not be reached. Never raises: a session must
    not fail to start, or to click, over this.
    """
    if sys.platform != "linux":
        return []
    try:
        x11, xi = _load()
    except OSError as e:
        logger.warning("Pointer isolation: libX11/libXi not loadable (%s)", e)
        return []
    dpy = x11.XOpenDisplay(display_name.encode())
    if not dpy:
        logger.warning("Pointer isolation: cannot open display %s", display_name)
        return []
    try:
        changes = planned_changes(_query(xi, dpy), isolated)
        if changes:
            _apply(xi, dpy, changes)
            x11.XSync(dpy, 0)
        return [device.name for device, _ in changes]
    except Exception as e:
        logger.warning("Pointer isolation on %s failed: %s", display_name, e)
        return []
    finally:
        x11.XCloseDisplay(dpy)
