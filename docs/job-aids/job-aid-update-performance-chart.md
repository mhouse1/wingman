# Job Aid: Updating performance-trends.html

This guide explains how to add new data points and update the performance-trends.html chart for the Wingman project.

## Steps to Add a New Data Point

1. **Run the Performance Tests**
   
   Run the full performance test workflow:
   
   ```sh
   make test-perf
   ```
   This will:
   - Run all automated tests
   - Update `tests/test-output/performance.json` with the latest results
   - Generate the CSV and HTML chart files

2. **Commit the New Data Point**
   
   To add the new data point to the chart history, commit the updated performance.json:
   
   ```sh
   make wrelease
   ```
   This will:
   - Force add the new `performance.json` to git (even though it's normally ignored)
   - Commit with the current `WINGMAN_VERSION` and details
   - Regenerate the chart to include the new data point

## Adding Multiple Data Points for the Same Version

You can add multiple data points for the same `WINGMAN_VERSION` (e.g., to track performance across several runs or environments for a single release):

1. **Repeat the workflow:**
   - Run `make test-perf` to generate a new `performance.json` with fresh test results.
   - Run `make wrelease` to commit the new data point. Each commit, even with the same version, is stored as a separate entry in the chart history.

2. **Result:**
   - The chart will show multiple points on the x-axis for the same version label, one for each commit.
   - This is useful for comparing repeated runs, hardware changes, or configuration tweaks within a single release.

**Example:**
```
make test-perf
make wrelease   # First data point for v1.4.2
# ...change test conditions or rerun...
make test-perf
make wrelease   # Second data point for v1.4.2
```
All points will appear under the same version on the chart.

3. **View the Updated Chart**
   
   Open the chart in your browser:
   
   - `tests/test-output/performance-trends.html`

   The x-axis will show the `WINGMAN_VERSION` for each data point. Multiple points for the same version are supported.

## Troubleshooting

- **New data point not showing up?**
  - Make sure you committed `performance.json` (step 2).
  - Refresh the HTML file in your browser.
- **Want to preview uncommitted data?**
  - Run:
    ```sh
    make test-perf-preview
    ```
  - This includes the current (uncommitted) `performance.json` in the chart, but does not add it to history until you commit.

## Summary
- Use `make test-perf` to generate new results.
- Use `make wrelease` to add a new data point to the chart.
- Open `performance-trends.html` to view the chart.

---
For more details, see the Makefile targets or ask the project maintainers.
