# Job Aid 010 — Run MetalStorm on Linux (Heroic + Proton-GE)

| Status   | Date       | Wingman Version |
|----------|------------|-----------------|
| Draft    | 2026-08-23 | 1.8.5           |

MetalStorm is an Epic Games Store title with no native Linux client. This guide covers
installing and running it on Ubuntu via Heroic Games Launcher with Proton-GE, which is
the community-verified path as of mid-2026. Once MetalStorm ships on Steam the Steam +
Proton path will be simpler; until then, use this guide.

**Automation available:** `scripts/setup-linux.sh` covers everything below except Steps 2
and 3 (EGS login, MetalStorm install), which need a human and are done inside the
script's two pauses rather than as separate steps: Flatpak/Heroic install (Step 1),
Proton-GE download (part of Step 4), the i386 multiarch prerequisite, `uv`/dependency sync
and the `gi` bridge (Steps 5–6), and the `umu-run` symlink (Step 7, run after the pauses
since Heroic only fetches its bundled copy once a game install has happened). It does
**not** touch `input` group membership — see Step 8, that's obsolete. It does not run
`make r` for you (Step 9) — do that yourself once it finishes.

---

## Prerequisites

- Ubuntu 22.04 or later (other Debian-based distros should work)
- An Epic Games Store account with MetalStorm purchased
- GNOME on Wayland works directly — Wingman captures via PipeWire and injects input via
  XTest over XWayland (ADR 050/053). A pure X11 session also works. Neither root nor
  `input` group membership is required for either.
- **32-bit (i386) multiarch support**, even though MetalStorm itself is a 64-bit game.
  `umu-run`/Proton launch the game inside a Steam Runtime container (`pressure-vessel`),
  and that container's library-capture step needs the i386 architecture enabled to inject
  the host's real GPU driver — without it, DXVK inside the container silently falls back
  to seeing zero Vulkan devices (`DXVK: No adapters found`) even though the host GPU works
  fine outside the container. Check and fix before first launch:
  ```bash
  dpkg --print-foreign-architectures    # must include "i386" — empty means it's missing
  sudo dpkg --add-architecture i386
  sudo apt update
  sudo apt install libgl1:i386 mesa-vulkan-drivers:i386 libvulkan1:i386
  ```
  **If that install reports unmet dependencies on core packages** (`dpkg`, `python3`,
  `coreutils`, `tar`, `sed` wanting i386 counterparts, `pkgProblemResolver::Resolve
  generated breaks`) — do not force it with `apt --fix-broken install` or `-f`. That
  error means apt refused to proceed and nothing was changed. Two causes, check both:
  1. **A pending-upgrade backlog** (check `apt list --upgradable`). Clear it first
     (`sudo apt upgrade`) and retry.
  2. **A missing apt pocket.** A stock Ubuntu 24.04 install has four suites —
     `noble`, `noble-updates`, `noble-backports`, `noble-security` — but one machine
     had only `noble` and `noble-security` configured in
     `/etc/apt/sources.list.d/ubuntu.sources`. With `noble-updates` missing, several
     already-installed packages (e.g. `zlib1g`) had point-release versions that no
     longer matched *any* configured repo — `apt-cache policy zlib1g` showed the
     installed version backed only by `100 /var/lib/dpkg/status`, no repo line at
     all. That orphaning is what cascaded into apt wanting to touch `dpkg`/`python3`/
     `coreutils` on an unrelated i386 install. Diagnose with
     `grep Suites: /etc/apt/sources.list.d/ubuntu.sources` (should list all four);
     fix by appending the missing suite stanzas, matching the format of what's
     already there, then `sudo apt update && sudo apt upgrade` before retrying.
     **This was the actual root cause on the one machine that hit it** — clearing
     the upgrade backlog alone (cause 1) was necessary but not sufficient.
