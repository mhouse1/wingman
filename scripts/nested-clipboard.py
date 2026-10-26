"""Put text on the game display's clipboard, so Ctrl-V pastes it in the game.

ADR 099 runs the game on its own X server (`:3`), which has its own clipboard:
text copied on the desktop never reaches it, so Ctrl-V in the game pastes
nothing. This owns CLIPBOARD and PRIMARY on $DISPLAY and serves one UTF-8
string until the time runs out, another client copies, or the display closes.
Wine's clipboard bridge asks for TARGETS, then UTF8_STRING, and hands the text
to the game as Unicode, so characters the keyboard cannot type (the
`🄼🄾🄱🄸🅄🅂` call signs, Job Aid 013) paste intact.

`make clip TEXT=...` runs it with the nested display's DISPLAY.
"""

import argparse
import sys
import time

from Xlib import X, Xatom, display
from Xlib.error import ConnectionClosedError, DisplayConnectionError
from Xlib.protocol import event


def serve(text: str, seconds: float) -> int:
    d = display.Display()
    win = d.screen().root.create_window(0, 0, 1, 1, 0, X.CopyFromParent)
    clipboard = d.intern_atom("CLIPBOARD")
    targets = d.intern_atom("TARGETS")
    text_targets = (d.intern_atom("UTF8_STRING"), d.intern_atom("text/plain;charset=utf-8"))
    data = text.encode("utf-8")

    for selection in (clipboard, Xatom.PRIMARY):
        win.set_selection_owner(selection, X.CurrentTime)
    d.flush()
    if d.get_selection_owner(clipboard) != win:
        print(f"could not take the clipboard on {d.get_display_name()}", file=sys.stderr)
        return 1
    print(f"clipboard on {d.get_display_name()} holds {text!r} for {seconds:.0f} s "
          "- press Ctrl-V in the game", flush=True)

    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        while d.pending_events():
            e = d.next_event()
            if e.type == X.SelectionClear and e.atom == clipboard:
                print("another client copied; clipboard released", flush=True)
                return 0
            if e.type != X.SelectionRequest:
                continue
            prop = e.property or e.target
            if e.target == targets:
                e.requestor.change_property(prop, Xatom.ATOM, 32, [targets, *text_targets])
            elif e.target in text_targets:
                e.requestor.change_property(prop, e.target, 8, data)
                print("pasted", flush=True)
            else:
                prop = X.NONE
            e.requestor.send_event(event.SelectionNotify(
                time=e.time, requestor=e.requestor, selection=e.selection,
                target=e.target, property=prop), event_mask=0)
            d.flush()
        time.sleep(0.05)
    print("time up; clipboard released", flush=True)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("text")
    parser.add_argument("--seconds", type=float, default=900.0)
    args = parser.parse_args()
    try:
        return serve(args.text, args.seconds)
    except DisplayConnectionError as exc:
        print(f"no display to hold the clipboard ({exc}); start the game first", file=sys.stderr)
        return 1
    except ConnectionClosedError:
        print("display closed; clipboard released", flush=True)
        return 0


if __name__ == "__main__":
    sys.exit(main())
