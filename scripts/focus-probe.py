#!/usr/bin/env python3
"""Does X11 focus tracking work on this Wayland session?

Wingman injects keystrokes into whatever window has focus and has no focus
guard — only a capture-staleness check, which never fires on alt-tab because
mss keeps grabbing the monitor fine. The obvious guard reads _NET_ACTIVE_WINDOW
over X11, but this session is Wayland with a rootless Xwayland, so that property
may describe only X clients and go stale the moment focus moves to a native
Wayland window. A guard that is confidently wrong exactly when focus leaves the
game is worse than no guard.

This probe answers the question before anything is built. Run it alongside a
session, alt-tab between the game and other windows, and read the log:

    make focus-probe SECONDS=120

Two independent signals are sampled each tick:

  ewmh   _NET_ACTIVE_WINDOW — what the window manager advertises as active
  xfocus XGetInputFocus     — what the X server itself believes has focus

If either tracks alt-tab faithfully, a guard is cheap. If both keep naming the
game after focus has left it, an X11 guard cannot be built on this session and
the answer lies with the compositor instead.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass

# Identity is decided by the window's OWNING PROCESS, never its title.
#
# Titles cannot do this job. Verified 2026-08-28: with the game shut down, a
# VS Code window titled "Metalstorm config GitHub... - wingman - Visual Studio
# Code" matched a "metalstorm" substring test and was classified as the game. A
# guard using that rule would have kept injecting keystrokes into the editor —
# precisely the failure it exists to prevent. Any window can claim any title: a
# browser tab, a terminal running `less wingman.log`, this very editor.
#
# _NET_WM_PID gives the owning process; matching it against the game's real PID
# (the same /proc scan ADR 094 uses to close the game) is exact. WM_CLASS is the
# fallback for a window that sets no PID: Wine sets it from the executable.
GAME_PROCESS_NAME = "Metalstorm.exe"
GAME_WM_CLASS_HINTS = ("metalstorm", "wine")

VERDICT_GAME = "game"
VERDICT_OTHER = "other"
VERDICT_NONE = "none"
VERDICT_UNKNOWN = "unknown"


def _ppid_of(pid: int) -> int | None:
    """Parent pid from /proc/<pid>/stat field 4. Never raises."""
    try:
        with open(f"/proc/{pid}/stat") as fh:
            # comm can contain spaces and parentheses; everything after the
            # final ')' is positional, so split there.
            return int(fh.read().rsplit(")", 1)[1].split()[1])
    except (OSError, ValueError, IndexError):
        return None


def find_game_pids(process_name: str = GAME_PROCESS_NAME) -> set[int]:
    """PIDs whose `comm` matches, by /proc scan. Never raises.

    Mirrors wingman.game_shutdown.find_game_pids — kept standalone so the probe
    has no import dependency on the package it is investigating. `comm` is
    truncated to 15 characters by the kernel, so match on the prefix.
    """
    needle = process_name[:15]
    pids: set[int] = set()
    try:
        entries = os.listdir("/proc")
    except OSError:
        return pids
    for entry in entries:
        if not entry.isdigit():
            continue
        try:
            with open(f"/proc/{entry}/comm") as fh:
                if fh.read().strip().startswith(needle):
                    pids.add(int(entry))
        except (OSError, ValueError):
            continue
    return pids


def game_session_pids(process_name: str = GAME_PROCESS_NAME) -> set[int]:
    """Every PID in the game's Wine session, not just the game binary.

    Matching Metalstorm.exe alone is too narrow, and wrong in the dangerous
    direction. Verified 2026-08-28: the window the WM manages is "Wine Desktop",
    owned by explorer.exe (pid 3241639) — a SIBLING of Metalstorm.exe (3241663)
    under the same Proton launcher, pv-adverb. A guard matching only the game
    binary sees the active window as "not the game" whenever the virtual desktop
    is focused, suppresses every keypress, and silently stops wingman working.

    So the session is defined as everything descended from the game's parent.
    That includes explorer.exe and any Wine helper, and excludes unrelated apps:
    VS Code shares only systemd with the game, far above that root.
    """
    game = find_game_pids(process_name)
    if not game:
        return set()
    roots = {pp for pp in (_ppid_of(g) for g in game) if pp and pp > 1}
    if not roots:
        return set(game)
    session = set(game)
    try:
        entries = [int(e) for e in os.listdir("/proc") if e.isdigit()]
    except OSError:
        return session
    for pid in entries:
        seen = 0
        cur: int | None = pid
        # Walk up to the session root. The depth cap stops a malformed or
        # racing /proc from looping forever.
        while cur and cur > 1 and seen < 12:
            if cur in roots:
                session.add(pid)
                break
            cur = _ppid_of(cur)
            seen += 1
    return session


def classify(title: str | None, pid: int | None = None,
             game_pids: set[int] | None = None,
             wm_class: str | None = None) -> str:
    """Decide whether a window belongs to the game.

    Free of X so the decision a guard would make is testable without a display.
    The title distinguishes "some window" from "no window" only — it never
    identifies the game.
    """
    if title is None or not title.strip():
        return VERDICT_NONE
    if pid is not None and game_pids:
        return VERDICT_GAME if pid in game_pids else VERDICT_OTHER
    if wm_class:
        low = wm_class.lower()
        return VERDICT_GAME if any(h in low for h in GAME_WM_CLASS_HINTS) else VERDICT_OTHER
    # No PID and no class: refuse to guess. Calling an unidentifiable window the
    # game is the dangerous direction, so it is "other".
    return VERDICT_OTHER


@dataclass(frozen=True)
class FocusReading:
    """One tick's answer from both signals, already classified."""

    ewmh_title: str | None = None
    xfocus_title: str | None = None
    ewmh_verdict: str = VERDICT_NONE
    xfocus_verdict: str = VERDICT_NONE
    ewmh_error: str | None = None
    xfocus_error: str | None = None


