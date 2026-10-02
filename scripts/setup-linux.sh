#!/usr/bin/env bash
# setup-linux.sh — Install and configure MetalStorm + Wingman on Ubuntu/Linux.
#
# Automates (numbers match the Step sections below):
#   1. Flatpak
#   2. Heroic Games Launcher install
#   3. Proton-GE-latest download into Heroic's tools directory (so the Wingman
#      Makefile's PROTON_ROOT default resolves without an override — Heroic
#      left to its own defaults does not always pick GE-Proton)
#   4. i386 multiarch + 32-bit GL/Vulkan libs (Steam Runtime container
#      requirement — needed even though MetalStorm itself is 64-bit)
#   5. uv install, then scripts/upgrade-linux.sh (ADR 154): the .venv on a
#      uv-managed Python, PyGObject build headers, Xvfb for the gate lanes
#      (ADR 153), `uv sync --all-groups`, the gi import check, and the GNOME
#      window-left extension that opens the game window top-left (ADR 155)
#   6. umu-run symlink (Heroic bundles its own copy; there's no separate
#      install for it) — added after MetalStorm install, since that's the
#      point at which Heroic is guaranteed to have fetched it
#
# Requires Ubuntu 24.04 or later (checked first): PyGObject builds against
# GLib 2.80, which older releases do not ship.
#
# Does NOT touch `input` group membership — obsolete since ADR 053: Wingman's
# Linux input path uses XTest/XRecord, which needs neither root nor that group.
#
# Requires manual steps (script pauses and prompts):
#   - Epic Games Store login (OAuth in browser)
#   - MetalStorm install in Heroic UI
#   No per-game Wine/UMU configuration needed — Heroic defaults work out of the box.
#
# See docs/job-aids/010-run-metalstorm-on-linux.md for the full narrative,
# including troubleshooting for failure modes this script can't safely
# auto-fix (e.g. a machine with a broken/incomplete apt sources list).

set -euo pipefail

RED='\033[0;31m'
GRN='\033[0;32m'
YLW='\033[1;33m'
BLD='\033[1m'
RST='\033[0m'

info()  { echo -e "${GRN}[+]${RST} $*"; }
warn()  { echo -e "${YLW}[!]${RST} $*"; }
die()   { echo -e "${RED}[ERROR]${RST} $*" >&2; exit 1; }
pause() { echo -e "\n${BLD}${YLW}>>> MANUAL STEP — press Enter when done: $*${RST}"; read -r; }

