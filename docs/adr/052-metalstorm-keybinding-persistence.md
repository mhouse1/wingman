# ADR 052 — MetalStorm Keybinding Persistence and Cross-Platform Sharing

| Status   | Date       | Wingman Version |
|----------|------------|-----------------|
| Draft    | 2026-06-15 | 1.6.19          |

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

3. **Settings beyond keybindings**: the `inputBindingOverrides1` JSON blob contains
   keybindings only. Other settings (volume, HUD colour, control scheme) are in
   separate registry values in the same key. Decide whether to export the full
   `[Software\Starform\Metalstorm]` key (simpler) or filter to keybinding-only values
   (safer to share across accounts).

---

## Acceptance Criteria

- `./scripts/backup-metalstorm-config.sh export` produces a valid `.reg` file that
  can be imported on Linux and Windows.
- `./scripts/backup-metalstorm-config.sh import` applies the `.reg` file to the Wine
  prefix and keybindings take effect after restarting the game.
- `scripts/metalstorm-keybindings.reg` is committed to the repository.
- The script resolves the Proton Wine binary path without hardcoding a version string.

---

## References

- `scripts/backup-metalstorm-config.sh` — current backup script (raw `user.reg` copy)
- `scripts/metalstorm-keybindings.reg` — canonical keybinding export (to be created)
- `docs/job-aids/011-wingman-keybindings.md` — recommended keybinding reference
- ADR 049 — Linux migration: game launcher and automation layer
- ADR 051 — Linux pitch control: joystick binding required
