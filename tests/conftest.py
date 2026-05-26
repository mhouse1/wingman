import pytest
import sys
import os
import warnings
import re
from pathlib import Path
import base64
import json
from datetime import datetime

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

def pytest_configure(config):
    # Add WINGMAN_VERSION to the pytest-html report metadata
    if hasattr(config, '_metadata'):
        config._metadata['Wingman Version'] = WINGMAN_VERSION

@pytest.fixture(scope='session')
def strict_timing(request):
    """Fixture to provide strict timing mode flag."""
    return request.config.getoption("--strict-timing")

@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    """Collect test timing data for performance validation."""
    outcome = yield
    report = outcome.get_result()
    
    # Collect timing for the actual test call (not setup/teardown)
    if call.when == "call":
        test_name = item.name.split('[')[0]  # Remove parametrize suffix
        if test_name not in _test_timings:
            _test_timings[test_name] = []
        _test_timings[test_name].append(call.duration)

def pytest_sessionfinish(session, exitstatus):
    """Generate performance.json at end of test session."""
    output_dir = Path(__file__).parent / "test-output"
    output_dir.mkdir(parents=True, exist_ok=True)
    
    performance_data = {
        'timestamp': datetime.now().isoformat(),
        'version': WINGMAN_VERSION,
        'tests': {}
    }
    
    # Calculate average duration for each test (handle parametrized tests)
    for test_name, durations in _test_timings.items():
        avg_duration = sum(durations) / len(durations)
        performance_data['tests'][test_name] = {
            'duration': avg_duration,
            'runs': len(durations),
            'min': min(durations),
            'max': max(durations)
        }
    
    # Write performance.json
    perf_file = output_dir / "performance.json"
    try:
        with open(perf_file, 'w') as f:
            json.dump(performance_data, f, indent=2)
    except Exception as e:
        print(f"Warning: Could not write performance.json: {e}")


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
            except Exception as e:
                pass
    
    if images_html:
        # Add images section to postfix (bottom of summary)
        postfix.extend([
            '<h3 style="margin-top: 30px;">OCR Debug Images</h3>',
            images_html
        ])
