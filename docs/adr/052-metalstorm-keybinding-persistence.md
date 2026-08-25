# ADR 052 — MetalStorm Keybinding Persistence and Cross-Platform Sharing

| Status   | Date       | Wingman Version |
|----------|------------|-----------------|
| Accepted | 2026-08-21 | 1.8.5           |

## Context

MetalStorm's in-game keybindings must be configured manually after every fresh
install or Wine prefix recreation (see job aid `docs/job-aids/011-wingman-keybindings.md`
and ADR 051 for the Linux-specific joystick/controller requirement for pitch control).

Reconfiguring all bindings by hand is error-prone and slow. A backup/restore mechanism
is needed, and ideally the same saved config should be applicable on both Linux and
Windows without re-entry.

### Where Keybindings Are Stored

MetalStorm uses Unity's `PlayerPrefs` API to persist settings. The storage location
differs by platform:

| Platform | Storage location |
|---|---|
| Windows | `HKCU\Software\Starform\Metalstorm` in the Windows registry |
| Linux (Wine/Proton) | `[Software\Starform\Metalstorm]` section of the Wine prefix `user.reg` |

The relevant registry values are:

- `inputBindingOverrideIndex` — integer selecting the active binding slot
- `inputBindingOverrides1` — JSON blob containing all custom key assignments
- `hasSelectedControlScheme` — whether the controller/joystick mode was chosen
- Various `air-combat-settings--default_*` values covering volume, HUD colour, etc.

Game updates (which replace files in the game install directory) do **not** touch the
Wine prefix or the Windows registry, so keybindings survive routine game patches. The
risk is limited to prefix recreation on Linux or a fresh Windows install.

### Current State

A backup script exists at `scripts/backup-metalstorm-config.sh` that copies the
entire Wine `user.reg` file. This covers Linux only and is not human-readable or
directly importable on Windows.

---

## Problem — Cross-Platform Sharing

The Wine `user.reg` format is Wine-specific (REGEDIT4-derived but not identical to
the standard Windows `.reg` export format). A raw `user.reg` copy cannot be double-clicked
on Windows to import keybindings.

However, the standard **Windows `.reg` file format** is understood by both:

- **Windows**: `reg import keybindings.reg` or double-click
- **Linux/Wine**: `wine regedit keybindings.reg` (Wine's regedit accepts standard
  Windows `.reg` files)

This means a single committed `.reg` file can serve both platforms.

---

## Proposal

### 1. Export keybindings as a standard Windows `.reg` file

**On Linux**, use Wine's regedit via the Proton binary inside the Heroic Flatpak to
export just the Metalstorm registry key:

```bash
# Export (Linux — run once after configuring keybindings in-game)
WINEPREFIX=/home/michael/Games/Heroic/Prefixes/Metalstorm \
  wine regedit /E scripts/metalstorm-keybindings.reg \
  "HKEY_CURRENT_USER\Software\Starform\Metalstorm"
```

**On Windows**, use the built-in reg tool:

```bat
reg export "HKCU\Software\Starform\Metalstorm" scripts\metalstorm-keybindings.reg /y
```

### 2. Import on any platform

**On Linux (Wine/Proton)**:

```bash
WINEPREFIX=/home/michael/Games/Heroic/Prefixes/Metalstorm \
  wine regedit scripts/metalstorm-keybindings.reg
```

**On Windows**:

```bat
reg import scripts\metalstorm-keybindings.reg
```

Or double-click the `.reg` file in Explorer.

### 3. Commit the `.reg` file to the repository

The exported `scripts/metalstorm-keybindings.reg` is a text file (hex-encoded values
but human-readable structure). Committing it means:

- Any machine can restore the canonical keybinding set from `git pull`
- Changes to keybindings are tracked in git history
- Windows and Linux users share a single source of truth

### 4. Update `backup-metalstorm-config.sh`

Extend the script to support the cross-platform `.reg` export/import path alongside the
existing raw `user.reg` copy:

```bash
# Proposed additional commands:
./scripts/backup-metalstorm-config.sh export   # export to .reg (Linux, requires Wine)
./scripts/backup-metalstorm-config.sh import   # import from .reg (Linux, requires Wine)
```

The raw `backup`/`restore` commands (full `user.reg` copy) remain as a fast local
fallback that does not require Wine to be invokable from the terminal.

---

## Open Questions

1. **Heroic Flatpak Wine path**: invoking `wine regedit` from a terminal requires
   pointing at the correct Wine binary inside the Flatpak sandbox or using
   `flatpak run --command=wine com.heroicgameslauncher.hgl regedit`. The exact
   invocation needs to be tested and documented in the script.

2. **Proton vs Wine binary**: Heroic uses UMU/Proton, not plain Wine. The `regedit`
   binary lives inside the Proton build
   (`~/.var/app/com.heroicgameslauncher.hgl/config/heroic/tools/proton/GE-Proton-latest/files/bin/wine`).
   The script should resolve this path dynamically rather than hardcoding a version.

