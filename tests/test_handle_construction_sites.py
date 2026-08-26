"""ADR 092 Design 1 — pin the set of handle-construction sites.

ADR 091: `_linux_key_event` opened a new `Xlib.display.Display` for every key
press and every key release. Each construction retains ~16.2 KB permanently —
`close()` releases the socket but cannot un-create the resource classes
`Display.__init__` builds. That ran for two months at up to 1,666 MB/h.

This test does not detect leaks. It makes *adding a construction site* a
decision rather than a merge: any new one fails here with an explanation, and
staying passing requires either using the shared factory or writing down why
this site is different.

Discovery is by AST rather than regex so comments and strings cannot match, and
sites are keyed by **enclosing function** rather than line number — line numbers
shift on every unrelated edit and would turn this into a maintenance tax.

Research 009 lists other constructor categories worth watching. `_WATCHED` is
deliberately narrow: a guard that fires on everything gets disabled.
"""

import ast
from pathlib import Path

# Constructor names to track. Extend deliberately — see the module docstring.
_WATCHED = ("Display",)

SOURCE_DIR = Path("wingman")

# (module, enclosing function) -> (count, why it is allowed)
_APPROVED_SITES = {
    ("input_linux.py", "_shared_xtest_display"): (
        1, "THE APPROVED FACTORY — one connection per process (ADR 091)"),
    ("input_linux.py", "_linux_click"): (
        1, "per-call, deliberately: low hundreds per session, and its sleeps "
           "must not be held under the injection lock (ADR 091, 'Not done')"),
    ("input_linux.py", "_listener_loop"): (
        3, "once per listener start — setup, record and control connections"),
    ("input_linux.py", "_record_handler"): (
        1, "guarded by `if new:` — only when keys are registered after the "
           "loop has started"),
    ("move_game_window.py", "_connect"): (
        1, "one-shot tooling, never on the tick path"),
}

_GUIDANCE = """
New {ctor}() construction site: wingman/{module}:{func}

If this runs per operation on the tick path, it is the ADR 091 defect — each
construction retains ~16.2 KB permanently, which cost 1,277 MB in 105 minutes.
Use the shared factory (input_linux._shared_xtest_display).

If the site is justified, add it to _APPROVED_SITES with the reason.
"""


def _sites_in(path: Path) -> dict:
    """Map (module, enclosing function) -> count of watched constructor calls."""
    found: dict = {}

    def walk(node, enclosing):
        for child in ast.iter_child_nodes(node):
            name = (child.name
                    if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
                    else enclosing)
            if isinstance(child, ast.Call):
                fn = child.func
                called = (fn.attr if isinstance(fn, ast.Attribute)
                          else fn.id if isinstance(fn, ast.Name) else None)
                if called in _WATCHED:
                    key = (path.name, name)
                    found[key] = found.get(key, 0) + 1
            walk(child, name)

    walk(ast.parse(path.read_text(encoding="utf-8")), "<module>")
    return found


def _scan() -> dict:
    sites: dict = {}
    for path in sorted(SOURCE_DIR.glob("*.py")):
        sites.update(_sites_in(path))
    return sites


def test_no_unapproved_handle_construction_sites():
    """The guard: a new site must be a deliberate decision."""
    found = _scan()
    new = sorted(k for k in found if k not in _APPROVED_SITES)
    assert not new, "\n".join(
        _GUIDANCE.format(ctor=_WATCHED[0], module=m, func=f) for m, f in new)


def test_approved_sites_have_not_multiplied():
    """A site going from one construction to many is the same defect."""
    found = _scan()
    grown = {k: (found[k], _APPROVED_SITES[k][0])
             for k in _APPROVED_SITES if k in found and found[k] > _APPROVED_SITES[k][0]}
    assert not grown, (
        "construction count increased at an approved site — if it is now "
        f"per-operation, see ADR 091: {grown}")


def test_registry_has_no_stale_entries():
    """A removed site left in the registry silently weakens the guard."""
    found = _scan()
    stale = sorted(k for k in _APPROVED_SITES if k not in found)
    assert not stale, f"_APPROVED_SITES lists sites that no longer exist: {stale}"


def test_every_approved_site_states_a_reason():
    for key, (_count, reason) in _APPROVED_SITES.items():
        assert reason and len(reason) > 20, f"{key} needs a real justification"


def test_the_shared_factory_is_still_the_only_factory():
    """The ADR 091 fix in one assertion: key injection must not construct."""
    found = _scan()
    assert ("input_linux.py", "_linux_key_event") not in found, (
        "_linux_key_event constructs a Display again — this is exactly the "
        "ADR 091 leak (~16.2 KB retained per key press AND per release)")
    assert found.get(("input_linux.py", "_shared_xtest_display")) == 1


def test_scanner_finds_a_planted_site(tmp_path):
    """The guard must be able to fail — a scanner that finds nothing is inert."""
    mod = tmp_path / "planted.py"
    mod.write_text("from Xlib import display\n"
                   "def hot_path():\n"
                   "    d = display.Display(':0')\n"
                   "    d.close()\n", encoding="utf-8")
    assert _sites_in(mod) == {("planted.py", "hot_path"): 1}


def test_scanner_ignores_the_name_in_comments_and_strings(tmp_path):
    """Why this is an AST walk and not a regex."""
    mod = tmp_path / "decoy.py"
    mod.write_text('# Display(":0") in a comment\n'
                   'DOC = "call Display(\':0\') here"\n'
                   'def f():\n'
                   '    return DOC\n', encoding="utf-8")
    assert _sites_in(mod) == {}
