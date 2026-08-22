#!/usr/bin/env python3
"""Copy MetalStorm settings and keybindings between Wine prefixes, without identity.

ADR 052 Open Question 3, decided: **filter, do not copy the whole key.**

`[Software\\Starform\\Metalstorm]` mixes two unrelated things in one Unity
PlayerPrefs key — the settings you want to share across accounts, and the
identity that MUST NOT be shared:

    client-settings--default_auth_token             <- login
    client-settings--default_selectedAccountId      <- which account
    client-settings--default_generatedDeviceIdentifier
    chat-text:direct-chat-*                         <- private messages

Copying the section wholesale would overwrite the target account's login with
the source account's, silently merging two accounts into one. So this works
from an **allowlist** of setting names, not a denylist: a new identity key added
by a future game version is excluded by default rather than leaked by default.

Handles the Wine `user.reg` value forms — `dword:`, `hex:`, `hex(4):` — including
multi-line values continued with a trailing backslash, which is how the
`inputBindingOverrides1` keybinding blob is stored.

Usage:
    sync-metalstorm-settings.py SOURCE_PREFIX TARGET_PREFIX [--dry-run]
"""

import re
import subprocess
import sys
import time
from pathlib import Path

_SECTION = r"[Software\\Starform\\Metalstorm]"
_HASH_SUFFIX = re.compile(r"_h\d+$")

# Allowlist of PlayerPrefs base names (the trailing _h<hash> is stripped first).
_ALLOW_PREFIXES = (
    "air-combat-settings--default_",   # volumes, HUD colours, control scheme,
                                       # and the inputBindingOverride* blobs
    "air-combat-hangar-default_",      # hangar sort preferences
    "Screenmanager ",                  # resolution, window position, fullscreen
)
_ALLOW_EXACT = frozenset({
    "client-settings--default_audioVolume-v2",
    "client-settings--default_fullScreenMode",
    "client-settings--default_selectedRegion",
    "UnityGraphicsQuality",
    "UnitySelectMonitor",
})

# Excluded even though they match an allowed prefix.
_DENY_EXACT = frozenset({
    # A "clear credentials on next start" flag — copying it can log the target
    # account out, which is the opposite of what this script is for.
    "air-combat-settings--default_clear-creds",
})

# Belt-and-braces: if any of these ever matched the allowlist it is a bug, so
# assert rather than rely on the allowlist alone.
_IDENTITY_MARKERS = ("auth_token", "selectedAccountId", "generatedDeviceIdentifier",
                     "find-accounts-for-did", "skipAccountFlow",
                     "outbound_email_pin_request", "savedClientErrorReportAddress",
                     "chat-text:", "cloud_userid", "player_session",
                     "unity_connect", "clear-creds")


def base_name(key: str) -> str:
    return _HASH_SUFFIX.sub("", key)


def is_allowed(key: str) -> bool:
    b = base_name(key)
    if b in _DENY_EXACT:
        return False
    if b in _ALLOW_EXACT:
        return True
    return any(b.startswith(p) for p in _ALLOW_PREFIXES)


def parse_section(text: str):
    """Return (entries, section_start, section_end).

    `entries` maps quoted key -> full raw text of the entry, continuation lines
    included. Order is preserved.
    """
    lines = text.splitlines(keepends=True)
    start = None
    for i, line in enumerate(lines):
        if line.startswith(_SECTION):
            start = i
            break
    if start is None:
        return {}, None, None
    end = len(lines)
    for i in range(start + 1, len(lines)):
        if lines[i].startswith("["):
            end = i
            break

    entries, i = {}, start + 1
    while i < end:
        line = lines[i]
        if line.startswith('"'):
            chunk = [line]
            while chunk[-1].rstrip("\n").endswith("\\") and i + 1 < end:
                i += 1
                chunk.append(lines[i])
            raw = "".join(chunk)
            key = raw[1:raw.index('"', 1)]
            entries[key] = raw
        i += 1
    return entries, start, end


def prefix_in_use(prefix: Path) -> bool:
    try:
        out = subprocess.run(["pgrep", "-af", "wineserver|Metalstorm.exe"],
                             capture_output=True, text=True, timeout=10).stdout
    except Exception:
        return False
    return str(prefix) in out


def main(argv) -> int:
    args = [a for a in argv[1:] if not a.startswith("--")]
    dry = "--dry-run" in argv
    if len(args) != 2:
        print(__doc__)
        return 2

    src_reg = Path(args[0]).expanduser() / "user.reg"
    dst_prefix = Path(args[1]).expanduser()
    dst_reg = dst_prefix / "user.reg"
    for p in (src_reg, dst_reg):
        if not p.is_file():
            print(f"ERROR: {p} not found — is that a Wine prefix?")
            return 1
    if src_reg.resolve() == dst_reg.resolve():
        print("ERROR: source and target are the same prefix")
        return 1
    if not dry and prefix_in_use(dst_prefix):
        print(f"ERROR: {dst_prefix} is in use — quit the game first "
              "(wineserver rewrites user.reg on exit and would discard this)")
        return 1

    src_text = src_reg.read_text(encoding="utf-8", errors="replace")
    dst_text = dst_reg.read_text(encoding="utf-8", errors="replace")
    src_entries, s_start, _ = parse_section(src_text)
    dst_entries, d_start, d_end = parse_section(dst_text)
    if s_start is None:
        print("ERROR: source prefix has no Starform/Metalstorm key")
        return 1
    if d_start is None:
        print("ERROR: target prefix has no Starform/Metalstorm key — "
              "launch the game once and log in first")
        return 1

    copy = {k: v for k, v in src_entries.items() if is_allowed(k)}
    for k in copy:
        assert not any(m in k for m in _IDENTITY_MARKERS), \
            f"BUG: identity key {k!r} passed the allowlist"

    skipped = sorted(base_name(k) for k in src_entries if k not in copy)
    changed = [k for k, v in copy.items() if dst_entries.get(k) != v]

    print(f"source : {src_reg.parent.name}")
    print(f"target : {dst_reg.parent.name}")
    print(f"copying: {len(copy)} setting keys ({len(changed)} differ)")
    print(f"holding back {len(skipped)} identity/diagnostic keys:")
    for k in skipped:
        print(f"    - {k}")
    if not changed:
        print("\nOK: target already matches — nothing to do")
        return 0
    if dry:
        print(f"\n[dry-run] would update {len(changed)} keys")
        return 0

    backup = dst_reg.with_suffix(f".reg.bak-{time.strftime('%Y%m%d_%H%M%S')}")
    backup.write_text(dst_text, encoding="utf-8")

    lines = dst_text.splitlines(keepends=True)
    head, section, tail = lines[:d_start + 1], lines[d_start + 1:d_end], lines[d_end:]
    kept = []
    i = 0
    while i < len(section):
        line = section[i]
        if line.startswith('"'):
            chunk = [line]
            while chunk[-1].rstrip("\n").endswith("\\") and i + 1 < len(section):
                i += 1
                chunk.append(section[i])
            raw = "".join(chunk)
            key = raw[1:raw.index('"', 1)]
            kept.append(copy.get(key, raw))
        else:
            kept.append(line)
        i += 1
    existing = {k for k in dst_entries}
    added = [v for k, v in copy.items() if k not in existing]

    body = "".join(head) + "".join(kept)
    if added:
        if not body.endswith("\n"):
            body += "\n"
        body += "".join(added)
    dst_reg.write_text(body + "".join(tail), encoding="utf-8")
    print(f"\nOK: updated {len(changed)} keys ({len(added)} new). "
          f"Backup: {backup.name}")
    print("Restart the game for the bindings to take effect.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