3. ~~**Settings beyond keybindings**~~ **RESOLVED 2026-08-21 — filter, and by
   allowlist.** Multi-account runs (Research 005) forced the decision: the key
   mixes settings with identity, so copying it whole merges two accounts.

   Of 72 values in `[Software\Starform\Metalstorm]`, 38 are settings and 34
   are identity or diagnostics — including `auth_token`, `selectedAccountId`,
   `generatedDeviceIdentifier`, and 13 `chat-text:*` conversation histories.
   `client-settings--default_*` is a **mixed** family (`audioVolume`,
   `fullScreenMode`, `selectedRegion` sit beside `auth_token`), so filtering by
   name prefix is not sufficient — it needs a per-key allowlist.

   Implemented as `scripts/sync-metalstorm-settings.py`
   (`make sync-settings-1`). It works from an **allowlist**, not a denylist, so
   an identity key added by a future game version is excluded by default rather
   than leaked by default, and asserts on identity markers as a second check.

   `air-combat-settings--default_clear-creds` is excluded despite matching an
   allowed prefix: it is a "clear credentials on next start" flag, and copying
   it can log the target account out.

---

## Status 2026-08-21 — the sharing problem is solved for Linux prefix-to-prefix

`scripts/sync-metalstorm-settings.py` copies settings and keybindings between
Wine prefixes directly, parsing `user.reg` (`dword:`, `hex:`, `hex(4):`,
including the multi-line backslash continuations the `inputBindingOverrides1`
blob uses). Verified: after the sync, `inputBindingOverrides1` is byte-identical
between prefixes while `auth_token`, `selectedAccountId` and
`generatedDeviceIdentifier` all remain distinct.

It refuses to write a prefix that is in use, because wineserver rewrites
`user.reg` on shutdown and would silently discard the change; it backs up first
and is idempotent.

**This does not close the ADR.** Its cross-platform goal is untouched: there is
still no committed `.reg` file, and Open Questions 1 and 2 (finding the Proton
`wine` binary to run `regedit`) remain open. The script sidesteps them by
editing `user.reg` directly, which works Linux-to-Linux only. Windows sharing
still needs the `.reg` export path below.

## Resolution 2026-08-21 — solved by a different mechanism than proposed

Accepted. The problem this ADR exists for — *reconfiguring keybindings by hand
after every prefix recreation* — is solved. It was solved by direct
prefix-to-prefix `user.reg` synchronisation rather than by the `.reg`
export/import route proposed above, so the original acceptance criteria are
recorded here as **descoped, not met**, to keep the decision history honest.

### Original criteria — status

| Criterion | Status |
|-----------|--------|
| `backup-metalstorm-config.sh export` produces a valid `.reg` | **Descoped** — not implemented |
| `backup-metalstorm-config.sh import` applies it | **Descoped** — not implemented |
| `scripts/metalstorm-keybindings.reg` committed | **Descoped** — no such file |
| Script resolves the Proton Wine binary without hardcoding | **Descoped** — no longer needed |

Open Questions 1 and 2 (locating `regedit` inside the Heroic Flatpak / Proton
build) are closed as **moot**: editing `user.reg` directly needs no Wine binary,
which is why the route was taken.

### What was built instead

`scripts/sync-metalstorm-settings.py` (`make sync-settings-1` / `-2`) copies the
38 settings-and-keybinding values between prefixes while holding back the 34
identity and diagnostic values — see Open Question 3 above, which this ADR now
answers. Verified 2026-08-21: `inputBindingOverrides1` byte-identical across
prefixes while `auth_token`, `selectedAccountId` and `generatedDeviceIdentifier`
stay distinct.

`scripts/backup-metalstorm-config.sh` (whole-`user.reg` copy) remains the
disaster-recovery path for a single prefix.

### Known gap this does NOT cover

**Windows.** Direct `user.reg` editing is Wine-specific, so nothing here helps
share bindings with a Windows install, and the Makefile still carries a Windows
branch (`UNAME_S`). If Windows sharing is ever needed, the `.reg` route above
is still the right design and this ADR should be superseded rather than
reopened — per the project rule against editing an Accepted ADR.

Practically: bindings are configured once per Wine prefix and copied between
prefixes on the same machine, which is what multi-account operation
(Research 005) actually requires.

---

## References

- `scripts/backup-metalstorm-config.sh` — current backup script (raw `user.reg` copy)
- `scripts/sync-metalstorm-settings.py` — prefix-to-prefix settings/keybinding
  sync, identity excluded (the implemented solution)
- ~~`scripts/metalstorm-keybindings.reg`~~ — never created; see Resolution
- `docs/job-aids/011-wingman-keybindings.md` — recommended keybinding reference
- ADR 049 — Linux migration: game launcher and automation layer
- ADR 051 — Linux pitch control: joystick binding required
- Research 005 — multi-account run targets; the consumer that forced Open
  Question 3 to be decided
