# ADR 049 — Linux Migration: Game Launcher and Automation Layer

| Status   | Date       | Wingman Version |
|----------|------------|-----------------|
| Accepted | 2026-06-15 | 1.6.19          |

## Context

Wingman currently runs on Windows only. The game (MetalStorm PC) is launched natively
and Wingman automates it via Win32 mouse injection (`ctypes.windll.user32`) and the
`keyboard` library for key injection. The goal is to run the full stack — game plus
Wingman automation — on Linux.

There are two independent problems to solve:

1. **Running the game on Linux** — MetalStorm has no native Linux client; a
   compatibility layer is required.
2. **Wingman's automation layer on Linux** — several input injection calls are
   Windows-only and will silently fail on Linux with the current code.

---

## Part 1 — Running the Game on Linux

### Availability Note

MetalStorm PC is an Epic Games Store title. As of June 2026, it is **not yet available
on Steam**, though a Steam release is expected in the near future. Once it ships on
Steam, the Steam + Proton path below becomes simpler; until then, Heroic is the primary
launcher.

### Community Evidence

A Reddit thread (r/linux_gaming) documents a user successfully running MetalStorm on
Ubuntu via Heroic after other methods failed. The accepted solution:

> Go to Heroic settings → Advanced → enable "use UMU as Proton runtime". Then open
> Wine Manager → PROTON-GE section, download Proton-GE-latest. In the game's settings,
> change the Wine version to Proton-GE-latest.

The poster noted that UMU + Proton-GE gives a Steam-like Proton environment for non-Steam
games. Multiple respondents confirmed Heroic over Lutris. One ProtonDB report for
MetalStorm marks it "borked" under the default runtime, reinforcing the need for Proton-GE.

### Options Evaluated

**Heroic Games Launcher + Proton-GE**

Heroic is an open-source launcher for Epic Games Store and GOG titles. It supports
UMU as an alternative Proton runtime, allowing non-Steam games to use Proton without
requiring a Steam installation. Community reports confirm MetalStorm runs on Ubuntu via
Heroic with Proton-GE; the default Proton runtime is insufficient (ProtonDB: "borked").

Proton-GE (GloriousEggroll community build) provides better codec support and input
handling than stock Valve Proton for non-Steam titles and is available through Heroic's
Wine Manager.

**Steam + "Enable Steam Play for all titles"**

MetalStorm is not yet on Steam. It can be added as a non-Steam shortcut and run via
Steam's Proton layer, but this is more cumbersome for an EGS title and Proton support
for non-Steam shortcuts is less polished than for native Steam catalogue entries.
Once MetalStorm ships on Steam this path becomes the simpler option.

**UMU-launcher (standalone)**

UMU is a standalone Proton runner that does not require either Steam or Heroic. It
provides a Steam-like Proton environment for arbitrary Windows executables. This is the
engine that Heroic uses internally when "use UMU as Proton runtime" is enabled.

**Native Linux client**

No native Linux build of MetalStorm exists. Compatibility layers are required.

### Decision — Game Launcher

Use **Heroic Games Launcher with Proton-GE-latest** as the primary path until MetalStorm
ships on Steam:

- Install Heroic from Flathub (`com.heroicgameslauncher.hgl`) for the latest version.
- Log in to Epic Games Store, install MetalStorm, and launch — no per-game Wine or UMU
  configuration is needed. Heroic's defaults (UMU runtime, Proton-GE-latest) applied
  automatically and the game ran without any manual settings changes.
- Re-calibrate crop coordinates on Linux after first successful launch.
- Steam shortcut remains a documented fallback (and becomes the preferred path once
  MetalStorm is available on Steam).

### Confirmed Working Configuration

Verified 2026-06-13 on:

| Component | Value |
|-----------|-------|
| OS | Ubuntu 24.04.4 LTS (Noble Numbat) |
| CPU | Intel Core Ultra 7 265 (20 cores) |
| GPU | Intel Arrow Lake-S (i915 driver) |
| Heroic | 2.22.0 Hajrudin (Flatpak) |
| Proton build | GE-Proton10-34 via UMU 1.4.0 |
| UMU runtime | steamrt3 (sniper) |
| Install path | `/home/michael/Games/Heroic/Metalstorm` |
| Wine prefix | `/home/michael/Games/Heroic/Prefixes/Metalstorm` |
| Proton tools path | `~/.var/app/com.heroicgameslauncher.hgl/config/heroic/tools/proton/` |

