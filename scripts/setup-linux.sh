#!/usr/bin/env bash
# setup-linux.sh — Install and configure MetalStorm + Wingman on Ubuntu/Linux.
#
# Automates:
#   1. Flatpak + Heroic Games Launcher install
#   2. Proton-GE-latest download into Heroic's tools directory
#   3. User added to 'input' group (for Wingman key injection)
#
# Requires manual steps (script pauses and prompts):
#   - Epic Games Store login (OAuth in browser)
#   - MetalStorm install in Heroic UI
#   No per-game Wine/UMU configuration needed — Heroic defaults work out of the box.

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
# Step 4 — input group (Wingman key injection)
# ---------------------------------------------------------------------------
info "Checking 'input' group membership..."
if groups | grep -qw input; then
    info "Already in 'input' group."
else
    warn "Adding ${USER} to 'input' group (required for Wingman key injection)..."
    sudo usermod -aG input "$USER"
    warn "Group change takes effect after you log out and back in."
fi

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
# Done
# ---------------------------------------------------------------------------
echo ""
echo -e "${GRN}${BLD}Setup complete.${RST}"
echo ""
echo "Next steps:"
echo "  • Launch MetalStorm from Heroic and confirm the game loads with keyboard/mouse working."
echo "  • Then run Wingman from the repo root:"
echo "      make preflight   # verify dependencies + group membership"
echo "      make r           # start Wingman"
echo ""
if ! groups | grep -qw input; then
    warn "Remember: log out and back in for the 'input' group change to take effect before running Wingman."
fi
