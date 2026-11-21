#!/usr/bin/env python3
"""Version-controlled MetalStorm keybindings: export from one PC, deploy on another.

    make export-keybindings   # on VEDA: write the game's bindings into the repo
    make deploy-keybindings   # on any PC: load the repo's bindings into the game

The tracked file is `scripts/metalstorm-keybindings.reg`. It holds the control
entries only (`is_binding`): the binding and override blobs, control scheme,
turn mode and input system for keyboard and mouse, joystick and gamepad. Volumes,
HUD colours, resolution and window position are per-machine and stay out, and
the identity keys (login, account, device id, chat) never pass the allowlist of
`sync-metalstorm-settings.py`, which this script applies first.

The file is a Windows registry file kept as UTF-8 so git diffs stay readable;
each entry is the raw text Wine writes in `user.reg`, which is also the value
syntax regedit reads.

Deploy, by platform:
  Linux   — merges the entries into the target prefix's `user.reg` (the game
            must be closed: wineserver rewrites the file when it exits), and
            keeps a timestamped backup of the old file beside it.
  Windows — writes a UTF-16 copy of the file and runs `reg import` on it.

Usage:
    metalstorm-keybindings.py export SOURCE_PREFIX [FILE]
    metalstorm-keybindings.py deploy TARGET_PREFIX [FILE] [--dry-run]
(TARGET_PREFIX is ignored on Windows.)
"""

import importlib.util
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

_HERE = Path(__file__).resolve().parent
DEFAULT_FILE = _HERE / "metalstorm-keybindings.reg"

_spec = importlib.util.spec_from_file_location(
    "sync_metalstorm_settings", _HERE / "sync-metalstorm-settings.py")
sync = importlib.util.module_from_spec(_spec)
sys.modules.setdefault("sync_metalstorm_settings", sync)
_spec.loader.exec_module(sync)

_REG_HEADER = "Windows Registry Editor Version 5.00"
_REG_KEY = r"[HKEY_CURRENT_USER\Software\Starform\Metalstorm]"
_COMMENT = ("; MetalStorm keybindings, exported from {src} on {when} by "
            "`make export-keybindings`.\n"
            "; Deploy with `make deploy-keybindings`. Do not hand-edit the values.\n")

_BINDING_PREFIX = "air-combat-settings--default_"
_BINDING_NAMES = (
    "liveBindings-",             # the binding blob, one per device
    "inputBindingOverride",      # remapped keys and their index
    "liveControlScheme-",
    "liveTurnMode-",
    "liveActiveDeviceType-",
    "liveWorkingSetMigrated-",   # without it the game re-migrates over the bindings
    "inputSystem-",
    "hasSelectedControlScheme",
    "useSimulatorControls-",
)


def is_binding(key: str) -> bool:
    """A control entry: allowed by the settings sync, and one of the binding names."""
    if not sync.is_allowed(key):
        return False
    base = sync.base_name(key)
    if not base.startswith(_BINDING_PREFIX):
        return False
    return base[len(_BINDING_PREFIX):].startswith(_BINDING_NAMES)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def bindings_from_user_reg(text: str) -> dict:
    entries, start, _ = sync.parse_section(text)
    if start is None:
        return {}
    found = {k: v for k, v in entries.items() if is_binding(k)}
    for k in found:
        assert not any(m in k for m in sync._IDENTITY_MARKERS), \
            f"BUG: identity key {k!r} passed the binding filter"
    return found


def render_file(entries: dict, src: str) -> str:
    body = "".join(v if v.endswith("\n") else v + "\n" for v in entries.values())
    return (f"{_REG_HEADER}\n\n"
            + _COMMENT.format(src=src, when=time.strftime("%Y-%m-%d"))
            + f"\n{_REG_KEY}\n{body}")


def parse_file(text: str) -> dict:
    """The entries of a file written by `render_file`, keyed like `parse_section`."""
    lines = text.replace("\r\n", "\n").splitlines(keepends=True)
    try:
        start = next(i for i, ln in enumerate(lines) if ln.strip() == _REG_KEY)
    except StopIteration:
        return {}
    entries, i = {}, start + 1
    while i < len(lines):
        line = lines[i]
        if line.startswith("["):
            break
        if line.startswith('"'):
            chunk = [line]
            while chunk[-1].rstrip("\n").endswith("\\") and i + 1 < len(lines):
                i += 1
                chunk.append(lines[i])
            raw = "".join(chunk)
            entries[raw[1:raw.index('"', 1)]] = raw
        i += 1
    return entries


