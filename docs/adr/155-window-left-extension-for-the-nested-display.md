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

**D5. The extension keeps the window drawn at full rate while it is hidden.**
Added 2026-10-04 as version 4. Behind another window the game fell to one frame a
second: mutter sends a hidden window no word that its frame was shown, and
Xwayland then waits a second per frame
([ADR 099](099-nested-display-lane-for-unattended-operation.md), V3). Mutter
exempts a window that has a copy of itself on the desktop, so `_keepShown` holds
one for as long as the window lives: a `Clutter.Clone` of the window, one pixel
in the corner of the screen, see-through and taking no input. It is made when
the window's first frame arrives and destroyed when the window closes or the
extension is disabled. Only the Shell can do this, which is why it lives here
and not in wingman.

## Consequences

- After `make upgrade-linux` and one logout/login, the `Xwayland on :3` window
  opens at the top-left on every launch.
- Capture is unaffected either way. The nested display's framebuffer is
  captured directly, and the game sits at `+0+0` inside it (ADR 099). This
  decision is about the operator's screen layout.
- D4 can declare a GNOME version the extension was never run on. If a future
  Shell changes the placement API, the extension fails to load and the window
  falls back to Mutter's default placement. Nothing worse happens.
- With D5 the desktop does a frame for every frame the game draws, whether or
  not the window can be seen. That is the point of it, and it is also a cost:
  a hidden game window no longer lets the desktop idle.
- D5 rests on one condition in mutter's source (`has_mapped_clones` in
  `meta_surface_actor_wayland_is_view_primary`, read in 50.1). A later mutter
  can change it without notice, and the sign is the one-a-second picture coming
  back. The journal line `holding a one-pixel copy` says only that the copy was
  made, not that mutter still honours it.
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
- [x] VEDA, 2026-10-02: after `make upgrade-linux` and a re-login,
      `gnome-extensions info` shows `Version: 2`, `State: ACTIVE`.
- [x] VEDA, 2026-10-02 05:26: the journal shows
      `wingman-window-left: moved "Xwayland on :9" from 1553,516 to 67,32`
      (the work-area origin: right of the dock, below the top bar).
- [x] VEDA, 2026-10-04 13:11: after a re-login `gnome-extensions info` shows
      `Version: 4`, `State: ACTIVE`, and at 13:12:04 the journal shows
      `holding a one-pixel copy of "Xwayland on :3" ... (mapped=yes)`.
- [x] VEDA, 2026-10-04 13:12 to 13:19, D5: a battle of 6 min 38 s flown with the
      window fully covered by the editor. Picture rate median 41.7 a second,
      lowest reading 27.5, no deaths. Without the copy the same cover gave 0.8 a
      second (12:27 the same day). Figures: Design 001, the rows for 12:27 and 13:12.
- [ ] D5 with the window minimised, on another workspace, behind the lock
      screen, and with the monitor off.
- [ ] VEDA: the operator confirms by eye that `make r` opens the
      `Xwayland on :3` window at the top-left.
- [ ] A second Linux PC, after `make upgrade-linux` and a logout.
