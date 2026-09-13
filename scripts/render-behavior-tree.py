#!/usr/bin/env python3
"""Research 013 — render the actual ADR 024 selector, not a hand-drawn copy.

`docs/architecture.md`'s Behavior Tree section is prose plus a hand-maintained
Mermaid diagram. It has drifted from code before (ADR 139 D1's own history is
an offset-based `build_tree()` that silently broke priority order, twice,
before anyone noticed in the diagram). This script builds the tree the same
way `wingman/main.py` does — `build_tree(bt_cfg, regroup_enabled=...)`,
selection-only, no actuators wired, no analyzer, no live game needed — and
prints its *actual* structure, so "does the diagram match the code" is a
question you can answer by running something instead of re-reading both.

Walks composite children generically (not just a flat top-level list), so it
keeps working once ACS Mode adds a nested branch (a Selector or Sequence
living inside one priority slot, e.g. Engage-vs-BoresightEngage) instead of
today's one-leaf-per-slot shape.

Usage:
    render-behavior-tree.py                 # ascii tree + Mermaid, to stdout
    render-behavior-tree.py --mermaid-only
    render-behavior-tree.py --config PATH   # default wingman/config.yaml
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import py_trees  # noqa: E402
import yaml  # noqa: E402

from wingman.behavior_tree import build_tree  # noqa: E402

_SHAPE = {
    py_trees.composites.Selector: ("{{", "}}"),   # hexagon: "first that can run"
    py_trees.composites.Sequence: ("[[", "]]"),   # subroutine: "all in order"
}


def _label(node) -> str:
    text = node.name.replace('"', "'")
    for cls, (_open, _close) in _SHAPE.items():
        if isinstance(node, cls):
            kind = "any" if cls is py_trees.composites.Selector else "all"
            return f'{text} ({kind})'
    return text


def _mermaid_lines(node, node_id, counter, lines) -> None:
    for child in getattr(node, "children", []):
        counter[0] += 1
        child_id = f"n{counter[0]}"
        opener, closer = _SHAPE.get(type(child), ("[", "]"))
        lines.append(f'    {node_id} --> {child_id}{opener}"{_label(child)}"{closer}')
        _mermaid_lines(child, child_id, counter, lines)


def to_mermaid(root) -> str:
    lines = ["flowchart TD", f'    n0{{{{"{_label(root)}"}}}}']
    _mermaid_lines(root, "n0", [0], lines)
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--config", default="wingman/config.yaml")
    ap.add_argument("--mermaid-only", action="store_true")
    ap.add_argument("--ascii-only", action="store_true")
    args = ap.parse_args(argv)

    with open(args.config, "r", encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh)

    bt_cfg = cfg.get("behavior_tree", {})
    regroup_enabled = bool(cfg.get("minimap", {}).get("regroup_enabled", False))
    tree = build_tree(bt_cfg, regroup_enabled=regroup_enabled)

    if not args.mermaid_only:
        print("# Selection-only build: no actuators wired, matches config.yaml as loaded.")
        print(py_trees.display.ascii_tree(tree.root))
    if not args.ascii_only:
        if not args.mermaid_only:
            print("\n--- paste into a ```mermaid fence in docs/architecture.md ---\n")
        print(to_mermaid(tree.root))
    return 0


if __name__ == "__main__":
    sys.exit(main())
