#!/usr/bin/env bash
# upgrade-linux.sh — Bring a Linux Wingman install up to date: Python environment
# and desktop integration.
#
# Run it on every Linux PC that runs Wingman after pulling this change, and
# again after any Ubuntu release upgrade. It is safe to re-run: each step
# checks before it acts. setup-linux.sh Step 5 calls it on a fresh install.
#
#   scripts/upgrade-linux.sh                 # or: make upgrade-linux
#   scripts/upgrade-linux.sh --rebuild-venv  # also move a system-Python venv
#                                            # onto a uv-managed Python
#
# Why it exists: the PipeWire capture backend (wingman/capture.py) imports gi
# (PyGObject). The venv used to borrow the apt-built copy through
# system_gi_bridge.pth, which only works while the system python3 has the same
# minor version as the venv. Ubuntu 26.04 ships python3.14 and builds its gi for
# 3.14 only, so the 3.12 venv fails with "cannot import name '_gi' from
# partially initialized module 'gi'". PyGObject is now a locked dependency in
# pyproject.toml, compiled into the venv by `uv sync`, and that build needs
# headers from apt. ADR 154 records the decision.
#
# Steps:
#   1. Recreate .venv if its interpreter no longer runs (a venv built on a
#      system Python that the OS upgrade removed), on a uv-managed Python.
#   2. apt: the compiler and headers PyGObject and pycairo build against, the
#      GStreamer typelib gi loads at run time, Xvfb for the gate lanes, and,
#      for a venv on the system Python only, that Python's tkinter binding.
#   3. Delete the obsolete system_gi_bridge.pth.
#   4. uv sync --all-groups, which compiles PyGObject and pycairo.
#   5. Verify gi and Gst import from the venv, and tkinter for make test.
#   6. GNOME only: install or refresh the window-left Shell extension that opens
#      the game window at the top-left of the screen (ADR 155), declaring the
#      running Shell version so an upgrade does not leave it OUT OF DATE.

set -euo pipefail

RED='\033[0;31m'
GRN='\033[0;32m'
YLW='\033[1;33m'
BLD='\033[1m'
RST='\033[0m'

info()  { echo -e "${GRN}[+]${RST} $*"; }
warn()  { echo -e "${YLW}[!]${RST} $*"; }
die()   { echo -e "${RED}[ERROR]${RST} $*" >&2; exit 1; }

REBUILD_VENV=0
for arg in "$@"; do
    case "$arg" in
        --rebuild-venv) REBUILD_VENV=1 ;;
        -h|--help) sed -n '2,29p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) die "Unknown argument: $arg (try --help)" ;;
    esac
done

[[ "$(uname -s)" == "Linux" ]] || die "Linux only: Windows installs do not use gi."
[[ $EUID -ne 0 ]] || die "Run as your normal user, not root; the script calls sudo for apt itself."

