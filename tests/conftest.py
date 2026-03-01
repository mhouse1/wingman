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

def pytest_configure(config):
    # Add WINGMAN_VERSION to the pytest-html report metadata
    if hasattr(config, '_metadata'):
        config._metadata['Wingman Version'] = WINGMAN_VERSION

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
