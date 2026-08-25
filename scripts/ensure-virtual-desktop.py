#!/usr/bin/env python3
"""Ensure a Wine prefix runs MetalStorm in a draggable virtual desktop.

Research 005. Without these keys the game launches true-fullscreen, which
breaks wingman: the capture region, `game_window_offset`, and every calibrated
crop assume the windowed geometry, and `undecorate-game-window` has no window
to act on.

`cp -a` of an existing prefix does NOT reliably carry them. Proton runs a
prefix update (wineboot) on first launch of a copied prefix, which resets
Wine-owned registry keys while leaving application keys intact — observed
2026-08-21, where the copied prefix kept its Unity `Screenmanager` prefs and
lost `Software\\Wine\\Explorer` entirely.

Idempotent: does nothing when the keys are already present.

Usage:
    ensure-virtual-desktop.py <wine-prefix> [WIDTHxHEIGHT]

Refuses to edit a prefix that is in use — wineserver rewrites user.reg on
shutdown and would discard the change.
"""

import re
import subprocess
import sys
import time
from pathlib import Path

_EXPLORER = r"[Software\\Wine\\Explorer]"
_DESKTOPS = r"[Software\\Wine\\Explorer\\Desktops]"


def prefix_in_use(prefix: Path) -> bool:
    """True when a wineserver or the game is live for this prefix."""
    try:
        out = subprocess.run(["pgrep", "-af", "wineserver|Metalstorm.exe"],
                             capture_output=True, text=True, timeout=10).stdout
    except Exception:
        return False   # cannot tell; the backup below is the safety net
    return str(prefix) in out


def main(argv) -> int:
    if not 2 <= len(argv) <= 3:
        print(__doc__)
        return 2
    prefix = Path(argv[1]).expanduser()
    size = argv[2] if len(argv) == 3 else "1920x1200"
    if not re.fullmatch(r"\d{3,5}x\d{3,5}", size):
        print(f"ERROR: size must look like 1920x1200, got {size!r}")
        return 2

    reg = prefix / "user.reg"
    if not reg.is_file():
        print(f"ERROR: no user.reg under {prefix} — is that a Wine prefix?")
        return 1
    if prefix_in_use(prefix):
        print(f"ERROR: {prefix} is in use — quit the game first "
              "(wineserver rewrites user.reg on exit and would discard this)")
        return 1

    text = reg.read_text(encoding="utf-8", errors="replace")
    if _EXPLORER in text and _DESKTOPS in text:
        print(f"OK: virtual desktop already configured in {prefix.name}")
        return 0

    backup = reg.with_suffix(f".reg.bak-{time.strftime('%Y%m%d_%H%M%S')}")
    backup.write_text(text, encoding="utf-8")

    ts = int(time.time())
    ft = (ts + 11644473600) * 10 ** 7      # unix epoch -> Windows FILETIME
    add = ""
    if _EXPLORER not in text:
        add += f'\n{_EXPLORER} {ts}\n#time={ft:x}\n"Desktop"="Default"\n'
    if _DESKTOPS not in text:
        add += f'\n{_DESKTOPS} {ts}\n#time={ft:x}\n"Default"="{size}"\n'
    if not text.endswith("\n"):
        text += "\n"
    reg.write_text(text + add, encoding="utf-8")
    print(f"OK: virtual desktop {size} enabled for {prefix.name} "
          f"(backup: {backup.name})")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
