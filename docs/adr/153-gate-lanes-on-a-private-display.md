# ADR 153 — Gate Lanes on a Private Display

| Status | Date       | Wingman Version |
|--------|------------|-----------------|
| Draft  | 2026-10-02 | 1.9.0           |

Supersedes two parts of [ADR 045](045-dual-lane-runtime-validation-replay-and-live-screen.md):
where its lanes run, and the missile-zero evidence its live lane requires.

## Decision

**D1. The ADR 044 replay lane and the ADR 045 live lane run on a private Xvfb display.**
`make rr-path1` and `make rr-live-path1` source `scripts/gate-display.sh`, which
starts Xvfb (1920x1200, the shipped capture region) on a free display number. It
sets `DISPLAY` to that display and `XDG_SESSION_TYPE=x11`, unsets
`WAYLAND_DISPLAY`, and stops the server when the recipe exits. The presenter,
capture, injection and hotkey observation all use that display. The live lane
therefore captures through the mss backend, not the PipeWire portal.

**D2. `WINGMAN_GATE_DISPLAY=real` keeps a lane on the session display.** Use it to
debug a lane against the real desktop on purpose. The `make newpaths` capture lane
is unchanged and still drives the session display, because the real game runs there.

**D3. The live lane's missile-zero evidence is pursuit, not eject-and-dive.** With
shipped config (`pursuit_mode.enabled`, `default_mission: su30`, and
[ADR 152](152-pursuit-resupply-priority.md) resupply actuation), the missiles-0
frame hands the aircraft to pursuit mode. The capture step waits for entry into
`GAME_BATTLE_EJECT`. `tests/runtime_live_validate.py` requires
`pursue_and_engage — tracking engaged` and a `PURSUIT SUMMARY` line, and it
requires a manual takeover to explain any external stop other than shutdown,
respawn or match end.

## Consequences

- No "Remote Desktop — Allow remote interaction" dialogs during `make tp` and
  `make tp-full`.
- The gates no longer take over the operator's screen, keyboard focus or mouse.
- Linux hosts need Xvfb. `scripts/upgrade-linux.sh` installs it, and a lane
  without it fails with an instruction to run `make upgrade-linux`.
- The live lane no longer exercises the PipeWire capture backend. That backend
  now serves only `NESTED=0` runs on Wayland. The nested lane, which is the
  production default ([ADR 099](099-nested-display-lane-for-unattended-operation.md)),
  captures with mss, which is the backend the gate now exercises.
  `tests/test_automated_levels.py` still constructs the PipeWire backend.
- The legacy missiles-empty eject dive is no longer covered by the live lane.
  Unit tests cover it, and it still runs when pursuit is disabled.

## Why

GNOME 50 (Ubuntu 26.04) starts the session Xwayland with `-enable-ei-portal`.
In that mode Xwayland sends XTest through libei and asks the RemoteDesktop
portal for permission once per X client connection. `wingman/input_linux.py`
opens a connection for every click, so a gate run raised one dialog per click.
Nothing is remembered: the portal permission store held no `remote-desktop`
entries after approvals. Ubuntu 24.04's GNOME 46 did not pass the flag, so XTest
on `:0` went straight to the X server.

The nested Xwayland (`scripts/nested-display.py`) is started without that flag,
so production runs never ask. The gate lanes were the only injectors left on `:0`.

Observed on VEDA, 2026-10-02:

```
/usr/bin/Xwayland :0 -rootless ... -byteswappedclients -enable-ei-portal
GNOME Shell 50.1, xwayland 2:24.1.10-1, xdg-desktop-portal-gnome 50.0
```

D3 fixes a gate that could not pass under shipped config. In the 2026-10-02 run,
the su30 mission saw the target icon on the missiles-0 frame and entered pursuit
without `fire_eject()`. The `missiles_empty` capture trigger therefore never
fired (`timeout_after_82.0s`). Pursuit with resupply actuation also never falls
through to `eject_and_dive` at zero ammo (`wingman/controller.py`).

## Validation

- [ ] `make rr-path1-gate` passes on VEDA with no portal dialog.
- [ ] `make rr-live-path1-gate` passes on VEDA with no portal dialog, and its log
      shows `Gate display: private Xvfb`.
- [ ] Both gates pass on a second Linux PC after `make upgrade-linux`.