def export(source_prefix: Path, out: Path) -> int:
    reg = source_prefix / "user.reg"
    if not reg.is_file():
        print(f"ERROR: {reg} not found — is that a Wine prefix?")
        return 1
    if not sync.wait_until_free(source_prefix, 0.0):
        print("WARNING: the game is running in that prefix. Bindings changed in this "
              "session are not on disk until it quits.")
    entries = bindings_from_user_reg(_read(reg))
    if not entries:
        print(f"ERROR: no keybindings in {reg} — launch the game once and log in first")
        return 1
    text = render_file(entries, source_prefix.name)
    old = parse_file(_read(out)) if out.is_file() else {}
    out.write_text(text, encoding="utf-8")
    changed = sorted(sync.base_name(k) for k, v in entries.items() if old.get(k) != v)
    print(f"exported {len(entries)} keybinding keys from {source_prefix.name} to {out}")
    for k in changed:
        print(f"    changed: {k}")
    if not changed:
        print("    no change since the last export")
    return 0


def _deploy_linux(entries: dict, target_prefix: Path, dry: bool) -> int:
    reg = target_prefix / "user.reg"
    if not reg.is_file():
        print(f"ERROR: {reg} not found — is that a Wine prefix?")
        return 1
    text = _read(reg)
    current, start, _ = sync.parse_section(text)
    if start is None:
        print(f"ERROR: {target_prefix.name} has no Starform/Metalstorm key — "
              "launch the game once and log in first")
        return 1
    changed = sorted(sync.base_name(k) for k, v in entries.items() if current.get(k) != v)
    print(f"target : {target_prefix}")
    print(f"deploy : {len(entries)} keybinding keys ({len(changed)} differ)")
    for k in changed:
        print(f"    - {k}")
    if not changed:
        print("OK: the game already has these bindings — nothing to do")
        return 0
    if dry:
        print(f"[dry-run] would update {len(changed)} keys")
        return 0
    if not sync.wait_until_free(target_prefix, 0.0):
        print(f"ERROR: {target_prefix.name} is in use — quit the game first "
              "(wineserver rewrites user.reg on exit and would discard this)")
        return 1
    backup = reg.with_suffix(f".reg.bak-{time.strftime('%Y%m%d_%H%M%S')}")
    backup.write_text(text, encoding="utf-8")
    merged, _ = sync.merge_entries(text, entries)
    reg.write_text(merged, encoding="utf-8")
    print(f"OK: updated {len(changed)} keys. Backup: {backup.name}")
    print("Account prefixes (g2, g3) pick them up at their next launch.")
    return 0


def windows_reg_bytes(entries: dict) -> bytes:
    """What `reg import` reads: UTF-16 LE with a BOM and CRLF line ends."""
    text = render_file(entries, "repo").replace("\r\n", "\n").replace("\n", "\r\n")
    return ("﻿" + text + "\r\n").encode("utf-16-le")


def _deploy_windows(entries: dict, dry: bool) -> int:
    print(f"deploy : {len(entries)} keybinding keys into HKCU\\Software\\Starform\\Metalstorm")
    if dry:
        print("[dry-run] would run reg import")
        return 0
    stamp = time.strftime("%Y%m%d_%H%M%S")
    backup = Path(tempfile.gettempdir()) / f"metalstorm-before-{stamp}.reg"
    subprocess.run(["reg", "export", r"HKCU\Software\Starform\Metalstorm",
                    str(backup), "/y"], check=False)
    with tempfile.NamedTemporaryFile(suffix=".reg", delete=False) as fh:
        fh.write(windows_reg_bytes(entries))
        path = fh.name
    try:
        rc = subprocess.run(["reg", "import", path], check=False).returncode
    finally:
        os.unlink(path)
    if rc != 0:
        print(f"ERROR: reg import failed (exit {rc})")
        return 1
    print(f"OK: imported. Backup of the old key: {backup}")
    return 0


def deploy(target_prefix: Path, src: Path, dry: bool) -> int:
    if not src.is_file():
        print(f"ERROR: {src} not found — run `make export-keybindings` on VEDA first")
        return 1
    entries = parse_file(_read(src))
    if not entries:
        print(f"ERROR: {src} holds no keybindings")
        return 1
    bad = [k for k in entries if not is_binding(k)]
    if bad:
        print(f"ERROR: {src} holds keys that are not keybindings: {bad}")
        return 1
    if sys.platform == "win32":
        return _deploy_windows(entries, dry)
    return _deploy_linux(entries, target_prefix, dry)


def main(argv) -> int:
    dry = "--dry-run" in argv
    args = [a for a in argv[1:] if not a.startswith("--")]
    if len(args) not in (2, 3) or args[0] not in ("export", "deploy"):
        print(__doc__)
        return 2
    prefix = Path(args[1]).expanduser()
    file = Path(args[2]).expanduser() if len(args) == 3 else DEFAULT_FILE
    if args[0] == "export":
        return export(prefix, file)
    return deploy(prefix, file, dry)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
