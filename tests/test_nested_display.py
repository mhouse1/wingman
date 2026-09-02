"""Nested display lane helper (scripts/nested-display.py, ADR 099).

The X access cannot be tested here — there is no nested display in CI — so what
is under test is the logic that decides *which* window to focus, the idempotence
that makes `start` safe as a Makefile prerequisite on every run, and the
refusal that keeps a misconfigured environment from producing a silently
useless server.
"""

import importlib.util
import sys
import unittest.mock as mock
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "nested_display", Path(__file__).parent.parent / "scripts" / "nested-display.py")
nd = importlib.util.module_from_spec(_spec)
sys.modules["nested_display"] = nd
_spec.loader.exec_module(nd)

SESSION = {4242, 4243}


class _FakeProp:
    def __init__(self, pid):
        self.value = [pid]


class _FakeGeom:
    def __init__(self, w, h):
        self.width, self.height = w, h


class _FakeWindow:
    """Minimal python-xlib window stand-in."""

    def __init__(self, wid, pid, w, h, name=None, raises=False):
        self.id = wid
        self._pid = pid
        self._geom = _FakeGeom(w, h)
        self._name = name
        self._raises = raises

    def get_full_property(self, *_a, **_k):
        if self._raises:
            raise RuntimeError("no property")
        return _FakeProp(self._pid) if self._pid is not None else None

    def get_geometry(self):
        return self._geom

    def get_wm_name(self):
        return self._name


def _display_with(windows):
    d = mock.MagicMock()
    d.intern_atom.return_value = 42
    d.screen.return_value.root.query_tree.return_value.children = windows
    return d


# --- which window gets the focus ---------------------------------------------

def test_the_virtual_desktop_wins_over_helper_windows():
    """Wine maps a 1x1 "Default IME" window and a small tool window alongside
    the virtual desktop. Focus must land on the desktop, not on a helper — the
    game reads keys from the desktop window."""
    ime = _FakeWindow(0x2A00002, 4242, 1, 1, "Default IME")
    tool = _FakeWindow(0x2A00001, 4242, 119, 34)
    desktop = _FakeWindow(0x400004, 4242, 1920, 1200, "Wine Desktop")
    got = nd._game_windows(_display_with([ime, tool, desktop]), SESSION)
    assert [w.id for w in got] == [0x400004, 0x2A00001, 0x2A00002]


def test_windows_outside_the_game_session_are_ignored():
    """ADR 098 D2: identity is the Wine session, never the title. A window
    titled like the game but owned by another process is not the game."""
    impostor = _FakeWindow(0x900001, 999999, 1920, 1200, "Wine Desktop")
    assert nd._game_windows(_display_with([impostor]), SESSION) == []


def test_a_window_with_no_pid_property_is_ignored():
    assert nd._game_windows(_display_with([_FakeWindow(0x1, None, 800, 600)]), SESSION) == []


def test_a_window_that_raises_does_not_take_the_scan_down():
    good = _FakeWindow(0x400004, 4243, 1920, 1200, "Wine Desktop")
    bad = _FakeWindow(0x2, 4242, 100, 100, raises=True)
    assert [w.id for w in nd._game_windows(_display_with([bad, good]), SESSION)] == [0x400004]


def test_an_unreadable_window_tree_returns_empty_rather_than_raising():
    d = mock.MagicMock()
    d.intern_atom.return_value = 42
    d.screen.return_value.root.query_tree.side_effect = RuntimeError("no tree")
    assert nd._game_windows(d, SESSION) == []


# --- start is idempotent, and refuses the one env that cannot work ------------

def test_start_leaves_a_running_display_alone():
    """Safe as a Makefile prerequisite: a second `make rd` must not spawn a
    second server on the same display."""
    with mock.patch.object(nd, "display_is_up", return_value=True), \
         mock.patch("subprocess.Popen") as popen:
        assert nd.start(":3", "1920x1200") == 0
    popen.assert_not_called()


def test_start_refuses_when_there_is_no_host_compositor():
    """Xwayland is itself a Wayland client. Under the game's own env — which
    strips WAYLAND_DISPLAY so Wine cannot pick winewayland.drv and bypass the
    nested display — it has nothing to attach to. Better to say so than to
    spawn a server that never comes up."""
    with mock.patch.object(nd, "display_is_up", return_value=False), \
         mock.patch.dict("os.environ", {}, clear=True), \
         mock.patch("subprocess.Popen") as popen:
        assert nd.start(":3", "1920x1200") == 1
    popen.assert_not_called()