def agreement(reading: FocusReading) -> str:
    """Do the two signals agree? This is the probe's whole point.

    'agree' means a guard could use either. 'disagree' means the choice of
    signal changes the answer, and the log shows which one tracked reality.
    """
    if reading.ewmh_error or reading.xfocus_error:
        return VERDICT_UNKNOWN
    return "agree" if reading.ewmh_verdict == reading.xfocus_verdict else "disagree"


class _XProbe:
    """Both focus signals, read through libX11 command-line clients.

    python-xlib is deliberately NOT used. Xwayland writes its auth cookie with
    an empty display-number field, which libX11 treats as a wildcard and
    python-xlib does not: get_best_auth() raises XNoAuthError and the connection
    is refused (verified 2026-08-28 on this host). xprop and xdpyinfo
    authenticate normally.

    Never raises out of sample().
    """

    def __init__(self, display_name: str):
        self._env = dict(os.environ, DISPLAY=display_name)
        if self._run(["xprop", "-root", "_NET_SUPPORTED"]) is None:
            raise RuntimeError(f"cannot query {display_name} with xprop")

    def _run(self, argv) -> str | None:
        try:
            out = subprocess.run(argv, env=self._env, capture_output=True,
                                 text=True, timeout=5.0)
            return out.stdout if out.returncode == 0 else None
        except Exception:                            # noqa: BLE001 - probe must not die
            return None

    def _prop(self, wid: str, name: str) -> str | None:
        out = self._run(["xprop", "-id", wid, name])
        if not out or "=" not in out:
            return None
        return out.split("=", 1)[1].strip() or None

    def _describe(self, wid: str, game_pids: set[int]) -> tuple[str | None, str]:
        raw = self._prop(wid, "_NET_WM_NAME") or self._prop(wid, "WM_NAME")
        title = raw[1:-1] if raw and raw.startswith('"') and raw.endswith('"') else raw
        pid_raw = self._prop(wid, "_NET_WM_PID")
        pid = int(pid_raw) if pid_raw and pid_raw.isdigit() else None
        cls = self._prop(wid, "WM_CLASS")
        return title, classify(title, pid, game_pids, cls)

    def sample(self) -> FocusReading:
        game_pids = game_session_pids()       # refreshed: the game may start or stop mid-run
        e_title = x_title = None
        e_verdict = x_verdict = VERDICT_NONE
        e_err = x_err = None

        out = self._run(["xprop", "-root", "_NET_ACTIVE_WINDOW"])
        if out is None:
            e_err = "xprop failed"
        else:
            m = re.search(r"(0x[0-9a-fA-F]+)", out)
            if m and m.group(1) != "0x0":
                e_title, e_verdict = self._describe(m.group(1), game_pids)

        out = self._run(["xdpyinfo"])
        if out is None:
            x_err = "xdpyinfo failed"
        else:
            m = re.search(r"focus:\s+window (0x[0-9a-fA-F]+)", out)
            # "window 0x0" / PointerRoot means no X client holds focus — exactly
            # the state a guard must not mistake for the game.
            if m and m.group(1) != "0x0":
                x_title, x_verdict = self._describe(m.group(1), game_pids)
        return FocusReading(e_title, x_title, e_verdict, x_verdict, e_err, x_err)


