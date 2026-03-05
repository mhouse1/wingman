# Job Aid: Running Automated Tests via Command Line

This guide explains how to run the automated test suite for the Wingman project using the command line.

## 2. Run the Automated Tests
From the project root or the tests/ directory, run:
```
uv run pytest tests/test_automated_levels.py
```

## 3. Generate an HTML Test Report
To create a detailed HTML report of the test results:
```
uv run pytest tests/test_automated_levels.py --html=tests/test-output/report.html --self-contained-html
```
- The report will be saved at `tests/test-output/report.html`.
- Open this file in your browser to view the results.

## 4. Test Output
- The test suite will print results to the terminal.
- If any test fails, review the error message for details.

## 5. Troubleshooting
- Ensure all required test images (e.g., `RESPAWN.png`, `RESPAWNB.png`) are in the `test_screenshots/` folder.
- If you see import errors, make sure you are running from the correct directory and your environment is activated.
- For missing dependencies, run:
```
uv pip install -r requirements.txt
```

---
For more details, see the project README or contact the project maintainer.
