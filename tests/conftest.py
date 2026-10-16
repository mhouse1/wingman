import pytest
import sys
import os
import warnings
import re
import select
import shutil
import subprocess
import time
from pathlib import Path
import base64

_SUPPRESSED_WARNING_PATTERNS = [
    re.compile(r"torch\\.ao\\.quantization is deprecated and will be removed in 2\\.10\\.", re.IGNORECASE),
    re.compile(r"'pin_memory' argument is set as true but no accelerator is found", re.IGNORECASE),
]

_ORIGINAL_SHOWWARNING = warnings.showwarning


def _wingman_showwarning(message, category, filename, lineno, file=None, line=None):
    text = str(message)
    if any(pattern.search(text) for pattern in _SUPPRESSED_WARNING_PATTERNS):
        return
    _ORIGINAL_SHOWWARNING(message, category, filename, lineno, file=file, line=line)


warnings.showwarning = _wingman_showwarning

_pin_memory_filter = "ignore:'pin_memory' argument is set as true but no accelerator is found.*:UserWarning"
existing_warning_filters = os.environ.get("PYTHONWARNINGS", "")
if _pin_memory_filter not in existing_warning_filters:
    os.environ["PYTHONWARNINGS"] = (
        f"{existing_warning_filters},{_pin_memory_filter}" if existing_warning_filters else _pin_memory_filter
    )

warnings.filterwarnings(
    "ignore",
    message=r"torch\\.ao\\.quantization is deprecated and will be removed in 2\\.10\\..*",
    category=DeprecationWarning,
    module=r"easyocr\\.detection",
)
warnings.filterwarnings(
    "ignore",
    message=r"torch\\.ao\\.quantization is deprecated and will be removed in 2\\.10\\..*",
    category=DeprecationWarning,
    module=r"easyocr\\.recognition",
)
warnings.filterwarnings(
    "ignore",
    message=r"torch\\.ao\\.quantization is deprecated and will be removed in 2\\.10\\..*",
    category=DeprecationWarning,
    module=r"torch\\.ao\\.quantization\\.quantize",
)
warnings.filterwarnings(
    "ignore",
    message=r"'pin_memory' argument is set as true but no accelerator is found.*",
    category=UserWarning,
)

# Import WINGMAN_VERSION from main module
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
try:
    from wingman.main import WINGMAN_VERSION
except Exception:
    WINGMAN_VERSION = "unknown"

# Session-scoped dictionary to collect test timing data
_test_timings = {}

def pytest_addoption(parser):
    """Add custom pytest options."""
    parser.addoption(
        "--strict-timing",
        action="store_true",
        default=False,
        help="Fail tests on timing violations (default: warnings only)"
    )

# The Xvfb this pytest process started, if any (_start_private_display).
_private_display = None