# Snap terminals (e.g. VS Code installed as snap) rewrite $HOME to
# ~/snap/code/<rev>/, causing UMU to look for umu-shim in the wrong place.
# Detect and abort early rather than producing a confusing failure later.
if [[ -n "${SNAP:-}" ]] || [[ "${HOME}" == */snap/* ]]; then
    die "Running inside a snap terminal (HOME=${HOME}).
    UMU will fail to find umu-shim from this environment.
    Open a non-snap terminal (e.g. GNOME Terminal) and re-run this script."
fi

# Fail before the long Flatpak/Heroic/Proton steps, not at Step 5: PyGObject
# needs GLib 2.80 (ADR 154), which Ubuntu ships from 24.04. Other Debian-based
# distros are not version-checked; Step 5 still fails cleanly if apt lacks the
# headers.
# shellcheck disable=SC1091
source /etc/os-release
if [[ "${ID:-}" == "ubuntu" ]]; then
    if dpkg --compare-versions "${VERSION_ID:-0}" lt 24.04; then
        die "Ubuntu ${VERSION_ID} is too old: Wingman needs Ubuntu 24.04 or later
    (PyGObject builds against GLib 2.80 — ADR 154). Upgrade the OS first."
    fi
    info "Ubuntu ${VERSION_ID} (${VERSION_CODENAME:-unknown})."
else
    warn "${PRETTY_NAME:-This distro} is not Ubuntu — continuing; it needs GLib 2.80 or later."
fi

# Proton-GE is stored under config/, not data/ — confirmed from Heroic 2.22.0 logs.
HEROIC_CONFIG="${HOME}/.var/app/com.heroicgameslauncher.hgl/config/heroic"
PROTON_DIR="${HEROIC_CONFIG}/tools/proton"

# ---------------------------------------------------------------------------
# Step 1 — Flatpak
# ---------------------------------------------------------------------------
info "Checking Flatpak..."
if ! command -v flatpak &>/dev/null; then
    warn "Flatpak not found — installing via apt"
    sudo apt-get update -qq
    sudo apt-get install -y flatpak
    flatpak remote-add --if-not-exists flathub https://flathub.org/repo/flathub.flatpakrepo
    warn "Flatpak installed. A logout/login is recommended after this script completes."
else
    info "Flatpak already installed."
fi

# ---------------------------------------------------------------------------
# Step 2 — Heroic Games Launcher
# ---------------------------------------------------------------------------
info "Checking Heroic Games Launcher..."
if ! flatpak list --app 2>/dev/null | grep -q "com.heroicgameslauncher.hgl"; then
    info "Installing Heroic from Flathub..."
    flatpak install -y flathub com.heroicgameslauncher.hgl
else
    info "Heroic already installed."
fi

# ---------------------------------------------------------------------------
# Step 3 — Proton-GE-latest
# ---------------------------------------------------------------------------
info "Fetching latest Proton-GE release info from GitHub..."
RELEASE_JSON=$(curl -sf "https://api.github.com/repos/GloriousEggroll/proton-ge-custom/releases/latest")
TAG=$(echo "$RELEASE_JSON" | grep '"tag_name"' | head -1 | cut -d'"' -f4)
TARBALL_URL=$(echo "$RELEASE_JSON" | grep "browser_download_url" | grep "\.tar\.gz\"" | head -1 | cut -d'"' -f4)

if [[ -z "$TAG" || -z "$TARBALL_URL" ]]; then
    warn "Could not fetch Proton-GE release info. Check your internet connection."
    warn "Download manually from https://github.com/GloriousEggroll/proton-ge-custom/releases"
    warn "Extract into: ${PROTON_DIR}/"
else
    PROTON_NAME="${TAG}"   # e.g. GE-Proton9-27
    INSTALL_PATH="${PROTON_DIR}/${PROTON_NAME}"

    if [[ -d "$INSTALL_PATH" ]]; then
        info "Proton-GE ${TAG} already installed at ${INSTALL_PATH}"
    else
        info "Downloading Proton-GE ${TAG}..."
        mkdir -p "$PROTON_DIR"
        TMP=$(mktemp -d)
        curl -L --progress-bar "$TARBALL_URL" -o "${TMP}/${TAG}.tar.gz"
        info "Extracting..."
        tar -xf "${TMP}/${TAG}.tar.gz" -C "$PROTON_DIR"
        rm -rf "$TMP"
        info "Proton-GE ${TAG} installed to ${INSTALL_PATH}"
    fi
fi

# ---------------------------------------------------------------------------
# Step 4 — i386 multiarch (Steam Runtime container requirement)
# ---------------------------------------------------------------------------
# umu-run/Proton launch the game inside a Steam Runtime container
# (pressure-vessel). That container's library-capture step needs i386 enabled
# to inject the host's real GPU driver — without it DXVK inside the container
# sees zero Vulkan adapters and the game crashes on launch (`DXVK: No adapters
# found`), even though the host GPU works fine outside the container. This is
# required even though MetalStorm itself is a 64-bit game.
info "Checking i386 (32-bit) multiarch support..."
if dpkg --print-foreign-architectures | grep -qx i386; then
    info "i386 architecture already enabled."
else
    info "Enabling i386 architecture..."
    sudo dpkg --add-architecture i386
    sudo apt-get update -qq
fi
if dpkg -s libgl1:i386 mesa-vulkan-drivers:i386 libvulkan1:i386 &>/dev/null; then
    info "32-bit GL/Vulkan libraries already installed."
else
    info "Installing 32-bit GL/Vulkan libraries..."
    if ! sudo apt-get install -y libgl1:i386 mesa-vulkan-drivers:i386 libvulkan1:i386; then
        warn "i386 library install failed with unmet dependencies. Do NOT force it"
        warn "with 'apt --fix-broken install' or '-f' — that can be destructive."
        warn "This usually means either a pending-upgrade backlog (try 'sudo apt"
        warn "upgrade' first) or a missing apt pocket — check 'grep Suites:"
        CODENAME="${VERSION_CODENAME:-<codename>}"
        warn "/etc/apt/sources.list.d/ubuntu.sources' lists all four suites (${CODENAME},"
        warn "${CODENAME}-updates, ${CODENAME}-backports, ${CODENAME}-security). See the"
        warn "troubleshooting section of docs/job-aids/010-run-metalstorm-on-linux.md for the fix."
        die "i386 library install failed — see guidance above, then re-run this script."
    fi
fi

# ---------------------------------------------------------------------------
# Step 5 — uv, Python dependencies, and PyGObject
# ---------------------------------------------------------------------------
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

info "Checking uv..."
if ! command -v uv &>/dev/null; then
    info "Installing uv..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
    # shellcheck disable=SC1091
    source "$HOME/.local/bin/env"
fi

# The same script brings an existing install up to date after an Ubuntu
# upgrade (ADR 154). It also installs python3-tk, but only when the venv sits
# on the system Python, since a uv-managed Python ships its own tkinter.
"${REPO_ROOT}/scripts/upgrade-linux.sh"

# ---------------------------------------------------------------------------
# Manual gate A — Epic Games Store login
# ---------------------------------------------------------------------------
echo ""
info "Launching Heroic..."
flatpak run com.heroicgameslauncher.hgl &>/dev/null &
disown

pause "Log in to Epic Games Store inside Heroic (click Log In → Epic Games Store, complete the browser OAuth), then press Enter"

# ---------------------------------------------------------------------------
# Manual gate B — Install MetalStorm
# ---------------------------------------------------------------------------
pause "In Heroic Library, find MetalStorm and click Install. Wait for it to complete, then launch the game — no Wine or UMU settings changes needed, Heroic defaults work. Press Enter when the game loads"

# ---------------------------------------------------------------------------
# Step 6 — umu-run standalone symlink
# ---------------------------------------------------------------------------
# Heroic bundles its own copy for internal use; there's no separate install
# for it upstream. It only exists once Heroic has actually run/updated a
# umu-based launch, which the manual gate above should have triggered.
UMU_BUNDLED="${HEROIC_CONFIG}/tools/runtimes/umu/umu-run"
info "Checking for umu-run..."
mkdir -p "$HOME/.local/bin"
if [[ -e "$HOME/.local/bin/umu-run" ]]; then
    info "umu-run already present at ~/.local/bin/umu-run."
elif [[ -f "$UMU_BUNDLED" ]]; then
    ln -sf "$UMU_BUNDLED" "$HOME/.local/bin/umu-run"
    info "Symlinked umu-run -> ${UMU_BUNDLED}"
else
    warn "Heroic's bundled umu-run not found at ${UMU_BUNDLED}."
    warn "Launch MetalStorm at least once from Heroic, then re-run this script,"
    warn "or symlink manually once it appears."
fi

# ---------------------------------------------------------------------------
# Done
# ---------------------------------------------------------------------------
echo ""
echo -e "${GRN}${BLD}Setup complete.${RST}"
echo ""
echo "Next steps:"
echo "  • Check what Proton build Heroic actually installed — this script's Proton-GE"
echo "    download should make the Wingman Makefile default (PROTON_ROOT) resolve"
echo "    without an override, but confirm with:"
echo "      ls ${PROTON_DIR}/"
echo "  • If the game crashes instantly with a Xalia/SDL 'No displays available' error,"
echo "    export PROTON_USE_XALIA=0 before launching (see job-aid 010 troubleshooting)."
echo "  • Then run Wingman from the repo root:"
echo "      make preflight   # verify dependencies"
echo "      make g           # launch the game alone first, confirm it comes up windowed"
echo "      make r           # start Wingman"
echo "  • After every Ubuntu release upgrade, and after pulling a dependency change,"
echo "    run 'make upgrade-linux' before anything else (ADR 154)."
echo ""
echo "See docs/job-aids/010-run-metalstorm-on-linux.md for the full troubleshooting table."