- `python3-tk` — required for `wingman/calibrate.py` and thus for `make test`, which
  collects `tests/test_calibrate_config_writer.py`. Not part of `uv sync`: `tkinter` has
  no PyPI wheel: it's a compiled binding to system Tcl/Tk, gated by how the interpreter
  itself was built. Ubuntu ships it as a separate package from base `python3`.
  ```bash
  sudo apt install python3-tk
  ```
- Internet connection for first-time Proton-GE (or equivalent) download (~1 GB)

---

## Step 1 — Install Heroic

Install the Flatpak from Flathub (preferred — keeps Heroic up to date automatically):

```bash
flatpak install flathub com.heroicgameslauncher.hgl
```

If Flatpak is not set up:

```bash
sudo apt install flatpak
flatpak remote-add --if-not-exists flathub https://flathub.org/repo/flathub.flatpakrepo
# then re-run the install command above
# log out and back in after adding the Flathub remote
```

---

## Step 2 — Log in to Epic Games Store

1. Open Heroic.
2. Click **Log in** → choose **Epic Games Store**.
3. Complete the OAuth login in the browser window.

---

## Step 3 — Install MetalStorm

1. In Heroic, open your **Library**.
2. Find MetalStorm and click **Install**.
3. Choose an install path with enough space (~3–5 GB) and let the download complete.

---

## Step 4 — Launch and Verify

Click **Play** in Heroic. On first launch Proton-GE will run initial setup (may take
30–60 seconds before the game window appears).

Expected: game window opens, keyboard and mouse respond, frame rate is acceptable.

**No manual Wine or UMU configuration is needed.** Heroic 2.22.0 applies Proton-GE (or
whatever default build it picks — see the note below) and UMU by default; MetalStorm
launched correctly without any changes to the per-game settings (verified 2026-06-13).
The community thread's advice to manually set UMU and Proton-GE in the game settings is
no longer necessary.

**Important:** launch Heroic from a non-snap terminal (GNOME Terminal, etc.), not from
VS Code's integrated terminal. VS Code installed as a snap rewrites `$HOME` and causes
the UMU shim lookup to fail. See the troubleshooting table below.

**Which Proton build you actually get varies by machine.** Heroic does not always
default to `GE-Proton-latest` — one machine got `Proton-CachyOS-latest` instead. Check
what's actually installed before assuming the Wingman Makefile default is right:

```bash
ls ~/.var/app/com.heroicgameslauncher.hgl/config/heroic/tools/proton/
```

If it isn't `GE-Proton-latest`, override `PROTON_ROOT` (see Step 9) — do not edit the
Makefile default, per the `?=` convention documented there.

---

## Step 5 — Install `uv` and sync Python dependencies

From the repository root:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
source $HOME/.local/bin/env    # or open a new terminal
uv sync --all-groups
```

`uv sync` builds `.venv` against the system Python interpreter (`uv run --active` follows
whatever `.venv` was created against) rather than downloading a standalone one, so system
packages that provide compiled extensions — `python3-tk` (above) and `gi` (next) — attach
to that same interpreter via the OS package manager, not via `uv`/`pip`.

---

## Step 6 — Bridge `gi` (PyGObject) into the venv

`wingman/capture.py`'s PipeWire backend imports `gi.repository.Gst`. Like `tkinter`,
`gi` has no PyPI wheel — it's a compiled binding to system GObject-introspection
libraries. Install the system package and GStreamer introspection data, then bridge them
into the uv-managed venv with a `.pth` file (Research 007):

```bash
sudo apt install python3-gi gir1.2-gstreamer-1.0
echo "/usr/lib/python3/dist-packages" > .venv/lib/python3.12/site-packages/system_gi_bridge.pth
```

Adjust the `python3.12` path segment to match your venv's actual Python version
(`ls .venv/lib/`). Verify:

```bash
uv run --active python -c "
import gi
gi.require_version('Gst', '1.0')
from gi.repository import Gst
Gst.init(None)
print('OK:', Gst.version_string())
"
```

---

## Step 7 — Install `umu-run` standalone

The README references `umu-run` as a standalone zipapp at `~/.local/bin/umu-run`, but
there's no separate install command anywhere upstream — Heroic already bundles its own
copy for its internal use. Symlink it rather than fetching a second copy:

```bash
mkdir -p ~/.local/bin
ln -sf ~/.var/app/com.heroicgameslauncher.hgl/config/heroic/tools/runtimes/umu/umu-run \
  ~/.local/bin/umu-run