def test_start_reports_failure_when_the_server_never_answers():
    with mock.patch.object(nd, "display_is_up", return_value=False), \
         mock.patch.dict("os.environ", {"WAYLAND_DISPLAY": "wayland-0"}), \
         mock.patch("subprocess.Popen"), \
         mock.patch("time.sleep"):
        assert nd.start(":3", "1920x1200") == 1


def test_start_survives_xwayland_missing_from_path():
    with mock.patch.object(nd, "display_is_up", return_value=False), \
         mock.patch.dict("os.environ", {"WAYLAND_DISPLAY": "wayland-0"}), \
         mock.patch("subprocess.Popen", side_effect=FileNotFoundError):
        assert nd.start(":3", "1920x1200") == 1


# --- focus reports rather than hangs -----------------------------------------

def test_focus_fails_cleanly_when_the_display_is_down():
    with mock.patch.object(nd, "display_is_up", return_value=False):
        assert nd.focus(":3", timeout=0.0) == 1


# --- config is the source of truth (ADR 099) ---------------------------------

def _cfg(tmp_path, body):
    p = tmp_path / "config.yaml"
    p.write_text(body)
    return str(p)


def test_config_drives_the_lane(tmp_path):
    with mock.patch.object(nd, "CONFIG_PATH",
                           _cfg(tmp_path, "nested:\n  enabled: true\n  display: ':7'\n  size: '800x600'\n")):
        nc = nd.load_nested_config()
    assert nc == {"enabled": True, "display": ":7", "size": "800x600"}


def test_override_forces_the_lane_off():
    """`make rd NESTED=0` must escape a global config flag — otherwise two
    simultaneous accounts are forced into the same lane."""
    with mock.patch.object(nd, "load_nested_config", nd.load_nested_config):
        with mock.patch("builtins.open", mock.mock_open(
                read_data="nested:\n  enabled: true\n")):
            assert nd.load_nested_config("0")["enabled"] is False
            assert nd.load_nested_config("1")["enabled"] is True
            # An empty override (make invoked without NESTED=) keeps config.
            assert nd.load_nested_config("")["enabled"] is True


def test_a_missing_or_broken_config_disables_the_lane(tmp_path):
    """Failing closed matters: a half-applied lane would capture the nested
    display while injecting into the operator's, which is the ADR 098
    corruption reintroduced."""
    with mock.patch.object(nd, "CONFIG_PATH", str(tmp_path / "nope.yaml")):
        assert nd.load_nested_config()["enabled"] is False
    with mock.patch.object(nd, "CONFIG_PATH", _cfg(tmp_path, "{{{ not yaml")):
        assert nd.load_nested_config()["enabled"] is False


# --- the Makefile contract ---------------------------------------------------

def test_env_prints_nothing_when_the_lane_is_off(capsys):
    assert nd.cmd_env({"enabled": False, "display": ":3", "size": "x"}) == 0
    assert capsys.readouterr().out == ""


def test_env_strips_wayland_display_so_wine_cannot_bypass_the_lane(capsys):
    assert nd.cmd_env({"enabled": True, "display": ":3", "size": "x"}) == 0
    out = capsys.readouterr().out.strip()
    assert out == "env -u WAYLAND_DISPLAY DISPLAY=:3"


def test_setup_is_a_no_op_when_the_lane_is_off():
    with mock.patch.object(nd, "start") as start:
        assert nd.cmd_setup({"enabled": False, "display": ":3", "size": "x"}) == 0
    start.assert_not_called()


# --- ADR 104 rev 2: placement is chosen from the host, not hardcoded ----------

def test_a_desktop_larger_than_the_framebuffer_stays_windowed():
    """VEDA: a 3840x1600 desktop has room to put the nested window beside the
    operator's work. Going fullscreen there covers the screen they are using,
    which is the thing ADR 099 exists to prevent."""
    assert nd.should_fullscreen("1920x1200", (3840, 1600)) is False


