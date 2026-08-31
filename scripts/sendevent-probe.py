#!/usr/bin/env python3
"""Can wingman deliver keys to the game while another window has focus?

ADR 098 stops injection when the operator alt-tabs, because XTest `fake_input`
posts at the X server's *focus*, not at a window: with the editor focused the
game receives nothing and the editor receives everything — the "tryi auganw"
corruption of 2026-08-28. The guard removes the corruption but not the
underlying limitation. Wingman still cannot fly while the operator works in
another window, which is the thing the operator actually wants.

XSendEvent addresses a window instead of the focus, so it is the obvious
candidate. Whether it works *here* is not obvious at all, and three independent
things have to hold:

  1. MECHANISM  python-xlib must deliver a synthetic KeyPress to a window at
                all, over this session's Xwayland auth.
  2. WINE       Wine's X11 driver must act on an event carrying `send_event`
                rather than discarding it, as many toolkits do.
  3. ROUTING    Wine must route that event to the game even though its desktop
                window does not hold X input focus — the driver tracks focus
                itself, and may drop keys aimed at a window it believes is
                unfocused.

Any one failing sinks the approach, and each fails in a different way and at a
different cost to fix. This probe separates them before a line of it reaches the
injection path, the same way `scripts/focus-probe.py` settled the focus question
before ADR 098 was written.

Step 1 is answered automatically: the probe creates its own window and sends
itself a key. Steps 2 and 3 cannot be — nothing outside the game reports whether
the game acted — so the probe sends a *labelled* burst to each candidate window,
one at a time, and asks the operator which burst they saw land. It records what
was sent, to which window, and independently confirms via ADR 098's own focus
check that the game really was unfocused at that moment.

    make sendevent-probe          # start the game, then alt-tab when told

The default key is PADLOCK_CAMERA ('p'): an unmistakable view change that costs
nothing if it does fire.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from dataclasses import dataclass, field

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# The auth shim, the focus check and the key map are reused rather than copied.
# focus-probe.py deliberately duplicated its /proc scan because it was
# investigating the very code it would have imported; that does not apply here —
# this probe investigates Wine, and wingman's identification of the game is
# settled, tested and shared.
from wingman.focus_guard import (FOCUS_GAME, FOCUS_OTHER, GAME_PROCESS_NAME,  # noqa: E402
                                 FocusGuard, find_game_pids, game_session_pids)
from wingman.input_linux import _ensure_xauthority  # noqa: E402
from wingman.keybindings import PADLOCK_CAMERA  # noqa: E402

MECH_SEND_EVENT = "send-event"
MECH_FOCUS_SWAP = "focus-swap"


@dataclass
class Candidate:
    """One X window that might be the game's real keyboard target.

    Wine's virtual desktop is a single X window owned by `explorer.exe`; the
    game's own windows are Wine-internal and never appear in the X tree. Which
    X window the driver reads keys from is not documented anywhere we can rely
    on, so every session-owned window is a candidate and the operator's eyes
    decide.
    """

    wid: int
    pid: "int | None"
    title: "str | None"
    wm_class: "str | None"
    depth: int
    mapped: bool
    geometry: str = ""
    obj: object = field(default=None, repr=False)

    @property
    def label(self) -> str:
        return (f"0x{self.wid:08x} depth={self.depth} pid={self.pid} "
                f"mapped={'yes' if self.mapped else 'no '} {self.geometry} "
                f"class={self.wm_class!r} title={self.title!r}")


def _text(raw) -> "str | None":
    """Decode an Xlib property value that may be bytes, str, or a list."""
    if raw is None:
        return None
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", "replace")
    if isinstance(raw, (list, tuple)):
        raw = " ".join(_text(x) or "" for x in raw)
    return str(raw).replace("\x00", " ").strip() or None


class _Prober:
    """The X side. Never raises out of a public method."""

    def __init__(self, display_name: str):
        _ensure_xauthority()
        from Xlib import X as _X, display as _xdisplay
        self._X = _X
        self._d = _xdisplay.Display(display_name)
        self._root = self._d.screen().root
        self._display_name = display_name

    # --- window discovery ---------------------------------------------------

    def _prop(self, win, name: str):
        try:
            atom = self._d.get_atom(name, only_if_exists=True)
            if not atom:
                return None
            p = win.get_full_property(atom, 0)
            return p.value if p else None
        except Exception:                    # noqa: BLE001 - probe must not die
            return None

    def _describe(self, win, depth: int) -> "Candidate | None":
        try:
            wid = win.id
            pid_raw = self._prop(win, "_NET_WM_PID")
            pid = int(pid_raw[0]) if pid_raw else None
            title = _text(self._prop(win, "_NET_WM_NAME")) or _text(self._prop(win, "WM_NAME"))
            cls = _text(self._prop(win, "WM_CLASS"))
            attrs = win.get_attributes()
            geo = win.get_geometry()
            return Candidate(wid=wid, pid=pid, title=title, wm_class=cls, depth=depth,
                             mapped=(attrs.map_state == self._X.IsViewable),
                             geometry=f"{geo.width}x{geo.height}", obj=win)
        except Exception:                    # noqa: BLE001
            return None

    def candidates(self, session: "set[int]", max_depth: int = 6) -> "list[Candidate]":
        """Every window owned by a game-session process, shallowest first.

        `_NET_WM_PID` is set on the managed top-level and usually not on its
        children, so a child inherits its nearest ancestor's owner. Without that
        inheritance the probe finds exactly one candidate — the Wine Desktop
        frame — and never tests the window beneath it.
        """
        found: list[Candidate] = []
        seen: set[int] = set()

        def walk(win, depth: int, owner: "int | None") -> None:
            if depth > max_depth:
                return
            c = self._describe(win, depth)
            if c is None:
                return
            effective = c.pid if c.pid is not None else owner
            if effective in session and c.wid not in seen:
                seen.add(c.wid)
                c.pid = effective
                found.append(c)
            try:
                children = win.query_tree().children
            except Exception:                # noqa: BLE001
                return
            for child in children:
                walk(child, depth + 1, effective)

        try:
            for top in self._root.query_tree().children:
                walk(top, 1, None)
        except Exception:                    # noqa: BLE001
            pass
        # Shallowest first: the WM-managed Wine Desktop is the likeliest target,
        # and the operator should see it tested before deep leaf windows.
        found.sort(key=lambda c: (c.depth, not c.mapped, c.wid))
        return found

    # --- sending ------------------------------------------------------------

    def keycode(self, key: str) -> int:
        from Xlib import XK as _XK
        aliases = {"space": "space", "enter": "Return", "esc": "Escape"}
        sym = _XK.string_to_keysym(aliases.get(key.lower(), key.lower()))
        return self._d.keysym_to_keycode(sym) if sym else 0

    def send_event_key(self, win, keycode: int, propagate: bool = False) -> "str | None":
        """One synthetic press/release pair addressed to `win`.

        Returns None on success or a short error string. `state=0` and
        `same_screen=1` mirror a real key on an unmodified keyboard; `time` is a
        rolling millisecond stamp because Wine reads it for GetMessageTime and a
        constant would make every key look simultaneous.
        """
        from Xlib.protocol import event as _event
        X = self._X
        stamp = int(time.monotonic() * 1000) & 0xFFFFFFFF
        try:
            for cls, mask in ((_event.KeyPress, X.KeyPressMask),
                              (_event.KeyRelease, X.KeyReleaseMask)):
                ev = cls(time=stamp, root=self._root, window=win, same_screen=1,
                         child=X.NONE, root_x=0, root_y=0, event_x=0, event_y=0,
                         state=0, detail=keycode)
                win.send_event(ev, event_mask=mask, propagate=propagate)
                self._d.sync()
                stamp = (stamp + 30) & 0xFFFFFFFF
                time.sleep(0.03)
            return None
        except Exception as e:               # noqa: BLE001
            return f"{type(e).__name__}: {e}"

    def focus_swap_key(self, win, keycode: int) -> "str | None":
        """Move X focus to `win`, XTest the key, put focus back.

        The fallback if XSendEvent proves dead. It is *not* free: for the
        duration of the swap the operator's own keystrokes go to the game, so a
        press landing mid-word is a corruption of exactly the kind ADR 098
        exists to prevent — smaller, but the same failure. Measured here only so
        the choice between mechanisms rests on data.
        """
        from Xlib.ext import xtest as _xtest
        X = self._X
        # Bound before the try: the finally below restores focus, and it must
        # not turn a failure to READ the old focus into a NameError.
        prev_win, prev_revert = X.PointerRoot, X.RevertToPointerRoot
        try:
            prev = self._d.get_input_focus()
            prev_win, prev_revert = prev.focus, prev.revert_to
            win.set_input_focus(X.RevertToParent, X.CurrentTime)
            self._d.sync()
            _xtest.fake_input(self._d, X.KeyPress, keycode)
            self._d.sync()
            time.sleep(0.03)
            _xtest.fake_input(self._d, X.KeyRelease, keycode)
            self._d.sync()
            return None
        except Exception as e:               # noqa: BLE001
            return f"{type(e).__name__}: {e}"
        finally:
            # Restoring focus is not optional: leaving it on the game silently
            # converts the operator's next sentence into flight input.
            try:
                if isinstance(prev_win, int):
                    self._d.set_input_focus(prev_win, prev_revert, X.CurrentTime)
                else:
                    prev_win.set_input_focus(prev_revert, X.CurrentTime)
                self._d.sync()
            except Exception:                # noqa: BLE001
                pass

    # --- step 1: does the mechanism work at all? ----------------------------

    def self_test(self, keycode: int, timeout_s: float = 2.0) -> "tuple[bool, str]":
        """Send a synthetic key to a window this probe owns and read it back.

        This is the control. If it fails, nothing the game does or does not do
        means anything — the probe itself cannot deliver an event, and a
        negative result about Wine would be an artefact.
        """
        X = self._X
        try:
            win = self._root.create_window(0, 0, 1, 1, 0, self._d.screen().root_depth,
                                           event_mask=X.KeyPressMask | X.KeyReleaseMask)
            self._d.sync()
        except Exception as e:               # noqa: BLE001
            return False, f"could not create a test window: {type(e).__name__}: {e}"
        try:
            err = self.send_event_key(win, keycode)
            if err:
                return False, f"send_event raised: {err}"
            give_up = time.monotonic() + timeout_s
            while time.monotonic() < give_up:
                for _ in range(self._d.pending_events()):
                    ev = self._d.next_event()
                    if ev.type == X.KeyPress and getattr(ev, "detail", None) == keycode:
                        return True, "own window received the synthetic KeyPress"
                time.sleep(0.02)
            return False, "own window never received the event (delivery is broken)"
        finally:
            try:
                win.destroy()
                self._d.sync()
            except Exception:                # noqa: BLE001
                pass


@dataclass
class Burst:
    """One labelled attempt: what was sent, where, and what focus was at the time."""

    index: int
    candidate: Candidate
    mechanism: str
    count: int
    focus_state: str
    error: "str | None" = None
    seen: "bool | None" = None               # filled in by the operator

    def line(self) -> str:
        return (f"burst {self.index}: mech={self.mechanism} focus={self.focus_state} "
                f"count={self.count} seen={self.seen} "
                + (f"error={self.error} " if self.error else "")
                + self.candidate.label)


def summarize(bursts: "list[Burst]", self_test: "tuple[bool, str]") -> str:
    ok, detail = self_test
    out = [f"self-test (mechanism): {'PASS' if ok else 'FAIL'} - {detail}", ""]
    if not ok:
        out += [
            "  VERDICT: the probe could not deliver a synthetic key to its own window,",
            "           so it proves nothing about Wine. Fix delivery first — check",
            "           XAUTHORITY and that DISPLAY names the session's Xwayland.",
        ]
        return "\n".join(out)

    if not bursts:
        out.append("  VERDICT: nothing was sent. Start the game and re-run.")
        return "\n".join(out)

    out.append(f"bursts: {len(bursts)}")
    for b in bursts:
        out.append("  " + b.line())
    out.append("")

    unfocused = [b for b in bursts if b.focus_state == FOCUS_OTHER]
    landed = [b for b in unfocused if b.seen]
    errored = [b for b in bursts if b.error]
    if errored:
        out.append(f"  {len(errored)} burst(s) failed to send — see error= above.")
    if not unfocused:
        out += [
            "  VERDICT: the game held focus for every burst, so the question was never",
            "           asked. Alt-tab away when the probe tells you to, then re-run.",
        ]
    elif landed:
        wins = ", ".join(f"0x{b.candidate.wid:08x} via {b.mechanism}" for b in landed)
        out += [
            "  VERDICT: keys REACHED the game while another window had focus.",
            f"           Working target(s): {wins}",
            "           Wingman can fly unfocused: route injection to that window and",
            "           keep ADR 098's guard as the fallback for when it is unavailable.",
        ]
    else:
        out += [
            "  VERDICT: no burst reached the game while it was unfocused.",
            "           Delivery works (self-test passed), so Wine either discards",
            "           send_event keys or drops keys aimed at a window it does not",
            "           consider focused. XSendEvent is a dead end for this game;",
            "           the remaining option is a nested X server for the game.",
        ]
    return "\n".join(out)


def _countdown(seconds: float, message: str) -> None:
    end = time.monotonic() + seconds
    while True:
        left = end - time.monotonic()
        if left <= 0:
            break
        print(f"\r{message} {left:4.1f}s ", end="", flush=True)
        time.sleep(0.1)
    print("\r" + " " * 72 + "\r", end="", flush=True)


def _ask_seen(bursts: "list[Burst]") -> None:
    """Ask which bursts the operator actually saw land in the game.

    Only the operator can answer steps 2 and 3: nothing outside the game reports
    whether it acted on a key. Left unanswered (non-interactive, or Ctrl-C), the
    bursts stay `seen=None` and the summary reports no landing rather than
    inventing one.
    """
    if not sys.stdin.isatty():
        print("not a tty - skipping the 'which burst landed?' question")
        return
    print("\nWhich burst numbers produced a visible effect in the game?")
    print("Enter numbers separated by spaces, 'none', or press Enter for none.")
    try:
        raw = input("> ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return
    if not raw or raw == "none":
        for b in bursts:
            b.seen = False
        return
    wanted = {int(t) for t in raw.replace(",", " ").split() if t.isdigit()}
    for b in bursts:
        b.seen = b.index in wanted


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--key", default=PADLOCK_CAMERA,
                    help="key to send (default: the padlock camera toggle - "
                         "unmistakable on screen and harmless if it fires)")
    ap.add_argument("--count", type=int, default=3, help="presses per burst")
    ap.add_argument("--dwell", type=float, default=4.0,
                    help="seconds between bursts, for the operator to watch")
    ap.add_argument("--arm", type=float, default=8.0,
                    help="seconds to alt-tab away before the first burst")
    ap.add_argument("--mechanism", choices=[MECH_SEND_EVENT, MECH_FOCUS_SWAP, "both"],
                    default=MECH_SEND_EVENT,
                    help="focus-swap steals keyboard focus briefly and can eat the "
                         "operator's own keystrokes - opt in deliberately")
    ap.add_argument("--candidate", type=int, default=None, metavar="N",
                    help="test only candidate N from a previous run's listing")
    ap.add_argument("--out", default="sendevent-probe.log")
    ap.add_argument("--display", default=os.environ.get("DISPLAY", ":0"))
    ap.add_argument("--wait-for-game", type=float, default=0.0, metavar="SECONDS")
    a = ap.parse_args(argv)

    try:
        prober = _Prober(a.display)
    except Exception as e:                   # noqa: BLE001
        print(f"cannot open display {a.display!r}: {e}", file=sys.stderr)
        print("Run this from the desktop session, not over a bare shell.", file=sys.stderr)
        return 2

    keycode = prober.keycode(a.key)
    if not keycode:
        print(f"no keycode for {a.key!r} on this keyboard map", file=sys.stderr)
        return 2

    self_test = prober.self_test(keycode)
    print(f"self-test: {'PASS' if self_test[0] else 'FAIL'} - {self_test[1]}")

    if a.wait_for_game > 0 and not find_game_pids():
        print(f"waiting up to {a.wait_for_game:.0f}s for {GAME_PROCESS_NAME}...")
        give_up = time.time() + a.wait_for_game
        try:
            while time.time() < give_up and not find_game_pids():
                time.sleep(1.0)
        except KeyboardInterrupt:
            print("cancelled while waiting")
            return 0

    session = game_session_pids()
    if not session:
        print(f"{GAME_PROCESS_NAME} is not running — start the game first.")
        with open(a.out, "w") as fh:
            fh.write(summarize([], self_test) + "\n")
        return 1

    cands = prober.candidates(session)
    print(f"\n{len(cands)} window(s) owned by the game session (pids {sorted(session)[:6]}...):")
    for i, c in enumerate(cands, 1):
        print(f"  {i}. {c.label}")
    if not cands:
        print("  none — the session owns no X window, so there is nothing to address.")
        with open(a.out, "w") as fh:
            fh.write(summarize([], self_test) + "\n")
        return 1
    if a.candidate is not None:
        if not 1 <= a.candidate <= len(cands):
            print(f"--candidate {a.candidate} is out of range", file=sys.stderr)
            return 2
        cands = [cands[a.candidate - 1]]

    # ADR 098's own check, sampled fresh per burst, so the log proves the game
    # really was unfocused rather than assuming the operator alt-tabbed.
    guard = FocusGuard({"enabled": True, "ttl_s": 0.0, "display": a.display})

    mechanisms = ([MECH_SEND_EVENT, MECH_FOCUS_SWAP] if a.mechanism == "both"
                  else [a.mechanism])

    print(f"\nSending {a.key!r} x{a.count} to each window, {a.dwell:.0f}s apart.")
    print("ALT-TAB AWAY FROM THE GAME NOW, and watch the game while it runs.")
    _countdown(a.arm, "starting in")

    bursts: list[Burst] = []
    index = 0
    try:
        for mech in mechanisms:
            for c in cands:
                index += 1
                state = guard.focus_state()
                b = Burst(index=index, candidate=c, mechanism=mech,
                          count=a.count, focus_state=state)
                note = " (game is focused - this burst proves nothing)" if state == FOCUS_GAME else ""
                print(f"burst {index}: {mech} -> 0x{c.wid:08x} "
                      f"({c.title or c.wm_class or 'untitled'}){note}")
                for _ in range(a.count):
                    err = (prober.send_event_key(c.obj, keycode) if mech == MECH_SEND_EVENT
                           else prober.focus_swap_key(c.obj, keycode))
                    if err:
                        b.error = err
                        break
                    time.sleep(0.15)
                bursts.append(b)
                _countdown(a.dwell, "  watching")
    except KeyboardInterrupt:
        print("\nstopped early by operator")

    _ask_seen(bursts)
    summary = summarize(bursts, self_test)
    with open(a.out, "w", buffering=1) as fh:
        fh.write(f"key={a.key!r} keycode={keycode} display={a.display} "
                 f"mechanism={a.mechanism} count={a.count}\n\n")
        fh.write(summary + "\n")
    print("\n" + summary)
    print(f"\nwrote {a.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
