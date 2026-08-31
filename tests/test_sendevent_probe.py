"""Send-event probe decision logic (scripts/sendevent-probe.py).

The probe asks whether wingman can deliver keys to the game while another window
has focus — the capability ADR 098's guard suppresses rather than provides. Its X
access cannot be tested here (there is no display in CI), so what is under test
is the part that decides *which* windows to address and *what the run proved*,
plus the promise that a malformed window never takes the walk down.
"""

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

_spec = importlib.util.spec_from_file_location(
    "sendevent_probe", Path(__file__).parent.parent / "scripts" / "sendevent-probe.py")
sp = importlib.util.module_from_spec(_spec)
# @dataclass resolves its own module via sys.modules[cls.__module__]; a
# file-loaded module must be registered before exec or the decorator raises.
sys.modules["sendevent_probe"] = sp
_spec.loader.exec_module(sp)

GAME_PID = 3241663          # Metalstorm.exe
DESKTOP_PID = 3241639       # explorer.exe — owns the "Wine Desktop" window
EDITOR_PID = 1122514

SESSION = {GAME_PID, DESKTOP_PID}


# --- fake X, just enough of it ----------------------------------------------

class FakeWin:
    def __init__(self, wid, props=None, children=(), viewable=True, broken=False):
        self.id = wid
        self.props = props or {}
        self.children = list(children)
        self.viewable = viewable
        self.broken = broken

    def get_full_property(self, atom, _type):
        if self.broken:
            raise RuntimeError("window vanished")
        if atom not in self.props:
            return None
        return SimpleNamespace(value=self.props[atom])

    def get_attributes(self):
        if self.broken:
            raise RuntimeError("window vanished")
        return SimpleNamespace(map_state=2 if self.viewable else 0)

    def get_geometry(self):
        return SimpleNamespace(width=1920, height=1080)

    def query_tree(self):
        if self.broken:
            raise RuntimeError("window vanished")
        return SimpleNamespace(children=self.children)


def make_prober(root_children):
    """A _Prober with its X connection replaced — __init__ would need a display."""
    p = object.__new__(sp._Prober)
    p._X = SimpleNamespace(IsViewable=2, NONE=0)
    p._root = FakeWin(1, children=root_children)
    p._d = SimpleNamespace(get_atom=lambda name, only_if_exists=True: name)
    p._display_name = ":0"
    return p


def win(wid, pid=None, title=None, cls=None, children=(), viewable=True, broken=False):
    props = {}
    if pid is not None:
        props["_NET_WM_PID"] = [pid]
    if title is not None:
        props["_NET_WM_NAME"] = title
    if cls is not None:
        props["WM_CLASS"] = cls
    return FakeWin(wid, props, children, viewable, broken)


# --- which windows are candidates -------------------------------------------

def test_the_wine_desktop_window_is_a_candidate():
    """The managed window belongs to explorer.exe, a sibling of the game under
    the Proton launcher — ADR 098 trap 3. Matching only the game binary finds
    nothing to address."""
    p = make_prober([win(0x200, pid=DESKTOP_PID, title="Wine Desktop", cls="explorer.exe")])
    cands = p.candidates(SESSION)
    assert [c.wid for c in cands] == [0x200]
    assert cands[0].pid == DESKTOP_PID


def test_children_inherit_the_owning_pid():
    """_NET_WM_PID is set on the managed top-level and not on its children.
    Without inheritance the probe finds one candidate — the frame — and never
    tests the window underneath, which is where Wine may actually read keys."""
    child = win(0x201, title=None)
    p = make_prober([win(0x200, pid=DESKTOP_PID, title="Wine Desktop", children=[child])])
    cands = p.candidates(SESSION)
    assert [c.wid for c in cands] == [0x200, 0x201]
    assert cands[1].pid == DESKTOP_PID


def test_another_applications_windows_are_never_candidates():
    """Sending the padlock key into the operator's editor is the failure the
    whole exercise exists to avoid."""
    p = make_prober([
        win(0x300, pid=EDITOR_PID, title="wingman - Visual Studio Code"),
        win(0x200, pid=DESKTOP_PID, title="Wine Desktop"),
    ])
    assert [c.wid for c in p.candidates(SESSION)] == [0x200]