def test_a_panel_no_bigger_than_the_framebuffer_goes_fullscreen():
    """Impulse: a 1920x1080 laptop panel has no room. Left to the compositor's
    default placement the window floated and was visibly cut off (2026-09-01)."""
    assert nd.should_fullscreen("1920x1200", (1920, 1080)) is True


def test_an_exact_match_goes_fullscreen():
    assert nd.should_fullscreen("1920x1200", (1920, 1200)) is True


def test_a_host_wider_but_shorter_is_not_fullscreen():
    """Both axes must fit. An ultrawide that is shorter than the framebuffer
    still has horizontal room for a window."""
    assert nd.should_fullscreen("1920x1200", (3440, 1080)) is False


def test_an_unknown_host_never_goes_fullscreen():
    """The costs are asymmetric: a missed fullscreen is a mispositioned window
    on a machine nobody is watching; a wrong fullscreen takes over the screen of
    someone who is working."""
    assert nd.should_fullscreen("1920x1200", None) is False


def test_an_unparseable_geometry_never_goes_fullscreen():
    for size in ("", "banana", "1920", "1920x", "1920X1200"):
        assert nd.should_fullscreen(size, (800, 600)) is False, size


def test_the_host_size_comes_from_a_connected_output(tmp_path):
    """sysfs rather than X: this runs from a make recipe that has no XAUTHORITY,
    where querying the operator's display fails outright."""
    for name, status, modes in (("card1-DP-1", "connected", "3840x1600\n2560x1440\n"),
                                ("card1-HDMI-A-1", "disconnected", "1920x1080\n")):
        d = tmp_path / name
        d.mkdir()
        (d / "status").write_text(status)
        (d / "modes").write_text(modes)
    with mock.patch.object(nd, "glob") as g:
        g.glob.return_value = [str(tmp_path / "card1-DP-1" / "modes"),
                               str(tmp_path / "card1-HDMI-A-1" / "modes")]
        assert nd.host_output_size() == (3840, 1600)


def test_the_largest_connected_output_wins(tmp_path):
    """A docked laptop: the question is whether ANY screen has room, and the
    external monitor is the one that does."""
    for name, modes in (("card1-eDP-1", "1920x1080\n"), ("card1-DP-1", "3840x2160\n")):
        d = tmp_path / name
        d.mkdir()
        (d / "status").write_text("connected")
        (d / "modes").write_text(modes)
    with mock.patch.object(nd, "glob") as g:
        g.glob.return_value = [str(tmp_path / n / "modes")
                               for n in ("card1-eDP-1", "card1-DP-1")]
        assert nd.host_output_size() == (3840, 2160)


def test_unreadable_sysfs_reports_unknown_rather_than_raising():
    with mock.patch.object(nd, "glob") as g:
        g.glob.return_value = ["/nonexistent/card9-DP-9/modes"]
        assert nd.host_output_size() is None


# --- the capture lane must launch the game where it is watching ---------------

def _makefile():
    from pathlib import Path
    return Path(__file__).parent.parent.joinpath("Makefile").read_text()


def test_the_capture_lane_launches_the_game_on_the_real_display():
    """wingman.main disables the nested lane for --capture-path-config, because
    that lane grabs the monitor through PipeWire. The Makefile has to agree:
    launching the game into :3 while wingman looks at :0 produced

        PipeWireBackend: game window not found in 3840x1600 frame

    on every cycle of a whole run (2026-09-02), with all nine PATH1 screenshots
    left uncaptured. NESTED=0 makes nested-setup and nested-focus no-ops and
    empties NESTED_ENV, so all four launch deps agree with wingman."""
    mk = _makefile()
    assert "newpaths: NESTED := 0" in mk, \
        "the capture lane must pin the game to the real display"
    body = mk.split("newpaths: NESTED := 0")[1]
    assert body.lstrip().startswith("newpaths: $(GAME_LAUNCH_DEPS)"), \
        "the override must sit immediately before the target it applies to"


def test_the_nested_lane_is_still_the_default_for_ordinary_runs():
    """Only the real-screen lane opts out. `make r` / `make rd` keep the ADR 099
    default, which is what lets the operator use the machine."""
    mk = _makefile()
    for target in ("r:", "rd:", "g:"):
        line = next(ln for ln in mk.splitlines() if ln.startswith(target))
        assert "GAME_LAUNCH_DEPS" in line
    assert "r: NESTED" not in mk and "rd: NESTED" not in mk
