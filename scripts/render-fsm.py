#!/usr/bin/env python3
"""Print the game FSM as Mermaid, generated from wingman/state.py. CR-018-14.

Usage:
    render-fsm.py              # the Mermaid diagram, to stdout
    render-fsm.py --write-doc  # also refresh the generated block in docs/architecture.md
"""

import argparse
import os
import pathlib
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from wingman.fsm_diagram import render_mermaid, replace_doc_block  # noqa: E402

DOC = pathlib.Path("docs/architecture.md")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write-doc", action="store_true",
                        help=f"rewrite the generated block in {DOC}")
    args = parser.parse_args()
    print(render_mermaid(), end="")
    if args.write_doc:
        DOC.write_text(replace_doc_block(DOC.read_text(encoding="utf-8")), encoding="utf-8")
        print(f"updated {DOC}", file=sys.stderr)


if __name__ == "__main__":
    main()
