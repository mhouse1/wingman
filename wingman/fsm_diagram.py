"""Render the FSM transition table as a Mermaid diagram. CR-018-14.

The hand-drawn diagram in docs/architecture.md had drifted from the table by at
least seven transitions (starting_play_visible, starting_recovery,
starting_give_up, cancel_detected from the lobby, manual_release,
respawn_detected, unknown_to_end_detected) before this replaced it. The doc now
holds the output of `render_doc_block()` between two markers, and
tests/test_fsm_table.py fails when the two differ. Regenerate with `make fsm`.

Labels are trigger names with spaces for underscores: the project's Mermaid
profile keeps labels to plain words for renderer compatibility (CLAUDE.md).
"""

from .state import FSM_TRANSITIONS, GameState

BEGIN = "<!-- BEGIN GENERATED: make fsm -->"
END = "<!-- END GENERATED: make fsm -->"


def _sources(transition) -> "list[str]":
    source = transition["source"]
    return list(source) if isinstance(source, (list, tuple)) else [source]


def render_mermaid(transitions=FSM_TRANSITIONS) -> str:
    """One edge per (source, destination) pair, its triggers joined by 'or'."""
    edges: "dict[tuple[str, str], list[str]]" = {}
    for t in transitions:
        for src in _sources(t):
            edges.setdefault((src, t["dest"]), []).append(t["trigger"].replace("_", " "))
    lines = ["stateDiagram-v2", f"    [*] --> {GameState.GAME_UNKNOWN.name} : startup"]
    lines += [f"    {src} --> {dst} : {' or '.join(labels)}"
              for (src, dst), labels in edges.items()]
    return "\n".join(lines) + "\n"


def render_doc_block() -> str:
    return f"{BEGIN}\n```mermaid\n{render_mermaid()}```\n{END}"


def replace_doc_block(text: str) -> str:
    """`text` with the generated block between the markers replaced."""
    start = text.index(BEGIN)
    end = text.index(END, start) + len(END)
    return text[:start] + render_doc_block() + text[end:]