def format_line(ts: float, r: FocusReading) -> str:
    stamp = time.strftime("%H:%M:%S", time.localtime(ts))
    return (f"{stamp} ewmh={r.ewmh_verdict:<7} xfocus={r.xfocus_verdict:<7} "
            f"{agreement(r):<8} ewmh_title={r.ewmh_title!r} xfocus_title={r.xfocus_title!r}"
            + (f" ewmh_error={r.ewmh_error}" if r.ewmh_error else "")
            + (f" xfocus_error={r.xfocus_error}" if r.xfocus_error else ""))


def summarize(readings: list[FocusReading]) -> str:
    if not readings:
        return "no samples"
    n = len(readings)
    agree = sum(1 for r in readings if agreement(r) == "agree")
    ewmh_game = sum(1 for r in readings if r.ewmh_verdict == VERDICT_GAME)
    xf_game = sum(1 for r in readings if r.xfocus_verdict == VERDICT_GAME)
    xf_none = sum(1 for r in readings if r.xfocus_verdict == VERDICT_NONE)
    # "Was the game ever focused?" must not hinge on one signal being right.
    # Run of 2026-08-28 10:09: ewmh named explorer.exe's "Wine Desktop" in all
    # 291 samples while xfocus named "Metalstorm" in 290 — and the summary,
    # reading ewmh alone, declared the game had never been active.
    seen_game = max(ewmh_game, xf_game)
    seen_other = sum(1 for r in readings
                     if VERDICT_OTHER in (r.ewmh_verdict, r.xfocus_verdict))
    out = [
        f"samples: {n}",
        f"  signals agree:        {agree}/{n} ({100*agree/n:.0f} pct)",
        f"  ewmh saw the game:    {ewmh_game}",
        f"  xfocus saw the game:  {xf_game}",
        f"  either saw another:   {seen_other}",
        f"  xfocus saw nothing:   {xf_none}",
    ]
    if ewmh_game == 0 and xf_game > 0:
        out.append("  NOTE: xfocus tracked the game but ewmh never did. _NET_ACTIVE_WINDOW")
        out.append("        names the Wine Desktop container (explorer.exe), so a guard")
        out.append("        must use xfocus or match the whole Wine session.")
    if seen_game == 0:
        out.append("  VERDICT: the game was never the active window during this run.")
        out.append("           Nothing is proved: with no game window there is no")
        out.append("           focus transition to detect. Start the game, then re-run.")
    elif seen_other == 0:
        out.append("  VERDICT: focus never left the game, or ewmh cannot see that it did.")
        out.append("           Alt-tab away during the run, or the probe proves nothing.")
    else:
        out.append("  VERDICT: ewmh DID observe focus leaving the game — a guard can read it.")
    return "\n".join(out)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--seconds", type=float, default=120.0)
    ap.add_argument("--interval", type=float, default=1.0)
    ap.add_argument("--out", default="focus-probe.log")
    ap.add_argument("--display", default=os.environ.get("DISPLAY", ":0"))
    ap.add_argument("--wait-for-game", type=float, default=0.0, metavar="SECONDS",
                    help="wait up to this long for the game process to appear "
                         "before sampling, so the probe can be armed at launch")
    a = ap.parse_args(argv)

    try:
        probe = _XProbe(a.display)
    except Exception as e:                           # noqa: BLE001
        print(f"cannot open display {a.display!r}: {e}", file=sys.stderr)
        print("Run this from the desktop session, not over a bare shell.", file=sys.stderr)
        return 2

    if a.wait_for_game > 0 and not find_game_pids():
        print(f"waiting up to {a.wait_for_game:.0f}s for {GAME_PROCESS_NAME} to appear...")
        give_up = time.time() + a.wait_for_game
        try:
            while time.time() < give_up and not find_game_pids():
                time.sleep(1.0)
        except KeyboardInterrupt:
            print("cancelled while waiting")
            return 0
    if not find_game_pids():
        print(f"  note: {GAME_PROCESS_NAME} is not running. With no game window there is")
        print("        no focus transition to observe — start the game first.")

    readings: list[FocusReading] = []
    deadline = time.time() + a.seconds
    print(f"probing {a.display} for {a.seconds:.0f}s — alt-tab between the game and other windows")
    interrupted = False
    with open(a.out, "w", buffering=1) as fh:
        try:
            while time.time() < deadline:
                now = time.time()
                r = probe.sample()
                readings.append(r)
                fh.write(format_line(now, r) + "\n")
                time.sleep(max(0.05, a.interval))
        except KeyboardInterrupt:
            # Stopping early is a normal way to end a probe. Keep what was
            # collected and report on it rather than dying with a traceback.
            interrupted = True
        summary = summarize(readings)
        if interrupted:
            summary = "stopped early by operator\n" + summary
        fh.write("\n" + summary + "\n")
    print(("\n" if interrupted else "") + summary)
    print(f"\nwrote {a.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