# A snap terminal rewrites $HOME to ~/snap/code/<rev>/, and a uv-managed Python
# installed there is deleted by the next snap refresh, taking the venv with it.
if [[ -n "${SNAP:-}" ]] || [[ "${HOME}" == */snap/* ]]; then
    die "Running inside a snap terminal (HOME=${HOME}).
    A uv-managed Python installed from here would be garbage-collected by snap.
    Open a non-snap terminal (e.g. GNOME Terminal) and re-run this script."
fi

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="${REPO_ROOT}/.venv"
cd "$REPO_ROOT"
# The project venv is .venv; an operator's unrelated active venv must not
# redirect uv sync.
unset VIRTUAL_ENV

command -v uv &>/dev/null || die "uv not found. Install it first (scripts/setup-linux.sh Step 5)."
grep -qi '"pygobject' pyproject.toml \
    || die "This checkout predates PyGObject in pyproject.toml. git pull, then re-run."
PY_VERSION="$(tr -d '[:space:]' < .python-version)"

# ---------------------------------------------------------------------------
# Step 1 — A venv whose interpreter still runs
# ---------------------------------------------------------------------------
venv_python_runs() {
    [[ -x "${VENV}/bin/python" ]] && "${VENV}/bin/python" -c 'import sys' &>/dev/null
}

venv_base_prefix() {
    "${VENV}/bin/python" -c 'import sys; print(sys.base_prefix)'
}

create_managed_venv() {
    info "Creating .venv on a uv-managed Python ${PY_VERSION}..."
    rm -rf "$VENV"
    UV_PYTHON_PREFERENCE=only-managed uv venv --python "$PY_VERSION" "$VENV"
}

if [[ ! -e "$VENV" ]]; then
    create_managed_venv
elif ! venv_python_runs; then
    warn ".venv's interpreter no longer runs (its base Python was probably removed by an OS upgrade)."
    create_managed_venv
elif [[ "$(venv_base_prefix)" == /usr* ]]; then
    if [[ $REBUILD_VENV -eq 1 ]]; then
        create_managed_venv
    else
        warn ".venv runs on the system Python ($(venv_base_prefix)). It will break at the"
        warn "next Ubuntu Python version bump. Re-run with --rebuild-venv to move it onto a"
        warn "uv-managed Python now (re-downloads packages uv has not cached)."
    fi
else
    info ".venv interpreter OK ($(venv_base_prefix))."
fi

# ---------------------------------------------------------------------------
# Step 2 — Build dependencies from apt
# ---------------------------------------------------------------------------
# build-essential, pkg-config  compile PyGObject and pycairo from their sdists
#                              (PyPI ships no Linux wheels for either)
# libgirepository-2.0-dev      PyGObject >= 3.52 builds against GLib >= 2.80
# libcairo2-dev                pycairo, a hard dependency of PyGObject
# gir1.2-gstreamer-1.0         Gst typelib that gi loads at run time
# xvfb                         private display for the make tp gate lanes
#                              (scripts/gate-display.sh, ADR 153)
APT_PACKAGES=(build-essential pkg-config libgirepository-2.0-dev libcairo2-dev gir1.2-gstreamer-1.0 xvfb)

# Read the whole output: `| grep -q` exits at the Candidate line, and under
# pipefail the SIGPIPE it sends apt-cache, still printing the version table,
# reads as "no candidate". LC_ALL=C keeps the label English.
apt_has_candidate() {
    local candidate
    candidate="$(LC_ALL=C apt-cache policy "$1" 2>/dev/null | awk '/^ *Candidate:/ {print $2}')"
    [[ -n "$candidate" && "$candidate" != "(none)" ]]
}

PY_MINOR="$("${VENV}/bin/python" -c 'import sys; print(f"{sys.version_info[0]}.{sys.version_info[1]}")')"

# A venv on a system Python finds Python.h only if apt's -dev package is
# installed; uv-managed Pythons ship their own headers.
PY_INCLUDE="$("${VENV}/bin/python" -c "import sysconfig; print(sysconfig.get_paths()['include'])")"
if [[ ! -f "${PY_INCLUDE}/Python.h" ]]; then
    APT_PACKAGES+=("python${PY_MINOR}-dev")
fi

# Likewise tkinter (make test, wingman/calibrate.py): a uv-managed Python ships
# it, a system Python needs apt's binding for that exact version. Ubuntu names
# it pythonX.Y-tk; python3-tk covers only the release's default Python.
if [[ "$(venv_base_prefix)" == /usr* ]] \
        && ! "${VENV}/bin/python" -c 'import tkinter' &>/dev/null; then
    if apt_has_candidate "python${PY_MINOR}-tk"; then
        APT_PACKAGES+=("python${PY_MINOR}-tk")
    else
        APT_PACKAGES+=(python3-tk)
    fi
fi

MISSING=()
for pkg in "${APT_PACKAGES[@]}"; do
    dpkg -s "$pkg" &>/dev/null || MISSING+=("$pkg")
done

if [[ ${#MISSING[@]} -eq 0 ]]; then
    info "Build dependencies already installed."
else
    info "Installing: ${MISSING[*]}"
    sudo apt-get update -qq
    for pkg in "${MISSING[@]}"; do
        apt_has_candidate "$pkg" \
            || die "apt has no candidate for ${pkg}. PyGObject needs GLib 2.80 or later (Ubuntu 24.04+)."
    done
    sudo apt-get install -y "${MISSING[@]}"
fi

# ---------------------------------------------------------------------------
# Step 3 — Remove the old gi bridge
# ---------------------------------------------------------------------------
# Left in place it would also put the system Python's whole dist-packages on the
# venv's path, where a module the venv lacks resolves to a build for the wrong
# Python.
shopt -s nullglob
BRIDGES=("${VENV}"/lib/python*/site-packages/system_gi_bridge.pth)
shopt -u nullglob
if [[ ${#BRIDGES[@]} -gt 0 ]]; then
    rm -f "${BRIDGES[@]}"
    info "Removed ${BRIDGES[*]#"${REPO_ROOT}/"}"
else
    info "No system_gi_bridge.pth to remove."
fi

# ---------------------------------------------------------------------------
# Step 4 — Sync the locked environment
# ---------------------------------------------------------------------------
info "Running 'uv sync --all-groups' (the first run compiles PyGObject and pycairo)..."
uv sync --all-groups

# ---------------------------------------------------------------------------
# Step 5 — Verify
# ---------------------------------------------------------------------------
info "Verifying gi and tkinter in the venv..."
if ! uv run --no-sync python - <<'EOF'
import os
import sys

import gi

venv = os.path.realpath(sys.prefix)
if not os.path.realpath(gi.__file__).startswith(venv + os.sep):
    sys.exit(f"gi imported from {gi.__file__}, outside the venv {venv}")
gi.require_version("Gst", "1.0")
from gi.repository import Gst

Gst.init(None)
print(f"    gi {gi.__version__} ({gi.__file__})")
print(f"    {Gst.version_string()}")
EOF
then
    die "gi verification failed (output above)."
fi

if uv run --no-sync python -c "import tkinter" &>/dev/null; then
    info "tkinter OK."
else
    warn "tkinter does not import. make test needs it: on a system-Python venv install"
    warn "python3-tk, or re-run with --rebuild-venv (uv-managed Pythons include it)."
fi

# ---------------------------------------------------------------------------
# Step 6 — GNOME Shell window-left extension (ADR 155)
# ---------------------------------------------------------------------------
# GNOME refuses an extension whose metadata does not list the running Shell's
# major version: 46 to 50 left it "OUT OF DATE" and disabled, and the nested
# display's window stopped opening at the top-left. The installed copy declares
# the running version too, so the next upgrade does not silently drop it. The
# extension uses only the window-placement API, which has not changed since 45;
# check the result after a GNOME upgrade all the same.
EXT_UUID="wingman-window-left@wingman.local"
EXT_SRC="${REPO_ROOT}/scripts/gnome-extension/${EXT_UUID}"
EXT_DST="${HOME}/.local/share/gnome-shell/extensions/${EXT_UUID}"
EXT_RELOGIN=0
if ! command -v gnome-shell &>/dev/null || ! command -v gnome-extensions &>/dev/null; then
    info "GNOME Shell not found — skipping the window-left extension."
else
    SHELL_MAJOR="$(gnome-shell --version | awk '{print $3}' | cut -d. -f1)"
    EXT_STAGE="$(mktemp -d)"
    cp "${EXT_SRC}/extension.js" "${EXT_STAGE}/"
    python3 - "${EXT_SRC}/metadata.json" "${EXT_STAGE}/metadata.json" "$SHELL_MAJOR" <<'EOF'
import json
import sys

src, dst, major = sys.argv[1], sys.argv[2], sys.argv[3]
meta = json.load(open(src, encoding="utf-8"))
if major not in meta["shell-version"]:
    meta["shell-version"].append(major)
    print(f"    GNOME Shell {major} is newer than the extension's tested list; declared it.")
with open(dst, "w", encoding="utf-8") as f:
    json.dump(meta, f, indent=4)
    f.write("\n")
EOF
    if diff -rq "$EXT_STAGE" "$EXT_DST" &>/dev/null; then
        info "Window-left extension already current."
    else
        mkdir -p "$EXT_DST"
        cp "${EXT_STAGE}/extension.js" "${EXT_STAGE}/metadata.json" "$EXT_DST/"
        info "Installed the window-left extension (GNOME Shell ${SHELL_MAJOR})."
        EXT_RELOGIN=1
    fi
    rm -rf "$EXT_STAGE"

    # gnome-extensions enable only knows extensions the running Shell has
    # scanned, which a new install is not until the next login. The setting
    # below is what that command writes, and the Shell reads it at login.
    ENABLED="$(gsettings get org.gnome.shell enabled-extensions)"
    if [[ "$ENABLED" != *"'${EXT_UUID}'"* ]]; then
        ENABLED="$(python3 -c '
import ast, sys
current = sys.argv[1].removeprefix("@as ")
uuids = ast.literal_eval(current)
uuids.append(sys.argv[2])
print(repr(uuids))' "$ENABLED" "$EXT_UUID")"
        gsettings set org.gnome.shell enabled-extensions "$ENABLED"
        info "Enabled the window-left extension."
        EXT_RELOGIN=1
    fi
    if [[ "$(gsettings get org.gnome.shell disable-user-extensions)" == "true" ]]; then
        warn "User extensions are switched off (Extensions app, top toggle); the"
        warn "window-left extension will not run until they are switched back on."
    fi

    EXT_STATE="$(gnome-extensions info "$EXT_UUID" 2>/dev/null | awk -F': ' '/State:/ {print $2}')"
    if [[ $EXT_RELOGIN -eq 1 || "$EXT_STATE" != "ACTIVE" ]]; then
        warn "Log out and back in once: on Wayland GNOME Shell loads extension"
        warn "changes only at login (current state: ${EXT_STATE:-not loaded})."
    else
        info "Window-left extension ACTIVE."
    fi
fi

echo ""
echo -e "${GRN}${BLD}Wingman install up to date.${RST}"