### Known Gotcha — Snap Terminal Environment Pollution

Launching Heroic from VS Code's integrated terminal (VS Code is installed as a snap)
causes `umu-shim` to fail. The snap sandbox rewrites `$HOME` to
`/home/<user>/snap/code/<rev>/` so UMU resolves its shim path to the sandboxed home
instead of the real one:

```
pv-adverb: E: Failed to execute child process
  "/home/michael/snap/code/247/.local/share/umu/umu-shim" (No such file or directory)
```

**Fix:** always launch Heroic (and run the setup script) from a non-snap terminal such
as GNOME Terminal. Do not use VS Code's integrated terminal for any step involving
Heroic or UMU.

---

## Part 2 — Wingman Automation Layer on Linux

### Current Windows Dependencies

| Component | Location | Mechanism | Linux status |
|-----------|----------|-----------|--------------|
| Mouse click | `controller.py:_raw_click` (inside `click_grid_region` and `click_crop`) | `ctypes.windll.user32.SetCursorPos` + `mouse_event` | **Broken** — explicit `sys.platform != "win32"` guard silently returns |
| Key injection | `controller.py` throughout | `keyboard` library (`press`, `release`, `press_and_release`) | Works, but requires root or `input` group membership on Linux |
| Screen capture | `capture.py`, daemon threads in `controller.py` | `mss` | Works on X11; requires XWayland when desktop is Wayland-native |

The mouse injection is the only hard blocker. Both `click_grid_region` and `click_crop`
already contain the guard:

```python
if sys.platform != "win32":
    logger.error("click_crop: Win32 mouse_event not available on %s", sys.platform)
    return
```

This means every UI click (lobby PLAY button, continue prompts, all FSM transitions
that require clicking) silently does nothing on Linux today.

### Why Wine/Proton Does Not Change the Input Path

When MetalStorm runs under Proton, it is a Wine process receiving native Linux input
events. Mouse clicks sent from the Linux host via X11 or Wayland are translated by
Wine into Windows mouse events that the game receives normally. Wingman therefore does
not need to inject input inside the Wine process — it injects at the Linux OS level and
Wine handles the translation automatically.

### Decision — Mouse Injection Replacement

Replace `ctypes.windll.user32.SetCursorPos` + `mouse_event` with **`pynput`**:

- `pynput` is a pure-Python cross-platform mouse and keyboard library.
- On Linux it drives X11 (`Xlib`) or `uinput`; on Windows it uses Win32 API
  transparently.
- It does not require root on Linux when X11 is available (unlike `keyboard` which
  requires root or `input` group for global key hooks).
- It is already a viable dependency given the project's existing Python toolchain.

The change is contained to the `_raw_click` inner function that appears in both
`click_grid_region` and `click_crop`. No other code paths need to change for basic
Linux compatibility.

### Decision — Keyboard Injection

Keep the `keyboard` library for key injection. On Linux it requires either:

- Running Wingman as root (`sudo`), or
- Adding the user to the `input` group: `sudo usermod -aG input $USER` (log out and
  back in to take effect).

The `input` group approach is preferred for daily use. Document this in the pre-flight
check (ADR 047).

### Decision — Screen Capture

`mss` uses `XGetImage` on the X11 root window, which Wayland compositors block as a
security measure. On a Wayland session (`XDG_SESSION_TYPE=wayland`), `mss` silently
returns `None` on every frame.

The actual solution is documented in **ADR 050**: MetalStorm runs inside a Wine virtual
desktop — a 1920×1200 XWayland window — and Wingman captures that specific window using
`xwd -id <window_id> -silent` (~38 ms per frame). A platform-dispatched backend
(`_XwdBackend` on Wayland, `_MssBackend` on Windows/X11) was added to `capture.py` so
the Windows path is unchanged. See ADR 050 for full details.

The Wine virtual desktop requires two registry keys in the Proton prefix:

```
[Software\Wine\Explorer]
"Desktop"="Default"

[Software\Wine\Explorer\Desktops]
"Default"="1920x1200"
```

---

