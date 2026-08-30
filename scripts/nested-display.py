#!/usr/bin/env python3
"""Start and focus the nested display lane (ADR 099).

Wingman injects at the X server's *focus* and captures the *screen*, so both
paths are positional: whatever the operator is looking at is what wingman types
into and reads from. ADR 098 removed the resulting corruption by suppressing
injection on alt-tab, which protects the operator's files at the cost of the
session. The nested lane removes the shared channel instead — the game gets its
own X display, where it is the only client and therefore always focused, and
whose root window is a real framebuffer that XGetImage can read.

The server must be a *rootful Xwayland*, and the choice is not stylistic:

  Xephyr    provides glamor for 2D but implements no DRI3, and DXVK requires
            DRI3 to present. The game does not start at all - it exits with
            "vulkan: No DRI3 support detected". There is no flag for this.
  Xwayland  has DRI3, because it is what already serves every X11 game on this
            machine. Run *rootful* rather than rootless it also keeps a real
            root framebuffer, which is the half Xephyr would have got right.

Usage:
    nested-display.py start   [--display :3] [--size 1920x1200]
    nested-display.py focus   [--display :3] [--timeout 30]
    nested-display.py status  [--display :3]
    nested-display.py stop    [--display :3]

`start` is idempotent: a display that already answers is left alone, so it is
safe as a Makefile prerequisite on every run.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ADR 098 D2: identify the game by its Wine session, never by window title. A
# title test is not merely weaker here, it is the specific trap that ADR's probe
# caught - an editor window titled "...Metalstorm..." satisfies a substring test
# with the game shut down. Reused rather than reimplemented.
from wingman.focus_guard import game_session_pids  # noqa: E402

DEFAULT_DISPLAY = ":3"
DEFAULT_SIZE = "1920x1200"
SERVER_LOG = "/tmp/wingman-nested-display.log"
CONFIG_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "wingman", "config.yaml")


def load_nested_config(override=None) -> dict:
    """The `nested:` section of config.yaml, with an optional override.

    config.yaml is the source of truth so that `make r` / `make rd` and wingman
    itself agree on the lane without a second switch to keep in sync. `override`
    is the per-run escape hatch (`make rd NESTED=0`) - needed because a single
    global flag would otherwise force two simultaneous accounts into one lane.
    """
    cfg = {}
    try:
        import yaml
        with open(CONFIG_PATH) as fh:
            cfg = (yaml.safe_load(fh) or {}).get("nested", {}) or {}
    except Exception:
        cfg = {}
    enabled = bool(cfg.get("enabled", False))
    if override is not None and str(override).strip() != "":
        enabled = str(override).strip().lower() in ("1", "true", "yes", "on")
    return {
        "enabled": enabled,
        "display": str(cfg.get("display") or DEFAULT_DISPLAY).strip(),
        "size": str(cfg.get("size") or DEFAULT_SIZE).strip(),
    }


def display_is_up(display: str) -> bool:
    """True when an X server answers on `display`."""
    try:
        from Xlib import display as xdisplay
        d = xdisplay.Display(display)
        d.close()
        return True
    except Exception:
        return False


def start(display: str, size: str) -> int:
    """Bring up a rootful Xwayland on `display`. Idempotent."""
    if display_is_up(display):
        print(f"nested display {display} already running")
        return 0

    if not os.environ.get("WAYLAND_DISPLAY"):
        # Xwayland is a Wayland client: it needs the operator's compositor to
        # attach its root window to. Stripping WAYLAND_DISPLAY is correct for
        # the *game* (it stops Wine choosing winewayland.drv and bypassing the
        # nested display) but fatal here, so the two must not share an env.
        print("ERROR: WAYLAND_DISPLAY is unset - Xwayland has no compositor to "
              "attach to. Run this target without the nested env.", file=sys.stderr)
        return 1

    log = open(SERVER_LOG, "ab", buffering=0)
    try:
        subprocess.Popen(
            ["Xwayland", display, "-geometry", size, "-decorate"],
            stdout=log, stderr=log,
            start_new_session=True,   # outlive the make recipe that spawned us
        )
    except FileNotFoundError:
        print("ERROR: Xwayland not found on PATH", file=sys.stderr)
        return 1

    for _ in range(30):
        time.sleep(0.5)
        if display_is_up(display):
            print(f"nested display {display} up ({size}, log: {SERVER_LOG})")
            return 0
    print(f"ERROR: {display} did not come up within 15 s - see {SERVER_LOG}",
          file=sys.stderr)
    return 1


def _game_windows(d, session: "set[int]") -> list:
    """Top-level windows on `d` owned by a process in the game's Wine session."""
    from Xlib import X, Xatom
    net_wm_pid = d.intern_atom("_NET_WM_PID")
    found = []
    try:
        children = d.screen().root.query_tree().children
    except Exception:
        return found
    for w in children:
        try:
            prop = w.get_full_property(net_wm_pid, Xatom.CARDINAL)
        except Exception:
            continue
        if prop and prop.value and int(prop.value[0]) in session:
            try:
                geom = w.get_geometry()
                found.append((geom.width * geom.height, w))
            except Exception:
                continue
    # Largest first: the Wine virtual desktop, not a 1x1 IME helper window.
    found.sort(key=lambda t: t[0], reverse=True)
    return [w for _, w in found]


