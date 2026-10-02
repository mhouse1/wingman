# ADR 155 — Window-Left Extension: Nested Display, Shipped in the Repo

| Status | Date       | Wingman Version |
|--------|------------|-----------------|
| Draft  | 2026-10-02 | 1.9.0           |

Supersedes [ADR 057](057-gnome-shell-extension-window-left-placement.md).

## Decision

**D1. The extension places the nested display's window.** `wingman-window-left`
now matches the rootful Xwayland window (title `Xwayland on :N`, app_id
`org.freedesktop.Xwayland`), which has been the window on the operator's desktop
since [ADR 099](099-nested-display-lane-for-unattended-operation.md). It still
matches the game's own window (`steam_app_0`) for `NESTED=0` runs. Either window
opens at the top-left of its monitor's work area, just below GNOME's top bar.

**D2. The move hooks the window actor's `first-frame` signal.** ADR 057 connected
`first-frame` on the `MetaWindow`, but in Mutter the signal belongs to the
`MetaWindowActor`. The extension now waits one idle for the actor and checks the
target on the first frame, since a Wayland client's title and app_id may not be
set at creation. It then moves the window and re-checks four times at 250 ms
intervals in case Mutter's initial placement lands after the move (ADR 057's
Known Issue). After that one second, an operator's drag is left alone.

**D3. The extension lives in the repo and `make upgrade-linux` installs it.** The
source is `scripts/gnome-extension/wingman-window-left@wingman.local/`.
`scripts/upgrade-linux.sh` Step 6 ([ADR 154](154-ubuntu-upgrade-path.md))
copies it to `~/.local/share/gnome-shell/extensions/`, adds it to
`org.gnome.shell enabled-extensions`, and warns that a logout is needed when
anything changed. Every Linux PC therefore gets it, and fresh installs get it
through `setup-linux.sh`.

**D4. The installed copy declares the running Shell version.** The repo lists the
versions it targets (46 to 50). The installer adds the running Shell's major
version when it's missing and says so. The next GNOME upgrade then can't
silently mark the extension OUT OF DATE, which is how 26.04 broke it.

## Consequences

- After `make upgrade-linux` and one logout/login, the `Xwayland on :3` window
  opens at the top-left on every launch.
- Capture is unaffected either way. The nested display's framebuffer is
  captured directly, and the game sits at `+0+0` inside it (ADR 099). This
  decision is about the operator's screen layout.
- D4 can declare a GNOME version the extension was never run on. If a future
  Shell changes the placement API, the extension fails to load and the window
  falls back to Mutter's default placement. Nothing worse happens.
- GNOME Shell's journal records each move:
  `journalctl --user -b _COMM=gnome-shell | grep wingman-window-left`.

## Why

After the Ubuntu 26.04 upgrade (GNOME Shell 50.1), the nested display's window
no longer opened at the top-left. On VEDA, 2026-10-02:

```
$ gnome-extensions info wingman-window-left@wingman.local
  Version: 1
  Enabled: No
  State: OUT OF DATE
$ cat .../metadata.json
    "shell-version": ["46"],
```

Updating the version list alone would not have helped. The installed extension
matched only `steam_app_0`, a window that has not been on the operator's desktop
since the nested lane became the default. Its `first-frame` hook was also on the
wrong object, which explains ADR 057's "sometimes lands at (0,0)": the move never
ran, and the occasional top-left placement was Mutter's own.

## Validation

- [x] `extension.js` parses (`gjs -m`; only the Shell-internal import is
      unresolvable outside the Shell), and `metadata.json` is valid JSON.
- [x] Install step, against a scratch HOME with stand-in GNOME tools: first
      install, no-op rerun, a newer Shell (51 declared), and an empty
      enabled-extensions list.
- [ ] VEDA: `make upgrade-linux` from a non-snap terminal, then log out and in;
      `gnome-extensions info` shows `State: ACTIVE`.
- [ ] VEDA: `make r` opens the `Xwayland on :3` window at the top-left, and the
      journal shows the `wingman-window-left: moved` line.
- [ ] A second Linux PC, after `make upgrade-linux` and a logout.