## Scope

In scope:

- Replace `ctypes.windll.user32` mouse injection with `pynput` in `controller.py`.
- Add `pynput` to `pyproject.toml` dependencies.
- Update the pre-flight check (ADR 047) to probe `input` group membership on Linux
  and surface the `pynput` import.
- Document the Heroic + Proton-GE setup in a job aid.

Out of scope:

- Multi-monitor layout differences between Windows and Linux (deferred; calibrate
  separately on Linux hardware).
- CI running on Linux (the test suite mocks OS input via `simulate_os_input`; no
  platform change needed there).

---

## Implementation Approach

1. Add `pynput` to `pyproject.toml` dependencies and run `uv sync`.

2. In `controller.py`, replace the `_raw_click` inner function in both
   `click_grid_region` and `click_crop`:

   ```python
   # Before (Windows only):
   def _raw_click(x, y):
       ctypes.windll.user32.SetCursorPos(x, y)
       time.sleep(0.05)
       ctypes.windll.user32.mouse_event(0x0002, 0, 0, 0, 0)
       time.sleep(0.05)
       ctypes.windll.user32.mouse_event(0x0004, 0, 0, 0, 0)

   # After (cross-platform via pynput):
   def _raw_click(x, y):
       from pynput.mouse import Button, Controller as MouseController
       mouse = MouseController()
       mouse.position = (x, y)
       time.sleep(0.05)
       mouse.press(Button.left)
       time.sleep(0.05)
       mouse.release(Button.left)
   ```

3. Remove the `if sys.platform != "win32": return` guards from both click methods —
   they are no longer needed once `pynput` handles both platforms.

4. Keep `import ctypes` only if other code still uses it; otherwise remove.

5. Update the ADR 047 pre-flight check to include:
   - `pynput` import check.
   - On Linux: probe `input` group membership and emit a warning if absent, explaining
     the `sudo usermod -aG input $USER` fix.

---

## Acceptance Criteria

- `make r` launches Wingman on Linux without errors in the controller init path.
- Clicking the PLAY crop in GAME_LOBBY moves the real mouse cursor to the correct
  screen position and fires a left click.
- Key injection (flares, maneuvers) works without root when user is in `input` group.
- `make test` suite passes unchanged (it uses `simulate_os_input` and is not affected
  by the platform change).
- `make preflight` reports `pynput` as installed and surfaces the `input` group warning
  on Linux if applicable.

---

## Consequences

Positive:

- Wingman runs on Linux, removing the Windows-only hardware requirement.
- `pynput` is cleaner and more maintainable than `ctypes.windll` calls; cross-platform
  by design.
- Opens the path to running multiple Wingman instances on a lightweight Linux headless
  server with a virtual display.

Trade-offs:

- `pynput` on Linux requires an X11 display (`DISPLAY` must be set); will not work in
  a headless environment without Xvfb or similar.
- `keyboard` library still requires `input` group on Linux — this is a one-time setup
  step per machine, surfaced by the pre-flight check.
- Proton/Wine adds a compatibility layer between Wingman's click coordinates and the
  game's actual input handling; coordinate calibration must be re-done on Linux.

## Alternatives Considered

1. `xdotool` subprocess calls for mouse injection.
   - Rejected: external process dependency, harder to install via `uv`, and subprocess
     latency is less predictable than a Python library call.

2. `pyautogui` for mouse and keyboard.
   - Rejected: already evaluated; `pyautogui` has known issues with multi-monitor
     coordinate mapping and requires `scrot`/`pillow` on Linux. `pynput` is narrower
     and more reliable for input injection only.

3. Keeping Win32 calls and using Wine's `SetCursorPos` inside the Proton process.
   - Rejected: requires running Wingman inside the Wine environment, which breaks the
     clean separation between the automation layer (host) and the game (Wine).

## In-Game Keybindings — Manual Setup Required

MetalStorm's control bindings must be configured manually after first launch on Linux.
The full recommended keybinding set is in the job aid:

**`docs/job-aids/011-wingman-keybindings.md`**

### Critical Linux-specific requirement — Pitch control

Under Wine on XWayland, `GetAsyncKeyState` (which MetalStorm uses for continuous pitch
input) does not reliably read held keys. Keyboard pitch bindings silently do nothing
regardless of which keys are assigned.

