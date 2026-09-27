# Test image: the locked Python environment plus a virtual X display (Xvfb),
# so `make test` runs unchanged anywhere Docker runs. No host X server needed,
# and no test has to be rewritten to avoid one.
#
#   make docker-test                         # build (cached), then make test
#   make docker-test DOCKER_CMD="make lint"  # any other target
#   make docker-shell                        # interactive shell, same setup
#
# The image holds dependencies only. The checkout is bind-mounted at /work at
# run time (see the docker-* targets in the Makefile), so the image rebuilds
# only when pyproject.toml, uv.lock or .python-version change.
#
# Ubuntu 24.04 to match the dev host (scripts/setup-linux.sh): its python3 is
# the 3.12 that .python-version pins, and python3-tk only exists as an OS
# package - it cannot come from the lock.
#
# Where Docker Hub's anonymous pull limit bites (shared-IP CI runners, cloud
# sandboxes: "429 Too Many Requests"), point this at Google's byte-identical
# mirror of the official image:
#   make docker-test DOCKER_BUILD_ARGS="--build-arg BASE_IMAGE=mirror.gcr.io/library/ubuntu:24.04"
ARG BASE_IMAGE=ubuntu:24.04
FROM ${BASE_IMAGE}

# OS packages the suite needs beyond the lock:
#   python3 python3-tk python3-venv  interpreter; tkinter for tests/calibrate.py
#   xvfb xauth                       the virtual X server (xvfb-run needs xauth)
#   libx11-6 libxrandr2 libxfixes3   loaded by mss through ctypes at capture time
#   libgl1 libglib2.0-0t64           loaded by opencv-python at import
#   x11-utils                        xprop / xdpyinfo / xwininfo (focus guard, capture)
#   git make procps tini             Makefile, git-aware tests, pgrep, PID 1
# The venv sits on the system Python, as on the dev host (CLAUDE.md, "Python
# Environment"). Of that section's two apt bindings only python3-tk is here:
# python3-gi and GStreamer serve the PipeWire capture backend, which runs only
# on a Wayland session, and no test imports gi.
ARG DEBIAN_FRONTEND=noninteractive
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ca-certificates git make procps tini \
      python3 python3-tk python3-venv \
      xvfb xauth x11-utils libx11-6 libxrandr2 libxfixes3 \
      libgl1 libglib2.0-0t64 \
 && rm -rf /var/lib/apt/lists/* \
 && install -d -m 1777 /tmp/.X11-unix

# The venv lives outside /work so the bind-mounted checkout, and any host .venv
# inside it, cannot shadow it. The Makefile's `uv run --active` finds it through
# VIRTUAL_ENV; plain `uv run` (lint, reqs-gate) through UV_PROJECT_ENVIRONMENT.
# Bytecode is compiled at build time because a non-root --user cannot write
# __pycache__ into /opt/venv, and every subprocess the tests spawn would
# otherwise recompile torch from source. EASYOCR_MODULE_PATH is where EasyOCR
# looks for its weights instead of ~/.EasyOCR; they are fetched below.
ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    VIRTUAL_ENV=/opt/venv \
    PATH=/opt/venv/bin:$PATH \
    UV_PYTHON_DOWNLOADS=never \
    UV_PYTHON_PREFERENCE=only-system \
    UV_COMPILE_BYTECODE=1 \
    EASYOCR_MODULE_PATH=/opt/easyocr

# Everything fetched over HTTPS happens in this one step:
#   - uv from PyPI, not ghcr.io/astral-sh/uv, because PyPI is reachable from
#     more restricted networks (Claude Code cloud sandboxes block ghcr.io's
#     blob host). pip only bootstraps uv, into its own /opt/uv venv; every
#     project package still comes from uv.lock through `uv sync`.
#   - the locked environment (~4 GB of wheels, most of it CUDA torch). No uv
#     cache is kept: a BuildKit cache mount would pin another ~4.5 GB of disk
#     to save one re-download per lock change, and disk is the scarcer resource
#     on CI runners and cloud sandboxes.
#   - EasyOCR's detection and recognition weights (~100 MB), which `make test`
#     otherwise downloads on first use. Baked in, test runs need no network.
#
# TLS-intercepting networks (corporate proxies, Claude Code cloud sandboxes)
# re-sign HTTPS with a CA this image does not trust, and every fetch here fails
# with CERTIFICATE_VERIFY_FAILED. Hand that CA in as the optional build secret
# `extra_ca` (the Makefile passes the host's $SSL_CERT_FILE when it is set). It
# is appended to a throwaway copy of the system bundle for this step only and
# never lands in a layer. Without it the step runs unchanged. apt needs no such
# help: the Ubuntu archive is plain HTTP.
ARG UV_VERSION=0.8.17
WORKDIR /work
COPY pyproject.toml uv.lock .python-version ./
RUN --mount=type=secret,id=extra_ca \
    if [ -s /run/secrets/extra_ca ]; then \
      cat /etc/ssl/certs/ca-certificates.crt /run/secrets/extra_ca > /tmp/ca.pem; \
      export SSL_CERT_FILE=/tmp/ca.pem PIP_CERT=/tmp/ca.pem; \
    fi \
 && python3 -m venv /opt/uv \
 && /opt/uv/bin/pip install --no-cache-dir "uv==${UV_VERSION}" \
 && ln -s /opt/uv/bin/uv /usr/local/bin/uv \
 && uv sync --frozen --all-groups --no-install-project --no-cache \
 && python -c "import easyocr; easyocr.Reader(['en'], gpu=False, verbose=False)" \
 && rm -f /tmp/ca.pem

# Run against exactly the lock the image was built from; never re-sync.
ENV UV_NO_SYNC=1

# tini reaps children and forwards Ctrl-C to the whole process group (-g), so an
# interrupted run stops pytest instead of orphaning it. xvfb-run gives every
# command its own X server, sized to the capture region in wingman/config.yaml
# (region and nested.size are both 1920x1200). Keep them in step: on xvfb-run's
# default 1280x1024 screen, get_frame() returns None and the two live-capture
# tests in test_automated_levels.py skip as "MetalStorm not running" instead of
# passing - a coverage loss that reads like an ordinary skip.
ENTRYPOINT ["tini", "-g", "--", "xvfb-run", "--auto-servernum", "--server-args=-screen 0 1920x1200x24"]
CMD ["make", "test"]