umu-run --help    # sanity check
```

It's a self-contained Python zipapp (bundles its own deps: Xlib, urllib3, six, ...) with
no Flatpak sandbox dependency, so it runs fine outside Heroic.

---

## Step 8 — Linux User Groups for Wingman

**No longer required.** As of ADR 053, Wingman's Linux input path uses XTest via
`python-xlib` for both mouse clicks and keyboard injection, and X11 RECORD (XRecord) for
hotkey observation — none of which need root or `input` group membership. This step is
kept only as a historical note: an earlier design used the `keyboard` library, which did
require `input` group membership; that design was replaced.

---

## Step 9 — Run Wingman

From the repository root:

```bash
make r      # INFO console
make rd     # DEBUG log to wingman.log
```

By default the game is launched onto a **nested X display** (`nested.enabled: true`
in `wingman/config.yaml`, ADR 099) rather than your desktop. `make r` starts the
nested server, launches the game onto it, focuses it, and runs Wingman — all as
prerequisites, so there is no extra step. You can use the machine normally while
a session runs.

```bash
make nested-status   # up? which window holds focus?
make nested-stop     # tear it down
make rd NESTED=0     # run on your own screen instead, for one run
```

Two things about the nested lane that are not obvious:

- **The nested server needs your Wayland session.** Xwayland is itself a Wayland
  client, so `nested-setup` runs *without* the game's env. `WAYLAND_DISPLAY` is
  stripped for the game only — that stops Wine choosing `winewayland.drv` and
  bypassing the nested display — and stripping it for the server is fatal.
- **The nested window has no window manager.** Nothing repositions the game, so
  it maps at the origin and `game_window_offset` is exactly zero. Closing the
  nested server window takes the whole lane down with it.

Run `make preflight` first to surface any missing dependencies:

```bash
make preflight
```

`make preflight` currently still WARNs about `keyboard`/`input` group privileges — that
check predates ADR 053 and is stale; the warning is safe to ignore since the actual
Linux runtime path doesn't use that library.

If the game launches but Wingman never finds it (`PipeWireBackend: game window not
found`, or it locks onto the wrong window via the frame-diff fallback), the game most
likely never rendered a window — check `/tmp/wingman-game-launch.log` and the crash
directory under
`<prefix>/drive_c/users/steamuser/AppData/Local/Temp/Starform/Metalstorm/Crashes/`
before assuming it's a Wingman capture bug. See the troubleshooting table.

Steps 1–9 above were validated end-to-end on a second machine (Impulse, Ubuntu 24.04,
Intel Iris Xe integrated graphics) on 2026-08-23 — every symptom in the troubleshooting
table below was hit for real and confirmed fixed, `make g` reached a windowed, logged-in
lobby.

**Environment variables worth setting** (export in `.bashrc`, or pass on the `make`
command line — all four launcher paths are `?=` Makefile variables so environment
values win over the defaults):

| Variable | When needed |
|----------|--------------|
| `PROTON_ROOT` | Whenever Heroic didn't install `GE-Proton-latest` — check with the `ls` in Step 4 |
| `PROTON_USE_XALIA=0` | If the game crashes immediately with `Xalia ... SDL_Init: No displays available` — see troubleshooting |
| `GAME_ARGS` | Last-resort override to force a specific Unity graphics backend, e.g. `GAME_ARGS=-force-d3d11`. Root-cause DXVK/adapter issues (missing i386 multiarch, see Prerequisites) first — this is a fallback knob, not the primary fix |

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---------|-------------|-----|
| Every click and key suppressed, `FocusGuard: game does not have focus (None)` | ADR 098's guard is querying the wrong display — the game is on the nested one | Ensure `focus_guard.display` is unset in config so it follows the injection display automatically (ADR 099 D6) |
| Hotkeys (backspace, `end`, `i/j/k/l`) do nothing | Hotkey observation moved off your display | XRecord must stay on the operator's `DISPLAY`; only capture and injection move (ADR 099 D4) |
| Game exits at launch with `vulkan: No DRI3 support detected` | The nested server is Xephyr, which has no DRI3, so DXVK cannot present | Use rootful Xwayland — `scripts/nested-display.py` does. Xephyr cannot run the game at all |
| `umu-shim: No such file or directory` | Launched Heroic from VS Code snap terminal | Open GNOME Terminal (not VS Code) and launch Heroic from there |
| `PROTONPATH '...GE-Proton-latest' is not valid, toolmanifest.vdf not found` | `PROTON_ROOT` default doesn't match what's actually installed | `ls .../tools/proton/` (Step 4) and override `PROTON_ROOT` (Step 9) |
| `/bin/sh: 1: umu-run: not found` (in `/tmp/wingman-game-launch.log`) | `umu-run` never installed at `~/.local/bin/umu-run` | Step 7 — symlink Heroic's bundled copy |
| `python: not found` from a `make` target | `uv` not on `PATH` in that shell (installed after the terminal opened, or `.bashrc` not sourced) | `source ~/.local/bin/env` or open a new terminal; confirm with `command -v uv` |
| `ModuleNotFoundError: No module named 'tkinter'` | `python3-tk` not installed | Prerequisites — `sudo apt install python3-tk` |
| `ModuleNotFoundError: No module named 'gi'` | `gi` not bridged into the uv venv | Step 6 |
| Game crashes instantly: `Unhandled exception in Xalia ... SDL_Init: No displays available` | Proton's Xalia accessibility bridge doesn't work on this XWayland setup | `export PROTON_USE_XALIA=0` before launching |
| Game crashes: `d3d12: could not create a DXGI factory` or `d3d11: failed to create factory (80004005)` | DXVK inside the Steam Runtime container can't see a real GPU — almost always missing i386 multiarch, not a GPU driver problem | Prerequisites — enable i386 multiarch and install the `:i386` graphics libs. Confirm the real cause in `/tmp/steam-0.log` (`PROTON_LOG=1`): look for `DXVK: No adapters found` |
| Player.log: `Forced GfxDevice 'Vulkan' was not built from editor` | The Windows build doesn't include Vulkan shaders — `-force-vulkan` isn't a usable workaround for this game | Fix the underlying DXVK/adapter issue instead (row above) rather than forcing Vulkan |
| Wingman mouse/key injection does nothing, `Xlib.error.DisplayConnectionError: ... Authorization required` even though other XWayland apps work | Fixed in `wingman/input_linux.py` (`_ensure_xauthority` used to hardcode display `:0`; some sessions use a different XWayland display number, e.g. `:2`) | Update to a version of Wingman that reads the real `$DISPLAY` instead of assuming `:0`; if still on an old checkout, this is the diff to look for |
| `mss` capture returns black frames | Wayland without XWayland (rare — PipeWire capture is now the default on GNOME Wayland, ADR 050) | Enable XWayland in display settings or use an X11 session |

---

## References

- ADR 049 — Linux migration: game launcher and automation layer
- ADR 050 — PipeWire screen capture on GNOME Wayland
- ADR 053 — Linux one-command launch: input injection stack, no root/`input` group required
- ADR 047 — Host environment pre-flight check
- Research 007 — PyCharm IDE fit (documents the `system_gi_bridge.pth` approach)
- Heroic Games Launcher: [Flathub page](https://flathub.org/apps/com.heroicgameslauncher.hgl)
- Proton-GE: available via Heroic Wine Manager → PROTON-GE tab (or whatever build Heroic
  actually installs — check before assuming)
