import pytest
import sys
import os
from pathlib import Path
import base64

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
