#!/usr/bin/env python3
"""Host environment pre-flight check — ADR 047.

Run via: make preflight
Exits 0 if all hard checks pass; exits with the count of failures otherwise.
Warnings (e.g. keyboard privileges on Linux) do not affect the exit code.
"""

import importlib.metadata
import importlib.util
import shutil
import subprocess
import sys

PASS = "PASS"
FAIL = "FAIL"
WARN = "WARN"


def _line(status, name, detail):
    return status, f"[{status}] {name:<14} {detail}"


def check_python():
    v = sys.version_info
    ver = f"{v.major}.{v.minor}.{v.micro}"
    if v >= (3, 10):
        return _line(PASS, "python", f"{ver} >= 3.10")
    return _line(FAIL, "python", f"{ver} < 3.10  (need >= 3.10; upgrade Python)")


def check_tool(name):
    if not shutil.which(name):
        return _line(FAIL, name, f"not found on PATH")
    try:
        raw = subprocess.check_output(
            [name, "--version"], stderr=subprocess.STDOUT, text=True
        ).splitlines()[0].strip()
    except Exception:
        raw = "(version unknown)"
    return _line(PASS, name, raw)


def _pkg_version(import_name, pkg_name=None):
    """Return version string for an importable package, or None."""
    try:
        mod = __import__(import_name)
        ver = getattr(mod, "__version__", None)
        if ver:
            return ver
    except Exception:
        pass
    try:
        return importlib.metadata.version(pkg_name or import_name)
    except Exception:
        return None


def check_package(import_name, label=None, pkg_name=None):
    name = label or import_name
    if importlib.util.find_spec(import_name) is None:
        return _line(FAIL, name, "not importable — run: uv sync")
    ver = _pkg_version(import_name, pkg_name) or "(installed)"
    if import_name == "yaml":
        pyyaml_ver = _pkg_version("yaml", "pyyaml") or ver
        return _line(PASS, name, f"(pyyaml {pyyaml_ver})")
    return _line(PASS, name, ver)


def check_keyboard():
    if importlib.util.find_spec("keyboard") is None:
        return _line(FAIL, "keyboard", "not importable — run: uv sync")
    ver = _pkg_version("keyboard") or "(installed)"
    if sys.platform != "linux":
        return _line(PASS, "keyboard", ver)
    try:
        with open("/dev/input/event0", "rb"):
            pass
        return _line(PASS, "keyboard", ver)
    except PermissionError:
        return _line(
            WARN,
            "keyboard",
            f"{ver}  (Linux: root or 'input' group required — sudo usermod -aG input $USER)",
        )
    except FileNotFoundError:
        return _line(
            WARN,
            "keyboard",
            f"{ver}  (Linux: /dev/input/event0 not found — cannot probe privileges)",
        )


def main():
    results = [
        check_python(),
        check_tool("uv"),
        check_tool("make"),
        check_tool("git"),
        check_package("cv2"),
        check_package("easyocr"),
        check_package("mss"),
        check_keyboard(),
        check_package("numpy"),
        check_package("yaml", pkg_name="pyyaml"),
        check_package("transitions"),
        check_package("plotly"),
        check_package("pandas"),
    ]

    for _, line in results:
        print(line)

    passed  = sum(1 for s, _ in results if s == PASS)
    failed  = sum(1 for s, _ in results if s == FAIL)
    warned  = sum(1 for s, _ in results if s == WARN)
    w_label = f"{warned} warning{'s' if warned != 1 else ''}"

    print()
    print(f"Pre-flight: {passed} passed, {failed} failed, {w_label}.")

    if failed:
        print()
        for s, line in results:
            if s == FAIL:
                print(f"  {line.strip()}")

    sys.exit(failed)


if __name__ == "__main__":
    main()
