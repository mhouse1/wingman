# ADR 154 — Ubuntu Upgrade Path: PyGObject in the Venv and `make upgrade-linux`

| Status | Date       | Wingman Version |
|--------|------------|-----------------|
| Draft  | 2026-10-02 | 1.9.0           |

Retires the `system_gi_bridge.pth` approach that
[Research 007](../research/007-pycharm-ide-fit.md) documents and that
`scripts/setup-linux.sh` Step 5 used to write.

## Decision

**D1. PyGObject is a locked, Linux-only project dependency.** `pyproject.toml`
declares `pygobject>=3.58.0 ; sys_platform == 'linux'`, and `uv.lock` pins
PyGObject 3.58.0 and pycairo 1.29.1. PyPI ships both only as sdists, so `uv sync`
compiles them into the venv. The venv no longer borrows apt's `python3-gi`.

**D2. The build and runtime prerequisites come from apt.** These are
`build-essential`, `pkg-config`, `libgirepository-2.0-dev` (PyGObject needs
GLib 2.80 or later, so Ubuntu 24.04 is the minimum), `libcairo2-dev` and
`gir1.2-gstreamer-1.0`. `xvfb` is also installed, for the gate lanes of
[ADR 153](153-gate-lanes-on-a-private-display.md). The Dockerfile and
`.github/workflows/release.yml` install the same build headers, because both run
`uv sync`.

**D3. `scripts/upgrade-linux.sh` (`make upgrade-linux`) is the one way to bring a
Linux host's environment up to date.** Run it once on every Linux PC after
pulling this change, and again after every Ubuntu release upgrade. It is
idempotent. It:

1. recreates `.venv` on a uv-managed Python if its interpreter no longer runs.
   A venv still on the system Python gets a warning, and `--rebuild-venv` moves it;
2. apt-installs only the missing D2 packages, adding `python3.X-dev` when the
   venv's Python has no headers;
3. deletes `system_gi_bridge.pth`;
4. runs `uv sync --all-groups`;
5. fails unless `gi` and `Gst` import from inside the venv, and warns if tkinter
   doesn't import;
6. on GNOME, installs or refreshes the window-left Shell extension and declares
   the running Shell version in it ([ADR 155](155-window-left-extension-for-the-nested-display.md)).

`scripts/setup-linux.sh` Step 5 calls it, so fresh installs and upgrades share
one path.

**D4. Prefer a venv on a uv-managed Python.** It ships its own headers and
tkinter, and an OS Python upgrade cannot remove it. This replaces the CLAUDE.md
rule that the venv sits on the system Python.

## Consequences

- After pulling, every `uv run` (and so every `make` target) re-syncs and tries
  to compile PyGObject. Until `make upgrade-linux` has installed the headers,
  that build fails. Run the script before anything else.
- Windows hosts are unaffected, because of the platform marker.
- A first sync compiles PyGObject and pycairo, which takes about a minute
  longer, once.
- The system Python's `dist-packages` is no longer on the venv's import path, so
  a module missing from the venv can no longer resolve to a build for another
  Python.
- `make rd NESTED=0` on Wayland works again.

## Other changes the same upgrade forced

The Python environment was one of five things Ubuntu 26.04 broke on VEDA. The
others have their own records:

- [ADR 153](153-gate-lanes-on-a-private-display.md): GNOME 50 raises a portal
  dialog for every XTest client on the session display, so the gate lanes run
  on a private Xvfb.
- [ADR 155](155-window-left-extension-for-the-nested-display.md): the
  window-left Shell extension went OUT OF DATE and never matched the nested
  window.
- [ADR 156](156-detach-the-host-pointer-while-a-click-is-sent.md): Xwayland
  24.1.10 delivers wingman's clicks on the nested display only while the
  operator's mouse is over its window.
- [ADR 157](157-release-modifiers-left-held-on-the-nested-display.md): the same
  Xwayland leaves Alt held on the nested display when the operator Alt+Tabs
  away, and the game then reads Enter as Alt+Enter and frames its window.

## Why

VEDA moved to Ubuntu 26.04, whose system Python is 3.14. The venv runs on
uv-managed CPython 3.12.13 (`.python-version`). The bridge appended
`/usr/lib/python3/dist-packages` to the venv's path, which only works when the
two minor versions match. That directory now ships only
`gi/_gi.cpython-314-x86_64-linux-gnu.so`, so `import gi` failed in the 3.12 venv:

```
ImportError: cannot import name '_gi' from partially initialized module 'gi'
(most likely due to a circular import) (/usr/lib/python3/dist-packages/gi/__init__.py)
```

Two tests failed: `tests/test_automated_levels.py::test_level2_live_capture` and
`::test_get_frame_and_analyze_frame`. The PipeWire capture backend (used on
Wayland without the nested display) was broken in the same way.

Alternatives rejected:

- **Rebuild the venv on system Python 3.14.** This would move the CUDA torch
  stack to cp314 wheels, give up the uv-managed interpreter (chosen for tkinter
  and to survive snap refreshes), and break again at the next Ubuntu Python bump.
- **Skip the two tests when gi is missing.** That hides a real breakage of the
  `NESTED=0` capture path.

## Validation

- [x] VEDA, 2026-10-02, after `make upgrade-linux`: `system_gi_bridge.pth` is gone,
      and `gi` 3.58.0 imports from `.venv/lib/python3.12/site-packages/gi/`
      with GStreamer 1.28.2 and pycairo 1.29.1.
- [x] VEDA, 2026-10-02 03:06:45, `rr-live-path1` run: `PipeWireBackend: pipeline
      running` (the backend that failed to import before).
- [ ] VEDA: `make upgrade-linux` re-run installs `xvfb` (added after the first run).
- [x] VEDA, 2026-10-02: `make test` 2516 passed, 35 skipped. `test_level2_live_capture`
      and `test_get_frame_and_analyze_frame` now get past `import gi` and construct
      the PipeWire capture, then skip with "MetalStorm not running".
- [ ] VEDA: the same two tests pass with MetalStorm running.
- [ ] A second Linux PC: `make upgrade-linux` succeeds and `make test` passes.
- [ ] `make docker-test` builds the image with the new build headers.
- [ ] The release workflow's `uv sync --no-group dev` succeeds on `ubuntu-latest`.
