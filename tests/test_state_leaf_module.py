"""CR-018-15: the FSM vocabulary lives in a leaf module.

analyzer.py loads EasyOCR and torch at import time. Measured 2026-09-27,
importing behavior_tree, controller or tick_handlers just for GameState cost
2.1-2.7 s; from wingman.state it is 0.06-0.21 s. That is paid on every
single-test run and by every xdist worker, so these guards keep the modules
that need only the enum from drifting back onto the analyzer.
"""

import ast
import pathlib
import subprocess
import sys

import wingman.analyzer as analyzer
import wingman.state as state

# The modules that need only the FSM vocabulary. main.py is the composition
# root and builds the analyzer itself, so it is not on this list.
_ENUM_ONLY_MODULES = (
    "wingman.behavior_tree",
    "wingman.controller",
    "wingman.tick_handlers",
    "wingman.eject_stuck_detector",
)


def test_state_imports_only_the_standard_library():
    tree = ast.parse(pathlib.Path(state.__file__).read_text())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0, "state.py must not import from the wingman package"
            imported.add(node.module.split(".")[0])
    assert imported <= set(sys.stdlib_module_names), imported - set(sys.stdlib_module_names)


def test_enum_only_modules_do_not_import_the_analyzer():
    """Run in a fresh interpreter: this test process has already imported the
    analyzer, so checking sys.modules here would prove nothing."""
    code = (
        "import sys\n"
        + "".join(f"import {m}\n" for m in _ENUM_ONLY_MODULES)
        + "heavy = [m for m in ('wingman.analyzer', 'easyocr', 'torch') if m in sys.modules]\n"
        + "print(','.join(heavy))\n"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         timeout=60, check=True)
    assert out.stdout.strip() == "", f"pulled in: {out.stdout.strip()}"


def test_the_analyzer_re_exports_the_same_objects():
    """Existing callers import these from wingman.analyzer. A copy instead of a
    re-export would make GameState.GAME_BATTLE from one module unequal to the
    other's, and every state comparison across the boundary would fail."""
    assert analyzer.GameState is state.GameState
    assert analyzer.GameEvent is state.GameEvent
    assert analyzer.BATTLE_STATES is state.BATTLE_STATES
    assert analyzer._FSM_TRANSITIONS is state.FSM_TRANSITIONS
    assert (analyzer.NOSE_UP, analyzer.NOSE_DOWN, analyzer.NOSE_UNKNOWN) == (
        state.NOSE_UP, state.NOSE_DOWN, state.NOSE_UNKNOWN)
