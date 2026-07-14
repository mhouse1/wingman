# ADR 057 — GNOME Shell Extension for Deterministic Wine Desktop Window Placement

| Status   | Date       | Wingman Version |
|----------|------------|-----------------|
| Draft | 2026-07-11 | 1.6.23          |

## Context

The MetalStorm Wine virtual desktop window (ADR 050) is not placed at a fixed
screen position by GNOME/Mutter on Wayland. Mutter applies its own window
placement heuristic to each newly mapped top-level window, and that placement
varies run to run. Confirmed from `wingman.log` across two separate launches:

```
wingman.log, 2026-07-05 20:47:42,217 [INFO] PipeWireBackend: game window found via xwininfo at (116, 119)
xwininfo -root -tree,  2026-07-11: 0x3000003 "Metalstorm": ("steam_app_0" "steam_app_0")  1920x1200+0+0  +266+269
```

The window is 1920x1200; the physical display is 3840x1600 (`xrandr`), leaving
1920px of horizontal and 400px of vertical slack for Mutter to place the window
within. This does not break Wingman's functionality — `make r` re-detects the
window's actual position via `xwininfo` on every launch (ADR 053/054) — but it
is a cosmetic annoyance and makes manual screen layout (e.g. keeping the game
flush against the left edge) impossible to rely on.

## Investigation — Approaches Evaluated

### Path 1: Raw X11 `ConfigureWindow` on the client window

`wingman/move_game_window.py` (ADR 054) already issues
`window.configure(x=x, y=y)` against the game's client window. Live-tested by
moving the window and re-checking `xwininfo`:

```
before: 1920x1200+0+0    +266+269
after:  1920x1200+0+269  +266+538   (requested x=0, y=269)
```

**Result: worse than a no-op.** Once the window manager reparents a top-level
window, a `ConfigureWindow` request the *client* issues on itself is
interpreted relative to its immediate X11 parent — which is now the WM's frame
window, not the root. So the request does not move the frame on screen at all;
it shifts the client's rendered content *within* the frame. Because the frame
is exactly the client's size with no border, this silently clips/misaligns the
visible game content — a real capture-alignment hazard, not just an
ineffective fix. The shift was reverted live by re-issuing
`configure(x=0, y=0)` to zero the relative offset back out.

### Path 2: EWMH `_NET_MOVERESIZE_WINDOW` client message

The standard mechanism pager tools (`wmctrl -r`, `xdotool windowmove`) use:
a `ClientMessage` sent to the root window with
`SubstructureRedirectMask | SubstructureNotifyMask`, asking the window manager
itself to reposition the frame. Tested in isolation (no raw configure
involved):

```
before: 1920x1200+0+0  +266+269
after:  1920x1200+0+0  +266+269   (requested x=0, y=269 — no change)
```

**Result: silently ignored.** Mutter does not honor `_NET_MOVERESIZE_WINDOW`
for this window under Wayland. This matches widely-reported GNOME/Wayland
behavior: window placement is the compositor's prerogative, and legacy X11/EWMH
client requests for absolute position are not binding on Wayland sessions the
way they are on X11 sessions.

### Path 3: `org.gnome.Shell.Eval` (call Mutter's own `move_frame()` via D-Bus)

Since only the compositor itself can reposition a window under Wayland, the
next idea was to ask the compositor to do it, via GNOME Shell's `Eval` D-Bus
method calling `global.display.focus_window.move_frame(true, x, y)` directly.

```
$ gdbus call --session --dest org.gnome.Shell --object-path /org/gnome/Shell \
    --method org.gnome.Shell.Eval "1+1"
(false, '')
```

**Result: blocked by design.** `Eval` only executes when GNOME Shell is in
"unsafe mode", which can only be toggled interactively via the Looking Glass
console (`Alt+F2` → `lg` → `global.context.unsafe_mode = true`) and does not
persist across logout/restart. Unusable for an always-automatic fix — it would
require a manual toggle before every play session.

### Path 4 (chosen): A local GNOME Shell extension

Extensions run *inside* the Shell process with full Mutter/GNOME Shell API
access and are not subject to the `unsafe_mode` gate that blocks `Eval` — that
gate exists specifically to stop *external* processes from running arbitrary
JS in the Shell, not to stop the Shell's own loaded extensions.

## Decision

Installed a minimal local (unpublished, not distributed via
extensions.gnome.org) GNOME Shell extension:

```
~/.local/share/gnome-shell/extensions/wingman-window-left@wingman.local/metadata.json
~/.local/share/gnome-shell/extensions/wingman-window-left@wingman.local/extension.js
```

`metadata.json`:

```json
{
    "uuid": "wingman-window-left@wingman.local",
    "name": "Wingman Window Left",
    "description": "Snaps the MetalStorm Wine virtual desktop window to the top-left corner (0,0) the moment it is created, so screen capture alignment is consistent across launches.",
    "shell-version": ["46"],
    "version": 1
}
```

`extension.js`:

```js
import { Extension } from 'resource:///org/gnome/shell/extensions/extension.js';

const TARGET_WM_CLASS = 'steam_app_0';

export default class WingmanWindowLeftExtension extends Extension {
    enable() {
        this._windowCreatedId = global.display.connect('window-created', (_display, metaWindow) => {
            this._armMove(metaWindow);
        });
    }

    disable() {
        if (this._windowCreatedId) {
            global.display.disconnect(this._windowCreatedId);
            this._windowCreatedId = null;
        }
    }

    _armMove(metaWindow) {
        const wmClass = metaWindow.get_wm_class();
        if (wmClass !== TARGET_WM_CLASS)
            return;

        const handlerId = metaWindow.connect('first-frame', () => {
            metaWindow.disconnect(handlerId);
            metaWindow.move_frame(true, 0, 0);
        });
    }
}
```

It connects to `global.display`'s `window-created` signal, filters for the
Wine virtual desktop window by WM_CLASS (`steam_app_0` — matches the same
window `move_game_window.py` targets), waits for that specific window's
`first-frame` signal so the move happens only once the window is actually
mapped and rendering, then calls Mutter's own `move_frame(true, 0, 0)`. This
call originates from inside the Shell/compositor itself rather than an
external X11 client, so unlike Paths 1-2 there is no SubstructureRedirect
step for Mutter to ignore — but as noted below under Known Issue, that alone
has not made the result deterministic in practice.

### Loading the extension (one-time, per machine)

GNOME Shell only scans `~/.local/share/gnome-shell/extensions/` for new
extensions at Shell startup. Confirmed live: after creating the extension
directory, `gnome-shell`'s process (`ps -p <pid> -o lstart,etime`) was still
the same PID with 16+ hours of uptime — a lock/unlock or fast user switch does
not restart it. On Wayland, GNOME Shell cannot be restarted in place the way
`Alt+F2` → `r` does on X11 (that would tear down the compositor mid-session),
so a full logout (back to the GDM greeter) and login back in was required
before `gnome-extensions list` showed the extension at all. After that one-time
logout, it loads automatically on every subsequent login without any further
manual step.

## Known Issue — Placement Still Not Deterministic

Post-deployment observation: the extension does **not** reliably move the
window to (0, 0). Sometimes the window lands in the top-left corner as
intended; usually it does not, and Mutter's own variable placement (the
original problem from Context) still wins. This ADR is downgraded from
Accepted back to Draft until the flakiness is root-caused and fixed.

Leading hypotheses (not yet confirmed live):

- **Race with Mutter's own placement pass.** `first-frame` fires when the
  window's first frame is composited, but Mutter may still apply its own
  "constrain position" placement logic (centering / anti-overlap / initial
  placement heuristics) slightly after that point — e.g. on a subsequent
  `position-changed` or `size-changed` event as Wine's virtual desktop
  finishes settling into its final 1920x1200 size. If Mutter's placement
  pass runs after our `move_frame()` call, it silently overrides it. This
  would explain intermittent success: the move only sticks when it happens
  to land after Mutter's own placement finishes, not before.
- **Wrong window matched.** The WM_CLASS filter (`steam_app_0`) may match an
  earlier transient/placeholder window in Wine's startup sequence (recall
  `move_game_window.py`'s own detection logic distinguishes a `"Metalstorm"`
  titled window from a `"Wine Desktop"` + `steam_app_0` titled window as two
  *different* candidates) rather than the final game window whose position
  actually matters for capture alignment.

Next step before re-accepting this ADR: reproduce with GNOME Shell's own
logging (`journalctl --user -f _COMM=gnome-shell`) or temporary `log()` calls
inside `extension.js` to see whether `_armMove` fires once or multiple times
per launch, and whether `move_frame` is being called at all versus being
called and then overridden.

## Consequences

- MetalStorm's Wine virtual desktop window **sometimes** lands at (0, 0) on
  launch, matching `config.yaml`'s `region: {left: 0, top: 0, ...}` assumption
  (ADR 050) — but usually still exhibits Mutter's original variable placement.
  See Known Issue above; this is not yet a working fix.
- No changes to the Wingman repo itself were needed or made — `Makefile`,
  `wingman/move_game_window.py`, and the `wait-game` target are unchanged.
  Positioning now happens entirely at the compositor level, transparently to
  Wingman's launch flow.
- This fix is **local machine configuration, not part of the git repo**. The
  two files above live outside `wingman/` and are not tracked by git. If this
  machine is reimaged or the game is set up on another machine, the extension
  must be recreated manually from the source in this ADR.
- The extension depends on private/unversioned Shell and Mutter JS APIs
  (`Meta.Window.move_frame`, the `first-frame` signal) rather than a
  documented public GNOME API. It was verified only on GNOME Shell 46.0; a
  future GNOME upgrade could change or remove these without notice, silently
  breaking the extension (it would simply stop moving the window — no error
  surfaces to Wingman, since Wingman never depended on this for correctness in
  the first place per ADR 054).

## Related

- [ADR 050 — Wayland Screen Capture and Windowed Game Configuration](050-wayland-screen-capture.md)
- [ADR 053 — Linux One-Command Launch](053-linux-one-command-launch.md)
- [ADR 054 — GNOME Wayland Freeze on Wine Window Drag](054-gnome-wayland-freeze-on-wine-window-drag.md)