def focus(display: str, timeout: float) -> int:
    """Point X input focus at the game window.

    There is no window manager on the nested display, so focus defaults to
    PointerRoot and keys route to whatever sits under the nested pointer. That
    is not a stable target, so focus is set explicitly.
    """
    from Xlib import X, display as xdisplay

    if not display_is_up(display):
        print(f"ERROR: {display} is not running", file=sys.stderr)
        return 1

    try:
        d = xdisplay.Display(display)
    except Exception as e:
        print(f"ERROR: cannot connect to {display}: {e}", file=sys.stderr)
        return 1
    deadline = time.monotonic() + timeout
    while True:
        session = game_session_pids()
        wins = _game_windows(d, session) if session else []
        if wins:
            win = wins[0]
            d.set_input_focus(win, X.RevertToParent, X.CurrentTime)
            d.sync()
            try:
                name = win.get_wm_name()
            except Exception:
                name = None
            print(f"nested focus set to {hex(win.id)} ({name!r}) on {display}")
            return 0
        if time.monotonic() >= deadline:
            break
        time.sleep(1.0)

    print(f"ERROR: no game window found on {display} within {timeout:.0f} s",
          file=sys.stderr)
    return 1


def status(display: str) -> int:
    """Report whether the lane is up and what currently holds focus."""
    if not display_is_up(display):
        print(f"nested display {display}: DOWN")
        return 1
    from Xlib import display as xdisplay
    try:
        d = xdisplay.Display(display)
    except Exception as e:
        # The server can die between the probe above and this connect. Report
        # it as DOWN rather than spilling an Xlib traceback at the operator.
        print(f"nested display {display}: DOWN ({type(e).__name__})")
        return 1
    session = game_session_pids()
    wins = _game_windows(d, session) if session else []
    try:
        focused = d.get_input_focus().focus
        fname = focused.get_wm_name() if hasattr(focused, "get_wm_name") else None
    except Exception:
        fname = None
    print(f"nested display {display}: UP")
    print(f"  game session pids : {len(session)}")
    print(f"  game windows      : {len(wins)}")
    print(f"  input focus       : {fname!r}")
    return 0


def stop(display: str) -> int:
    """Kill the Xwayland serving `display`.

    Delegates to wingman so the teardown has ONE definition and one matching
    rule. This previously ran `pkill -f "Xwayland :N"`, a substring match over
    the whole command line — which would also have matched the operator's own
    `Xwayland :0` session, and `:3` matches inside `:30`.
    """
    from wingman.game_shutdown import close_nested_display
    result = close_nested_display(display)
    if not result["found"]:
        print(f"nested display {display}: not running")
    else:
        print(f"nested display {display}: "
              f"{'stopped' if result['ok'] else 'FAILED to stop cleanly'}")
    return 0 if result["ok"] else 1


def cmd_env(nc: dict) -> int:
    """Print the env prefix the game must be launched under, or nothing.

    Consumed by the Makefile. WAYLAND_DISPLAY is stripped so Wine cannot pick
    winewayland.drv and bypass the nested display; XDG_SESSION_TYPE is NOT set
    here, because wingman now selects its capture backend from config rather
    than by inferring it from the session type.
    """
    if not nc["enabled"]:
        return 0
    print(f'env -u WAYLAND_DISPLAY DISPLAY={nc["display"]}')
    return 0


def cmd_setup(nc: dict) -> int:
    """Start the server iff the lane is enabled. A no-op otherwise."""
    if not nc["enabled"]:
        return 0
    return start(nc["display"], nc["size"])


def main(argv) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command",
                    choices=["start", "focus", "status", "stop", "env", "setup"])
    ap.add_argument("--nested", default=None,
                    help="override config: 1 enables the lane, 0 disables it")
    ap.add_argument("--display", default=None)
    ap.add_argument("--size", default=None)
    ap.add_argument("--timeout", type=float, default=30.0,
                    help="focus: seconds to wait for the game window to appear")
    a = ap.parse_args(argv[1:])
    nc = load_nested_config(a.nested)
    # An explicit --display/--size still wins over config.
    display = a.display or nc["display"]
    size = a.size or nc["size"]

    if a.command == "env":
        return cmd_env(nc)
    if a.command == "setup":
        return cmd_setup(nc)
    if a.command == "start":
        return start(display, size)
    if a.command == "focus":
        # A no-op when the lane is off, so it is safe as an unconditional
        # prerequisite of the on-screen run targets. Keyed on the RESOLVED
        # state, so `--nested 0` is a no-op too rather than an error.
        if not nc["enabled"]:
            return 0
        return focus(display, a.timeout)
    if a.command == "status":
        return status(display)
    return stop(display)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
