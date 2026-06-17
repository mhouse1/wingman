"""Reposition or undecorate the MetalStorm Wine virtual desktop window safely.

Dragging the Wine virtual desktop window by hand (mouse-down on the GNOME title bar,
move, mouse-up) has caused full desktop freezes on GNOME Wayland requiring a hard
power-cycle (see docs/adr/054-gnome-wayland-freeze-on-wine-window-drag.md). This module
provides two X11-protocol-level operations that avoid Mutter's interactive move/resize
grab entirely:

    make move-game-window X=100 Y=100   # reposition via ConfigureWindow
    make undecorate-game-window         # strip the title bar (removes the drag handle)

undecorate-game-window is run automatically by `wait-game` so it applies on every
launch without requiring a manual step.
"""
import argparse
import os
import re
import subprocess
import sys

from wingman.controller import _ensure_xauthority

_GEOM_RE = re.compile(r'(\d+)x(\d+)\+\d+\+\d+\s+\+(\d+)\+(\d+)')

# _MOTIF_WM_HINTS: [flags, functions, decorations, input_mode, status]
# flags = MWM_HINTS_DECORATIONS (1 << 1); decorations = 0 (none) removes the
# title bar entirely, eliminating the drag handle that triggers the freeze.
_MWM_HINTS_DECORATIONS_FLAG = 1 << 1
_MWM_HINTS_NO_DECORATIONS = [_MWM_HINTS_DECORATIONS_FLAG, 0, 0, 0, 0]


def _find_game_window_id():
    """Return (window_id, title) for the Metalstorm / Wine Desktop window, or None."""
    result = subprocess.run(
        ["xwininfo", "-root", "-tree"],
        capture_output=True, text=True, timeout=5.0,
    )
    candidates = []
    for line in result.stdout.splitlines():
        if '"Metalstorm"' in line:
            priority = 0
        elif '"Wine Desktop"' in line and "steam_app_0" in line:
            priority = 1
        else:
            continue
        m = _GEOM_RE.search(line)
        if not m:
            continue
        wid_match = re.search(r'(0x[0-9a-fA-F]+)', line)
        if not wid_match:
            continue
        candidates.append((priority, wid_match.group(1), line.strip()))

    if not candidates:
        return None
    candidates.sort()
    _, wid, title = candidates[0]
    return wid, title


def _connect():
    _ensure_xauthority()
    from Xlib import display as xdisplay
    display_name = os.environ.get("DISPLAY", ":0").strip()
    return xdisplay.Display(display_name)


def move_window(x: int, y: int) -> bool:
    found = _find_game_window_id()
    if found is None:
        print("ERROR: Metalstorm / Wine Desktop window not found (is the game running?)", file=sys.stderr)
        return False
    wid, title = found
    print(f"Found window {wid} — {title}")

    d = _connect()
    window = d.create_resource_object("window", int(wid, 16))
    window.configure(x=x, y=y)
    d.sync()
    d.close()
    print(f"Moved window to ({x}, {y})")
    return True


def undecorate_window() -> bool:
    """Strip the title bar so there is no drag handle to grab.

    Does not block GNOME's Super+drag-anywhere move gesture — see ADR 054 for the
    residual risk. Removing the title bar eliminates the vector that caused the
    original freeze (manual title-bar drag).
    """
    found = _find_game_window_id()
    if found is None:
        print("ERROR: Metalstorm / Wine Desktop window not found (is the game running?)", file=sys.stderr)
        return False
    wid, title = found
    print(f"Found window {wid} — {title}")

    d = _connect()
    window = d.create_resource_object("window", int(wid, 16))
    motif_hints_atom = d.intern_atom("_MOTIF_WM_HINTS")
    window.change_property(motif_hints_atom, motif_hints_atom, 32, _MWM_HINTS_NO_DECORATIONS)
    d.sync()
    d.close()
    print(f"Stripped decorations from window {wid} (no title bar — nothing to drag)")
    return True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--undecorate", action="store_true",
                         help="Strip the title bar (removes the drag handle)")
    parser.add_argument("--x", type=int, help="Target absolute X position (with move)")
    parser.add_argument("--y", type=int, help="Target absolute Y position (with move)")
    args = parser.parse_args()

    if args.undecorate:
        ok = undecorate_window()
    elif args.x is not None and args.y is not None:
        ok = move_window(args.x, args.y)
    else:
        parser.error("either --undecorate, or both --x and --y, are required")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
