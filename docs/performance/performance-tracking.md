# Performance Tracking and Trends

## Overview

The test suite automatically tracks performance metrics over time using three components:

1. **`performance.json`** - Generated after each test run
2. **`performance-history.csv`** - Aggregated trends from git history
3. **`performance-trends.html`** - Interactive visualization

## How It Works

### Step 1: Automatic Performance.json Generation

After each `make test` run, `tests/conftest.py` automatically generates `tests/test-output/performance.json`:

```json
{
  "timestamp": "2026-03-07T02:27:34.123456",
  "version": "1.0.0",
  "tests": {
    "test_level1_static_screenshot": {
      "duration": 16.8,
      "runs": 1,
      "min": 16.8,
      "max": 16.8
    },
    "test_level3_unit_ocr": {
      "duration": 13.5,
      "runs": 1,
      "min": 13.5,
      "max": 13.5
    }
  }
}
```

### Step 2: Commit Performance Snapshots

Include performance.json in version commits:

```bash
make test
git add tests/test-output/performance.json
git commit -m "v1.0.2: performance baseline"
```

Each commit becomes a data point in your performance history.

### Step 3: Generate Trends

Extract performance data from git history:

```bash
# Generate CSV from git history
make test-perf-csv

# Result: tests/test-output/performance-history.csv
```

Example CSV output:
```csv
timestamp,commit,version,test,duration,min,max,runs
2026-03-07T02:27:34.123456,a1b2c3d4,1.0.0,test_level1_static_screenshot,16.8,16.8,16.8,1
2026-03-07T02:27:34.123456,a1b2c3d4,1.0.0,test_level3_unit_ocr,13.5,13.5,13.5,1
2026-03-08T10:15:22.654321,e5f6g7h8,1.0.1,test_level1_static_screenshot,17.2,17.1,17.3,1
2026-03-08T10:15:22.654321,e5f6g7h8,1.0.1,test_level3_unit_ocr,14.1,13.9,14.3,1
```

### Step 4: Visualize Trends

Generate interactive HTML chart:

```bash
make test-perf-chart

# Result: tests/test-output/performance-trends.html
# Open in browser to see performance degradation over versions
```

## Workflow

### Local Development

```bash
# Run tests
make test

# Don't commit performance.json yet - it's just for this run
```

### Before Release

```bash
# Run tests one final time
make test

# Update version in wingman/main.py
# Commit the performance baseline with release
git add tests/test-output/performance.json
git commit -m "v1.0.2: final performance baseline"
git tag v1.0.2

git push origin main
git push origin v1.0.2
```

### Analyzing Performance Trends

```bash
# After several releases, generate trends
make test-perf-csv
make test-perf-chart

# Open tests/test-output/performance-trends.html in browser
```

## CSV Columns

| Column | Meaning |
|--------|---------|
| `timestamp` | ISO 8601 timestamp when tests ran |
| `commit` | Short commit hash (first 8 chars) |
| `version` | Wingman version from that commit |
| `test` | Test function name |
| `duration` | Average test duration (seconds) |
| `min` | Minimum duration from parametrized runs |
| `max` | Maximum duration from parametrized runs |
| `runs` | Number of test runs (for parametrized tests) |

## Requirements

### For CSV Generation
- Git repository with commit history
- Python (standard library only)

### For Chart Visualization
- `plotly` - `pip install plotly`
- `pandas` - `pip install pandas`

If plotly/pandas not installed, CSV is still generated and can be viewed in Excel/Sheets.

## Use Cases

### Detect Performance Regressions
```
v1.0.0: test_level3_unit_ocr = 14.5s
v1.0.1: test_level3_unit_ocr = 16.2s  ⚠️ +1.7s slower
v1.0.2: test_level3_unit_ocr = 23.1s  ⚠️ +6.9s slower (blocked by system issues)
```

### Track Optimization Impact
```
v1.0.2: test_level2_live_capture = 8.3s
[Optimize screen capture...]
v1.0.3: test_level2_live_capture = 6.1s  ✓ 2.2s improvement (26% faster)
```

### Hardware Baseline Comparison
Run same test suite on different machines:
```
MacBook M2:     test_level3 = 9.2s
Linux CI:       test_level3 = 12.1s
Windows laptop: test_level3 = 15.3s
```

## FAQ

**Q: Should I commit performance.json for every test run?**
A: No, only commit it for releases/milestones. Intermediate runs will be lost, which is fine—you only care about version-to-version trends.

**Q: How do I reset the history?**
A: Delete all performance.json commits from git history (git rebase -i), then `make test-perf-csv` will only include remaining commits.

**Q: Can I use this in CI?**
A: Yes! Add to CI config:
```bash
make test
make test-perf-csv
make test-perf-chart
# Commit results to separate 'performance' branch or artifact
```

**Q: How far back does history go?**
A: As far back as you've been committing performance.json files. Git log traces the history automatically.
