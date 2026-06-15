# Job Aid 010 — Run MetalStorm on Linux (Heroic + Proton-GE)

| Status   | Date       | Wingman Version |
|----------|------------|-----------------|
| Draft    | 2026-06-13 | 1.6.19          |

MetalStorm is an Epic Games Store title with no native Linux client. This guide covers
installing and running it on Ubuntu via Heroic Games Launcher with Proton-GE, which is
the community-verified path as of mid-2026. Once MetalStorm ships on Steam the Steam +
Proton path will be simpler; until then, use this guide.

**Partial automation available:** `scripts/setup-linux.sh` handles Heroic install,
Proton-GE download, and `input` group setup automatically, then pauses at the three
steps that require a human (EGS login, game install, in-UI settings). Run it instead of
Steps 1–7 below if preferred.

---

## Prerequisites

- Ubuntu 22.04 or later (other Debian-based distros should work)
- An Epic Games Store account with MetalStorm purchased
- X11 session (or XWayland enabled on a Wayland desktop) — required for Wingman screen
  capture and input injection
- Internet connection for first-time Proton-GE download (~1 GB)

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

**No manual Wine or UMU configuration is needed.** Heroic 2.22.0 applies Proton-GE and
UMU by default and MetalStorm launched correctly without any changes to the per-game
settings (verified 2026-06-13). The community thread's advice to manually set UMU and
Proton-GE in the game settings is no longer necessary.

**Important:** launch Heroic from a non-snap terminal (GNOME Terminal, etc.), not from
VS Code's integrated terminal. VS Code installed as a snap rewrites `$HOME` and causes
the UMU shim lookup to fail. See the troubleshooting table below.

---

## Step 5 — Linux User Groups for Wingman

Wingman's key injection (`keyboard` library) requires either `root` or `input` group
membership. Mouse injection (`pynput`) works without root when `DISPLAY` is set.

Add your user to the `input` group once:

```bash
sudo usermod -aG input $USER
# log out and back in for the change to take effect
```

Verify:

```bash
groups | grep input
```

---

## Step 6 — Run Wingman

From the repository root:

```bash
make r      # INFO console
make rd     # DEBUG log to wingman.log
```

Run `make preflight` first to surface any missing dependencies or group issues:

```bash
make preflight
```

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---------|-------------|-----|
| `umu-shim: No such file or directory` | Launched Heroic from VS Code snap terminal | Open GNOME Terminal (not VS Code) and launch Heroic from there |
| Game crashes before main menu | Corrupted Proton-GE download | Re-download Proton-GE-latest from Heroic Wine Manager |
| Wingman mouse clicks do nothing | `DISPLAY` not set | `echo $DISPLAY` — should be `:0` or `:1`; run Wingman from an X11 terminal |
| Wingman key injection fails | Not in `input` group | `sudo usermod -aG input $USER` then log out/in |
| `mss` capture returns black frames | Wayland without XWayland | Enable XWayland in display settings or use an X11 session |

---

## References

- ADR 049 — Linux migration: game launcher and automation layer
- ADR 047 — Host environment pre-flight check
- Heroic Games Launcher: [Flathub page](https://flathub.org/apps/com.heroicgameslauncher.hgl)
- Proton-GE: available via Heroic Wine Manager → PROTON-GE tab