def test_a_window_titled_like_the_game_is_not_a_candidate():
    """Verified 2026-08-28: a VS Code window titled "Metalstorm config GitHub…"
    satisfied a substring test. Ownership decides, never the title."""
    p = make_prober([win(0x300, pid=EDITOR_PID, title="Metalstorm config - wingman")])
    assert p.candidates(SESSION) == []


def test_shallowest_and_mapped_windows_are_tested_first():
    """Burst order is the operator's time. The managed top-level is the likeliest
    target and should be tried before deep or unmapped leaves."""
    deep = win(0x203, children=[])
    unmapped = win(0x202, viewable=False)
    top = win(0x200, pid=DESKTOP_PID, children=[win(0x201, children=[deep]), unmapped])
    cands = make_prober([top]).candidates(SESSION)
    assert [c.wid for c in cands] == [0x200, 0x201, 0x202, 0x203]


def test_a_broken_window_does_not_end_the_walk():
    """Windows die mid-enumeration. One that vanishes must cost its own subtree,
    not the sibling that is the real target."""
    p = make_prober([
        win(0x100, broken=True),
        win(0x200, pid=DESKTOP_PID, title="Wine Desktop"),
    ])
    assert [c.wid for c in p.candidates(SESSION)] == [0x200]


def test_the_walk_stops_at_the_depth_cap():
    leaf = win(0x210)
    node = leaf
    for wid in range(0x209, 0x200, -1):
        node = win(wid, children=[node])
    p = make_prober([win(0x200, pid=DESKTOP_PID, children=[node])])
    assert max(c.depth for c in p.candidates(SESSION, max_depth=3)) == 3


# --- what the run proved -----------------------------------------------------

def burst(index, seen=None, focus=sp.FOCUS_OTHER, mech=sp.MECH_SEND_EVENT, error=None):
    c = sp.Candidate(wid=0x200 + index, pid=DESKTOP_PID, title="Wine Desktop",
                     wm_class="explorer.exe", depth=1, mapped=True)
    return sp.Burst(index=index, candidate=c, mechanism=mech, count=3,
                    focus_state=focus, error=error, seen=seen)


def test_a_failed_self_test_proves_nothing_about_wine():
    """If the probe cannot deliver a key to its own window, a negative game
    result is an artefact of the probe, not a fact about Wine."""
    out = sp.summarize([burst(1, seen=False)], (False, "delivery is broken"))
    assert "FAIL" in out
    assert "proves nothing about Wine" in out
    assert "dead end" not in out


def test_bursts_sent_while_the_game_held_focus_prove_nothing():
    """The operator forgot to alt-tab. XTest would have worked too, so a landing
    here says nothing about addressing an unfocused window."""
    out = sp.summarize([burst(1, seen=True, focus=sp.FOCUS_GAME)], (True, "ok"))
    assert "the question was never" in out
    assert "REACHED" not in out


def test_a_landing_while_unfocused_is_the_positive_result():
    out = sp.summarize([burst(1, seen=False), burst(2, seen=True)], (True, "ok"))
    assert "REACHED the game" in out
    assert "0x00000202" in out


def test_no_landing_while_unfocused_rules_the_approach_out():
    out = sp.summarize([burst(1, seen=False), burst(2, seen=False)], (True, "ok"))
    assert "dead end" in out
    assert "nested X server" in out


def test_an_unanswered_run_reports_no_landing_rather_than_inventing_one():
    """seen=None means the operator never answered. That is not evidence of
    success, and the summary must not read it as any."""
    out = sp.summarize([burst(1, seen=None)], (True, "ok"))
    assert "REACHED" not in out
    assert "no burst reached" in out


def test_send_errors_are_surfaced():
    out = sp.summarize([burst(1, seen=False, error="BadWindow")], (True, "ok"))
    assert "failed to send" in out
