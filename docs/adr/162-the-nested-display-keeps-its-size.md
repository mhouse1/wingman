# ADR 162 — The Nested Display Keeps Its Size

| Status | Date       | Wingman Version |
|--------|------------|-----------------|
| Draft  | 2026-10-25 | 1.9.3           |

Supersedes the assumption in [ADR 104](104-nested-display-fullscreen-on-launch.md) that
`-geometry` fixes the nested display's screen for the life of the server ("a wrong placement
decision costs window placement, never captured pixels"). ADR 104's placement decision stands:
`-fullscreen` when the host has no room beside the operator's work, `-decorate` otherwise.

## Decision

**D1. While the nested lane runs, wingman keeps the nested screen at least `nested.size`.** A
guard thread (`wingman/nested_size_guard.py`, `NestedSizeGuard`) reads the screen's size every
second. When it is smaller than `nested.size` in width or height, the guard runs
`xrandr --display <nested> --output <its connected output> --mode <nested.size>`.

**D2. A larger screen is left alone.** Capture still fits inside it, and on a host bigger than
the framebuffer a `-fullscreen` window may legitimately make the screen larger. Setting it back
there would fight the compositor every second.

**D3. On by default, behind `nested.restore_size`.** Each restore logs a warning with the size it
found and a running count. A restore that fails (no `xrandr`, mode refused, no connected output)
logs a warning and is retried after 30 s, not every second.

## Consequences

- A resize of the nested window costs at most about a second of failed grabs instead of every
  grab until the operator notices. On 2026-10-25 it cost about ten minutes: 395
  `Frame capture failed` lines from 16:21:27 to 16:31:19, and 325 unseen `f` presses
  ([Anomaly 015](../anomaly/015-a-resized-nested-window-blinds-capture.md)).
- The operator can no longer make the `-decorate` window shorter or narrower than `nested.size`:
  the screen, and with it the window, goes straight back. Making it larger still works.
- Wingman now needs `xrandr` (Ubuntu `x11-xserver-utils`) on the host for this. Without it the
  guard logs one warning every 30 s and capture behaves as before. The Docker test image does not
  need it: the tests replace the command.
- One more daemon thread and one more X connection to the nested display while a session runs.

## Why

ADR 104 rests on `-geometry` setting the server's own framebuffer "independent of how the
compositor displays it". On Xwayland 24.1.10 that is true for the starting size only. Started
with `-decorate`, the server is an ordinary resizable window and resizes its screen to match the
window: at 16:30 on 2026-10-25 `:3` reported `current 1920 x 1197`, with a `1920x1197` RandR mode
the server had made beside its standard ones. The game's `Wine Desktop` window kept 1920x1200,
so the game was fine. Only capture, whose region is the configured 1920x1200, failed. Setting the
mode back by hand restored capture at the next grab.

## Alternatives considered

- **Start Xwayland without `-decorate`.** The window would then have no frame to drag and no
  title bar to double-click, but the compositor can still resize it (snapping, tiling,
  keyboard shortcuts), and ADR 104 chose `-decorate` so the operator can move the window
  beside their work.
- **Capture whatever size the screen is.** Every crop and offset is calibrated against
  1920x1200; a shorter screen would cut the bottom of the game, where the HUD's health and ammo
  sit.
- **Check only in `make nested-focus`.** It runs at launch; the resize came 45 minutes into the
  session.
- **Listen for RandR screen-change events instead of polling.** Faster by up to a second, but
  python-xlib 0.15 misparses this server's CRTC and output replies (an output named `LAND0` for
  `XWAYLAND0`), and a one-second poll of `get_geometry` is a single round trip.

## Validation

- **V1.** `tests/test_nested_size_guard.py`: a shorter or narrower screen is set back with the
  output `xrandr --query` names; the configured size or larger is left alone; a refused mode, a
  missing `xrandr` and no connected output are each reported without raising; the shipped config
  keeps the guard on.
- **V2, live server.** 2026-10-25 16:36:11 on `Xwayland :3` pid 2580999, no game running: shrunk
  to 1920x1080 by hand, `check_once` set it back to 1920x1200 in 16 ms. Done.
- **V3, live flight.** Resize the nested window during a battle. Expect one
  `ADR 162: :3 shrank to …` warning, at most one `Frame capture failed`, and the `Wine Desktop`
  window still 1920x1200 afterwards. Outstanding.
