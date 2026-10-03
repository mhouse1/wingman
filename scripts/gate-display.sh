# shellcheck shell=sh
# gate-display.sh — run a gate lane on a private Xvfb display (ADR 153).
#
# Sourced, not executed, by the Makefile's ADR 044/045 lane recipes:
#
#   . scripts/gate-display.sh; \
#   <commands that need a display>
#
# Starts Xvfb on a free display number, points every display consumer at it
# (DISPLAY for injection, capture, the presenter and hotkey observation;
# XDG_SESSION_TYPE=x11 so Capture picks mss rather than the PipeWire portal),
# and stops it when the recipe's shell exits.
#
# Why: GNOME 50 (Ubuntu 26.04) starts the session's Xwayland with
# -enable-ei-portal, so every X client that injects through XTest on :0 raises
# a "Remote Desktop — Allow remote interaction" dialog, one per X connection
# and never remembered. The lanes' clicks and keys used to land on :0. Xvfb has
# no portal, and the gates no longer take over the operator's screen.
#
# No-op off Linux, and when WINGMAN_GATE_DISPLAY is already set (a nested make,
# or WINGMAN_GATE_DISPLAY=real to drive the session display on purpose).
#
# Xvfb gets no -auth, like the nested Xwayland in scripts/nested-display.py:
# input_linux copies the session's cookie for whatever DISPLAY is, and a server
# without authorization accepts local clients regardless.

if [ -z "${WINGMAN_GATE_DISPLAY:-}" ] && [ "$(uname -s)" = "Linux" ]; then
    if ! command -v Xvfb >/dev/null 2>&1; then
        echo "ERROR: Xvfb is not installed. The gate lanes run on a private display" \
             "(ADR 153); run: make upgrade-linux" >&2
        exit 1
    fi
    _gate_fd_file="$(mktemp)"
    # 1920x1200 matches region in wingman/config.yaml, which the presenter
    # fills and the capture is pinned to; the Dockerfile's xvfb-run uses the
    # same size for the same reason. Keep them in step.
    Xvfb -displayfd 3 -nolisten tcp -screen 0 1920x1200x24 \
        3>"$_gate_fd_file" >/dev/null 2>&1 &
    _gate_xvfb_pid=$!
    _gate_cleanup() {
        kill "$_gate_xvfb_pid" 2>/dev/null
        rm -f "$_gate_fd_file"
    }
    trap _gate_cleanup EXIT
    trap '_gate_cleanup; exit 130' INT TERM
    # Xvfb writes its display number to the fd once it accepts connections.
    _gate_tries=0
    while [ ! -s "$_gate_fd_file" ]; do
        if ! kill -0 "$_gate_xvfb_pid" 2>/dev/null || [ "$_gate_tries" -ge 100 ]; then
            echo "ERROR: Xvfb did not start within 10s" >&2
            exit 1
        fi
        sleep 0.1
        _gate_tries=$((_gate_tries + 1))
    done
    WINGMAN_GATE_DISPLAY=":$(head -n 1 "$_gate_fd_file")"
    DISPLAY="$WINGMAN_GATE_DISPLAY"
    XDG_SESSION_TYPE=x11
    export WINGMAN_GATE_DISPLAY DISPLAY XDG_SESSION_TYPE
    unset WAYLAND_DISPLAY
    echo "Gate display: private Xvfb on $DISPLAY (ADR 153)"
fi
