# Job Aid: Running Automated Tests via Command Line

This guide explains how to run the automated test suite for the Wingman project using the command line.

## 2. Run the Automated Tests

You can run the automated tests using either direct commands or the provided Makefile targets.

### Option 1: Using Makefile Commands (Recommended)

- To run all automated tests:
  ```sh
  make test
  ```
  This will run the full test suite and generate an HTML report at `tests/test-output/report.html`.

- To run a specific test (see Makefile for more options):
  ```sh
  make test1   # Region 33 OCR test
  make test2   # Region 9 OCR test
  ```

### Option 2: Using Direct Commands

From the project root or the tests/ directory, run:
```
uv run pytest tests/test_automated_levels.py
```

## 3. Generate an HTML Test Report

The `make test` command will automatically generate a detailed HTML report at `tests/test-output/report.html`.

To generate the report manually, run:
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
