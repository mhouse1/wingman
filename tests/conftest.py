import pytest
import sys
import os

# Import WINGMAN_VERSION from main module
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
try:
    from wingman.main import WINGMAN_VERSION
except Exception:
    WINGMAN_VERSION = "unknown"

def pytest_configure(config):
    # Add WINGMAN_VERSION to the pytest-html report metadata
    if hasattr(config, '_metadata'):
        config._metadata['Wingman Version'] = WINGMAN_VERSION

@pytest.hookimpl(optionalhook=True)
def pytest_html_results_summary(prefix, summary, postfix):
    prefix.extend([f"Wingman Version: {WINGMAN_VERSION}"])
