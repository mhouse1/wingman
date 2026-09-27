"""CR-018-12: every writer of flight input has a row in the precedence table.

Three authorities act on the same flight axes (the tree, the per-tick reflexes,
the mission threads), and the rules for who beats whom used to be written one
call site at a time. docs/architecture.md "Flight-input precedence" is now the one
table, and the review's rule, "no new actuator outside the tree without a row in
that table", is this test.
"""

import ast
import pathlib

_PRESS_HELPERS = ("_climb_key", "_press_tracking_key", "_release_tracking_key",
                  "_eject_key", "_press_key")


def _owners_in_code():
    tree = ast.parse(pathlib.Path("wingman/controller.py").read_text(encoding="utf-8"))
    owners = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name in ("_climb_key", "_eject_key"):
            defaults = node.args.defaults
            for d in defaults:
                if isinstance(d, ast.Constant) and isinstance(d.value, str):
                    owners.add(d.value)
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        name = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", None)
        if name not in _PRESS_HELPERS:
            continue
        for k in node.keywords:
            if k.arg in ("action", "owner", "note") and isinstance(k.value, ast.Constant):
                owners.add(k.value.value)
        positional = {"_press_tracking_key": 1, "_release_tracking_key": 1, "_eject_key": 2}
        i = positional.get(name)
        if i is not None and len(node.args) > i and isinstance(node.args[i], ast.Constant):
            owners.add(node.args[i].value)
    return owners


def _precedence_section():
    doc = pathlib.Path("docs/architecture.md").read_text(encoding="utf-8")
    start = doc.index("### Flight-input precedence (CR-018-12)")
    end = doc.index("\n### ", start + 10)
    return doc[start:end]


def test_the_collector_finds_the_known_writers():
    """Guards the guard: an AST walk that silently found nothing would pass."""
    assert {"cruise", "evade", "stall_prevention", "climb", "climb_emergency",
            "boundary", "spawn_guard", "missile_evade", "disengage_roll"} <= _owners_in_code()


def test_every_writer_has_a_row():
    section = _precedence_section()
    missing = sorted(o for o in _owners_in_code() if f"`{o}`" not in section)
    assert missing == [], (
        f"flight-input writers with no row in docs/architecture.md 'Flight-input "
        f"precedence': {missing}. Add the row in the same change (CR-018-12).")