def _start_private_display():
    """Run the suite on a private Xvfb, as `make test` does (ADR 153).

    GNOME 50 raises an "Allow remote interaction" dialog for every XTest client
    on the session display, and pytest_sessionfinish sends XTest key releases.
    `make test` sources scripts/gate-display.sh first; a direct
    `uv run --active pytest tests/test_x.py` used to stay on :0 and prompt.
    No-op off Linux, without Xvfb, and when WINGMAN_GATE_DISPLAY is already set
    (make test, a gate lane, or WINGMAN_GATE_DISPLAY=real).
    """
    global _private_display
    if (os.environ.get("WINGMAN_GATE_DISPLAY") or not sys.platform.startswith("linux")
            or shutil.which("Xvfb") is None):
        return
    read_fd, write_fd = os.pipe()
    # Same screen as scripts/gate-display.sh; keep them in step.
    proc = subprocess.Popen(
        ["Xvfb", "-displayfd", str(write_fd), "-nolisten", "tcp",
         "-screen", "0", "1920x1200x24"],
        pass_fds=(write_fd,), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    os.close(write_fd)
    # Xvfb writes its display number to the fd once it accepts connections.
    number = b""
    deadline = time.monotonic() + 10.0
    with os.fdopen(read_fd, "rb", buffering=0) as pipe:
        while not number.endswith(b"\n"):
            ready, _, _ = select.select([pipe], [], [], max(deadline - time.monotonic(), 0))
            chunk = pipe.read(16) if ready else b""
            if not chunk:
                proc.kill()
                proc.wait()
                raise pytest.UsageError("Xvfb did not start within 10s (ADR 153)")
            number += chunk
    _private_display = proc
    display = f":{number.decode().strip()}"
    # XDG_SESSION_TYPE=x11 so Capture picks mss rather than the PipeWire portal.
    os.environ.update(WINGMAN_GATE_DISPLAY=display, DISPLAY=display, XDG_SESSION_TYPE="x11")
    os.environ.pop("WAYLAND_DISPLAY", None)


def pytest_configure(config):
    _start_private_display()
    # Add WINGMAN_VERSION to the pytest-html report metadata
    if hasattr(config, '_metadata'):
        config._metadata['Wingman Version'] = WINGMAN_VERSION


def pytest_report_header(config):
    if _private_display is not None:
        return f"display: private Xvfb on {os.environ['DISPLAY']} (ADR 153)"
    return None


def pytest_unconfigure(config):
    if _private_display is not None:
        _private_display.terminate()
        try:
            _private_display.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _private_display.kill()

@pytest.fixture(scope='session')
def strict_timing(request):
    """Fixture to provide strict timing mode flag."""
    return request.config.getoption("--strict-timing")

@pytest.fixture(autouse=True)
def _fresh_actuator_leases():
    """CR-018-09 Phase 2: the module-level Actuator records who holds which key,
    and for the throttle that record decides whether a release lifts the key. A
    lease left by one test must not keep the next test's throttle down."""
    from wingman import controller as _controller
    _controller._actuator._held.clear()
    yield
    _controller._actuator._held.clear()


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    """Collect test timing data for performance validation."""
    outcome = yield
    outcome.get_result()   # called for its side effect; the report itself is unused

    # Collect timing for the actual test call (not setup/teardown)
    if call.when == "call":
        test_name = item.name.split('[')[0]  # Remove parametrize suffix
        if test_name not in _test_timings:
            _test_timings[test_name] = []
        _test_timings[test_name].append(call.duration)

def _release_all_injectable_keys():
    """Safety net: unstick any XTest key a test may have left held.

    XTest key state lives in the X SERVER and survives this process — a test
    that presses a real key and dies before releasing leaves the server
    auto-repeating it into whatever window is focused ('iiiii…', observed
    2026-08-14 after a make test run). The leak vector: tests neutralize
    wingman.controller.keyboard_module via monkeypatch, but monkeypatch
    reverts at TEST teardown while mission daemon threads keep running — a
    thread that reaches a key press after the revert presses the REAL key,
    and pytest exiting mid-hold (or mid-release: X round-trips take seconds
    under load) latches it. Releasing an un-pressed key is a no-op, so this
    releases everything injectable, unconditionally, at session end.
    """
    try:
        from wingman.controller import _ensure_xauthority, _XKEY_ALIASES
        _ensure_xauthority()
        from Xlib import display as _xdisplay, XK as _XK
        from Xlib.ext import xtest as _xtest
        import Xlib.X as _X
        d = _xdisplay.Display()
        try:
            for key in ("i", "j", "k", "l", "e", "d", "w", "space", "a", "f",
                        "p", "q", ";", "u", "y", "x", "g",
                        "up", "down", "left", "right"):
                name = _XKEY_ALIASES.get(key, key)
                ks = _XK.string_to_keysym(name)
                kc = d.keysym_to_keycode(ks) if ks else 0
                if kc:
                    _xtest.fake_input(d, _X.KeyRelease, kc)
            d.sync()
        finally:
            d.close()
    except Exception:
        pass  # headless CI / no X — nothing to unstick


def pytest_sessionfinish(session, exitstatus):
    """Release any injected key a test left held."""
    _release_all_injectable_keys()


@pytest.fixture(scope='session')
def test_timings():
    """Provide access to collected test timing data."""
    return _test_timings


@pytest.hookimpl(optionalhook=True)
def pytest_html_results_summary(prefix, summary, postfix):
    prefix.extend([f"Wingman Version: {WINGMAN_VERSION}"])

    # Add debug OCR images to the summary section
    test_output_dir = Path(__file__).parent / "test-output"
    debug_images = [
        ("debug_ocr_grayscale.png", "OCR Grayscale Conversion"),
        ("debug_ocr_binary.png", "OCR Binary Thresholding"),
        ("debug_ocr_downscaled.png", "OCR Downscaled for Recognition"),
    ]

    # Check if any debug images exist
    images_html = ""
    for filename, label in debug_images:
        image_path = test_output_dir / filename
        if image_path.exists():
            try:
                # Read image and encode as base64
                with open(image_path, 'rb') as f:
                    img_data = base64.b64encode(f.read()).decode('utf-8')
                images_html += f'<div style="margin: 20px 0;"><h4>{label}</h4><img src="data:image/png;base64,{img_data}" style="max-width: 600px; border: 1px solid #ddd;"></div>'
            except Exception:
                pass

    if images_html:
        # Add images section to postfix (bottom of summary)
        postfix.extend([
            '<h3 style="margin-top: 30px;">OCR Debug Images</h3>',
            images_html
        ])