**Fix:** In MetalStorm **Settings → General**, set Controls to **Controller / Joystick**
mode. Pitch bindings then use the DirectInput device path, which Wine handles correctly.
Roll and all other controls work in either mode. See ADR 051 for the full investigation.

---

## Implementation Results

The final implementation differed significantly from the plan. See **ADR 053** for the complete technical record; the summary is below.

| Area | Planned | Actual |
|---|---|---|
| Mouse injection | `pynput` | `python-xlib` XTest (`Xlib.ext.xtest.fake_input`) |
| Key injection | `keyboard` library + `input` group | `python-xlib` XTest — no root, no `input` group |
| Hotkey listening | `keyboard.on_press_key` + `input` group | `python-xlib` XRecord (`Xlib.ext.record`) — non-consuming, no root |
| Screen capture | `mss` + XWayland | PipeWire XDG Desktop Portal (`_PipeWireBackend`) |
| Game window position | Manual `config.yaml` | `xwininfo -root -tree` auto-detection (primary) |
| Game launch | Manual (open Heroic, click Play) | `make r` via `umu-run` (fully automated) |

### Mouse and Keyboard Injection

`pynput` was never installed and was abandoned. `pyautogui` (already in `pyproject.toml`) failed at import time because mutter's XWayland auth file uses a wildcard display number that `python-xlib` cannot match. The fix was to create a temporary xauth file with an explicit `:0` entry, then use `python-xlib` XTest directly for both mouse clicks and key injection. No root, no `sudo`, no `input` group membership is required.

**Hotkey listening** (physical key presses triggering callbacks) was first attempted with `XGrabKey`, which intercepts keys but also prevents them from reaching the game window. The correct solution is the X11 RECORD extension, which observes events non-destructively — the game receives every keystroke and our handler is also called. The `keyboard` module is not used on Linux at all.

### Screen Capture

`mss` on Wayland returns `None` for all frames. The adopted solution is PipeWire via the XDG Desktop Portal ScreenCast API (`types=1`, monitor capture). A restore token saved on first grant skips the permission dialog on all future runs. See ADR 050 for the full investigation of `xwd`, GStreamer, and other approaches that were tried before PipeWire.

### Game Window Position

Wine/Proton registers an XWayland window even for DXVK/Vulkan games. `xwininfo -root -tree` finds the `"Metalstorm"` window and returns its absolute position, which maps 1:1 to PipeWire monitor frame coordinates. This replaces all manual `game_window_offset` configuration.

### Game Launch Automation

`umu-run` (the Proton launcher Heroic uses internally, installed as a standalone zipapp) launches MetalStorm directly without requiring Heroic's UI. `make r` now chains `launch-game` (kill stale instance + `umu-run` in background) → `wait-game` (poll for process + sleep for lobby load) → Wingman start. All paths are configurable via Makefile variables (`UMU_RUN`, `PROTON_ROOT`, `WINE_PREFIX`, `GAME_EXE`).

**VS Code terminal note:** `make r` must be run from a non-snap terminal (e.g. GNOME Terminal). VS Code installed as a snap rewrites `$HOME` and breaks `umu-shim`. See ADR 049 Known Gotcha section above.

---

## References

- [ADR 014](014-mouse-click-via-win32-mouse-event.md) — mouse click via Win32 mouse_event (original Windows decision)
- [ADR 047](047-host-environment-preflight-check.md) — host environment pre-flight check
- [ADR 050](050-wayland-screen-capture.md) — Wayland screen capture (PipeWire backend)
- [ADR 051](051-linux-pitch-control-joystick-binding.md) — Linux pitch control: joystick binding required
- [ADR 053](053-linux-one-command-launch.md) — **Full technical record**: window detection, XAUTHORITY, XTest mouse/keyboard injection, XRecord hotkey listening, game auto-launch
- `wingman/controller.py` — `_LinuxXTestKeyboard`, `_linux_click()`, `_linux_key_event()`
- `wingman/capture.py` — `_PipeWireBackend`, `_detect_via_x11()`
- `Makefile` — `launch-game`, `wait-game`, `r` targets
- `docs/job-aids/011-wingman-keybindings.md` — recommended MetalStorm keybindings
