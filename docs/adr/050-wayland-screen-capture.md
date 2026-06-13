# ADR 050 — Wayland Screen Capture and Windowed Game Configuration

| Status   | Date       | Wingman Version |
|----------|------------|-----------------|
| Accepted | 2026-06-13 | 1.6.19          |

## Context

Wingman's screen capture layer (`wingman/capture.py`) uses `mss`, which calls
`XGetImage` on the X11 root window. On a Wayland desktop session — even with XWayland
active — Wayland compositors (including GNOME's Mutter) block `XGetImage` on the root
window as a security measure. Every capture attempt silently returns `None`, causing the
main loop to emit continuous warnings:

```
[WARNING] Frame capture failed (monitor disconnected or region out of bounds) — skipping cycle
```

The failure was confirmed on Ubuntu 24.04.4 LTS (GNOME 46 Wayland, `XDG_SESSION_TYPE=wayland`).
`mss` works correctly on Windows and on Linux X11 sessions; Wayland is the only broken path.

### Windowed Mode Context

Wingman captures a fixed screen region (`region` in `config.yaml`). For capture
coordinates to align with the game, MetalStorm must occupy a known position on screen.
To make the game position predictable, Wine's virtual desktop mode was enabled by
writing registry keys directly into the Proton Wine prefix:

```
[Software\Wine\Explorer]
"Desktop"="Default"

[Software\Wine\Explorer\Desktops]
"Default"="1920x1200"
```

This causes MetalStorm to open inside a 1920×1200 Wine virtual desktop **XWayland window**
positioned at the top-left of the display, matching `config.yaml`'s
`region: {left: 0, top: 0, width: 1920, height: 1200}`. The registry change was applied to:

```
/home/michael/Games/Heroic/Prefixes/Metalstorm/user.reg
```

The `pfx/user.reg` path is a hardlink to the same inode.

---

## Investigation — Approaches Evaluated

Four Wayland capture paths were investigated before settling on the final approach.

### Path 1: GNOME Shell D-Bus (`org.gnome.Shell.Screenshot.ScreenshotArea`)

GNOME Shell exposes `org.gnome.Shell.Screenshot` on the session bus. In theory,
`ScreenshotArea(x, y, w, h, flash, filename)` captures an arbitrary screen region.

**Result: Blocked.** On GNOME 42+, the method returns:

```
AccessDenied: ScreenshotArea is not allowed
```

GNOME 42 removed unmediated screenshot access for non-trusted callers. This API is only
accessible from processes that GNOME Shell has explicitly whitelisted.

### Path 2: Mutter ScreenCast + PipeWire (`org.gnome.Mutter.ScreenCast`)

Mutter exposes `CreateSession` → `RecordArea` → `Start` → `PipeWireStreamAdded(node_id)`.
`CreateSession` and `RecordArea` succeed without a dialog, and `PipeWireStreamAdded`
fires with a valid PipeWire node ID.

**Result: Blocked at PipeWire link.** GStreamer's `pipewiresrc target-object={node_id}`
connects to PipeWire, reaches `PAUSED`, then fails with:

```
stream error: target not found
```

The ScreenCast node (`media.class: Stream/Output/Video`, `node.driver: true`) does not
allow arbitrary PipeWire clients to link to it. Access is mediated by the portal daemon;
direct linkage from external clients is rejected by Mutter's PipeWire access policy.

### Path 3: xdg-desktop-portal Screenshot (`org.freedesktop.portal.Screenshot`)

**Result: Interactive dialog, no unattended path.** The portal returns `response=2`
(no interactive response) without showing a dialog when called with `interactive=false`
and no stored permission. Even with `interactive=true`, the response arrives
immediately with code 2, suggesting the portal backend silently rejects requests
from non-Flatpak processes without a stored permission entry.

### Path 4: xdg-desktop-portal ScreenCast + OpenPipeWireRemote

The ScreenCast portal (version 5 on GNOME 46) supports `restore_token` for
permission persistence:

1. `CreateSession` → `SelectSources` (with `restore_token`) → `Start` → node_id
2. `OpenPipeWireRemote` (with `enable_fds=True` in jeepney) → PipeWire fd
3. `pipewiresrc fd={fd} target-object={node_id}`

**Result: Portal flow works; PipeWire linkage fails.** `CreateSession` through `Start`
succeeds on first run (with a monitor-selection dialog), saves a `restore_token`, and
subsequent runs bypass the dialog entirely. `OpenPipeWireRemote` returns a valid Unix fd.
However, `pipewiresrc fd={fd} target-object={node_id}` still fails with
`stream error: target not found` — the same error as Path 2. On PipeWire/GStreamer 1.0.5
(Ubuntu 24.04), the node ID from the portal's `Start` response is a global PipeWire ID
that is not linkable via `pipewiresrc` through the portal's restricted fd.

### Path 5: XWayland window capture via `xwd`

MetalStorm's Wine virtual desktop is an **XWayland window** — a real X11 window created
by Wine and composited by Mutter's XWayland bridge. It appears in `xwininfo -root -children`
and is capturable with `xwd -id {window_id} -silent`.

**Result: Works.** Benchmarked at ~38 ms per frame. No dialog, no portal, no PipeWire.
The XAUTHORITY cookie for `:0` (XWayland) is set by GNOME in the environment
(`/run/user/{uid}/.mutter-Xwaylandauth.*`), which Wingman inherits.

XWD captures the window content (not the root window), so Mutter's root-window block
does not apply. The captured frame is decoded from XWD binary format directly in Python
using `struct` — no external decode step.

---

## Decision

Replace `mss` in `capture.py` with a **platform-dispatched capture backend**:

- **Windows / Linux X11**: `_MssBackend` (unchanged, no regression).
- **Linux Wayland**: `_XwdBackend` — subprocess `xwd` on the Wine XWayland window.

The platform is detected at `Capture.__init__` time using `sys.platform` and
`$XDG_SESSION_TYPE`. The public interface (`get_frame() → np.ndarray | None`,
`grab_from_thread() → np.ndarray | None`) is identical across backends — no callers change.

### Why `xwd` Over Other Approaches

| Approach | Status | Reason eliminated |
|---|---|---|
| GNOME Shell ScreenshotArea D-Bus | Blocked | AccessDenied on GNOME 42+ |
| Mutter ScreenCast + pipewiresrc | Blocked | PipeWire access policy blocks link |
| xdg-desktop-portal Screenshot | Interactive | No unattended path |
| xdg-desktop-portal ScreenCast + OpenPipeWireRemote | Blocked | pipewiresrc target not found (PipeWire 1.0.5) |
| `xwd` on XWayland Wine window | **Works** | 38 ms, no dialog, standard tool |

`xwd` captures a specific XWayland window (not the root), which bypasses Mutter's
root-capture security restriction. The Wine virtual desktop window is identified
by its exact geometry (1920×1200) in `xwininfo -root -children` output.

---

## Implementation

### `capture.py` — `_XwdBackend`

```python
class _XwdBackend:
    def __init__(self, region):
        self._region = region
        self._display = os.environ.get("DISPLAY", ":0")
        self._xauthority = self._find_xauthority()
        self._window_id = None  # lazily resolved on first capture

    def _find_xauthority(self) -> str | None:
        xauth = os.environ.get("XAUTHORITY", "")
        if xauth and os.path.exists(xauth):
            return xauth
        import glob
        files = glob.glob(f"/run/user/{os.getuid()}/.mutter-Xwaylandauth.*")
        return files[0] if files else None

    def _find_wine_window(self) -> str | None:
        result = subprocess.run(["xwininfo", "-root", "-children"], ...)
        # Match by exact geometry (width x height) from config region
        ...

    def _capture(self) -> np.ndarray | None:
        result = subprocess.run(["xwd", "-id", self._window_id, "-silent"], ...)
        return self._parse_xwd(result.stdout)

    @staticmethod
    def _parse_xwd(data: bytes) -> np.ndarray | None:
        # Decode XWD ZPixmap header (big-endian struct) and return BGR ndarray
        ...
```

XAUTHORITY discovery order:

1. `$XAUTHORITY` env var (set by GNOME for Wayland sessions — present in any terminal).
2. Glob `/run/user/{uid}/.mutter-Xwaylandauth.*` (fallback if env var missing).

Window resolution order:

1. First XWayland window matching `config.yaml` region dimensions (1920×1200).
2. First window with "Wine Desktop" in its name.
3. Return `None` if no match (game not running); capture returns `None` and the
   main loop skips the tick gracefully.

The `_window_id` is cached after first discovery. If a subsequent capture fails
(window closed), it resets to `None` so discovery re-runs on the next tick.

### XWD Frame Decoding

XWD (X Window Dump) uses a fixed binary header followed by raw pixel data:

```
Offset  Size  Field
     0     4  header_size  (big-endian uint32)
    16     4  width
    20     4  height
    44     4  bits_per_pixel
    48     4  bytes_per_line
    72     4  ncolors
```

Pixel data starts at `header_size + ncolors * 12`. For 32-bit ZPixmap (BGRX), reshape
to `(height, width, 4)` and drop the alpha channel. For 24-bit, reshape to
`(height, width, 3)`. No external tools required — pure Python `struct` + `numpy`.

### External Dependencies (Linux/Wayland)

| Tool | Package | Purpose |
|---|---|---|
| `xwd` | `x11-apps` | Window capture |
| `xwininfo` | `x11-utils` | Window enumeration |

Both packages are present by default on Ubuntu Desktop. `make preflight` checks for both
and reports `FAIL` with install instructions if missing.

### `preflight.py` Changes

Two new Linux/Wayland-only checks replace the previous `jeepney` check:

```
[PASS] xwd            found  (XWayland capture)
[PASS] xwininfo       found  (XWayland window discovery)
```

### `pyproject.toml` Changes

`jeepney` and `pipewire-python` removed — no longer required.

---

## Windowed Mode — Operational Notes

Wine virtual desktop mode is configured per-prefix and persists across launches.
If the prefix is deleted or recreated, the registry keys must be re-applied.
`scripts/setup-linux.sh` applies these keys automatically as part of setup.

`config.yaml` region coordinates are interpreted as:

- `_MssBackend`: offset from the selected monitor's top-left.
- `_XwdBackend`: absolute coordinates within the captured XWayland window (which starts
  at (0,0) within the window frame), matching the Wine virtual desktop resolution.

With the Wine virtual desktop at 1920×1200 and `config.yaml` region `{left: 0, top: 0,
width: 1920, height: 1200}`, no configuration changes are needed on Linux.

---

## Scope

In scope:

- Rewrite `capture.py` with `_MssBackend` and `_XwdBackend` classes.
- Fix the inline `mss` capture in `main.py:_click_ready_after_invite` — replaced
  with `cap.grab_from_thread()`.
- Fix three daemon-thread `mss` calls in `controller.py` — replaced with
  `grab_from_thread()`.
- Update `preflight.py` to check for `xwd` and `xwininfo` on Linux/Wayland.
- Update `scripts/setup-linux.sh` to apply Wine virtual desktop registry keys.

Out of scope:

- KDE / non-GNOME Wayland compositors (deferred).
- PipeWire portal capture (investigated; blocked on PipeWire 1.0.5 + Ubuntu 24.04).
- Wayland input injection (separate concern; `keyboard` library handles key injection
  via `/dev/input/event*`).

---

## Acceptance Criteria

- `make r` on Linux/Wayland (GNOME 46, Ubuntu 24.04) captures frames without warnings.
- `make r` on Windows behaves identically to pre-ADR behaviour.
- `make r` on Linux/X11 uses `mss` (no regression).
- `make test` passes unchanged on all platforms.
- `make preflight` on Linux/Wayland reports `xwd` and `xwininfo` PASS, exits 0.

---

## Consequences

Positive:

- Wingman runs on a standard GNOME Wayland desktop without requiring session changes.
- Windows path is completely unchanged.
- Capture backend is extensible — additional backends can be added via the same
  `get_frame() / grab_from_thread()` interface.
- 38 ms per frame is comfortably within the 1.5-second tick budget.
- No dialog or permission grant required for unattended operation.

Trade-offs:

- Capture requires the game to be running in a Wine virtual desktop window at the
  configured dimensions. If the window is not found, the tick is skipped gracefully.
- `xwd` / `xwininfo` must be installed (`x11-apps`, `x11-utils`). Pre-installed on
  Ubuntu Desktop.
- Subprocess overhead (~38 ms) vs `mss` (~5–10 ms). Acceptable for the 1.5-second tick.

## Alternatives Considered

1. **Force X11 login session.**
   Rejected: breaks normal desktop workflow; unacceptable friction.

2. **GNOME Shell D-Bus ScreenshotArea.**
   Blocked on GNOME 42+ (AccessDenied). Documented above.

3. **Mutter ScreenCast + PipeWire.**
   Blocked by PipeWire access policy. Documented above.

4. **xdg-desktop-portal ScreenCast + OpenPipeWireRemote.**
   Portal flow works, but `pipewiresrc` target-not-found on PipeWire/GStreamer 1.0.5.
   May work on newer versions; revisit if Ubuntu upgrades to PipeWire 1.2+.

5. **XComposite window capture via `python-xlib`.**
   python-xlib 0.15 (venv version) does not correctly read `XAUTHORITY` from the
   environment. System `python3-xlib` is not installed. `xwd` achieves the same
   result with less complexity.

## References

- `wingman/capture.py` — `_XwdBackend` implementation
- `wingman/config.yaml` — `region` and `monitor` fields
- `scripts/setup-linux.sh` — Wine virtual desktop registry setup
- ADR 049 — Linux migration: game launcher and automation layer
- ADR 047 — Host environment pre-flight check
- Wine virtual desktop: `HKCU\Software\Wine\Explorer` registry key
- XWD file format: `man xwd(1)`
