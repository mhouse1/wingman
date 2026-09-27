"""
Performance tracking utilities for test performance trends.

Features:
- Record performance.json snapshots to a local (untracked) history file
- Generate CSV trends over versions
- Visualize performance degradation with charts
"""

import json
import csv
import sys
from pathlib import Path
from datetime import datetime
from typing import Dict, List, Optional


class PerformanceTracker:
    """Track and analyze test performance over time.

    History is local-only: performance.json is no longer tracked in git, so
    each `make wrelease` appends the current snapshot to a JSONL file on the
    machine that ran it (veda). Kept outside tests/test-output/ because
    `make clean` wipes that directory.
    """

    def __init__(self, repo_root: Path = None):
        if repo_root is None:
            repo_root = Path(__file__).resolve().parent.parent
        self.repo_root = repo_root
        self.perf_file = repo_root / "tests" / "test-output" / "performance.json"
        self.history_file = repo_root / "tests" / "perf-history" / "performance-history.jsonl"

    def _read_current(self) -> Optional[Dict]:
        """Read the latest performance.json written by conftest, if any."""
        if not self.perf_file.exists():
            return None
        try:
            with open(self.perf_file, 'r') as f:
                return json.load(f)
        except Exception as e:
            print(f"Warning: Could not read current performance.json: {e}", file=sys.stderr)
            return None

    def load_history(self) -> List[Dict]:
        """Return recorded performance snapshots, oldest first."""
        if not self.history_file.exists():
            return []
        snapshots = []
        with open(self.history_file, 'r') as f:
            for lineno, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    snapshots.append(json.loads(line))
                except json.JSONDecodeError as e:
                    print(f"Warning: skipping malformed history line {lineno}: {e}", file=sys.stderr)
        return snapshots

    def record_current(self) -> bool:
        """Append the current performance.json to the local history.

        Skips a snapshot whose timestamp is already recorded, so re-running
        wrelease after an aborted commit does not duplicate the point.
        """
        current = self._read_current()
        if current is None:
            print(f"No performance.json at {self.perf_file} - run 'make test' first", file=sys.stderr)
            return False
        if any(s.get('timestamp') == current.get('timestamp') for s in self.load_history()):
            print(f"[OK] Snapshot {current.get('timestamp')} already recorded")
            return True
        self.history_file.parent.mkdir(parents=True, exist_ok=True)
        with open(self.history_file, 'a') as f:
            f.write(json.dumps(current) + '\n')
        print(f"[OK] Recorded v{current.get('version', 'unknown')} snapshot to {self.history_file.name}")
        return True

    @staticmethod
    def _rows_for(perf_data: Dict, label: str, fallback_version: str) -> List[Dict]:
        version = perf_data.get('version', fallback_version)
        perf_timestamp = perf_data.get('timestamp', datetime.now().isoformat())
        return [
            {
                'timestamp': perf_timestamp,
                'commit': label,
                'version': version,
                'test': test_name,
                'duration': metrics.get('duration', 0),
                'min': metrics.get('min', 0),
                'max': metrics.get('max', 0),
                'runs': metrics.get('runs', 1)
            }
            for test_name, metrics in perf_data.get('tests', {}).items()
        ]

    def generate_csv_trends(self, output_file: Path = None, include_current: bool = False) -> Path:
        """
        Generate CSV with performance trends over recorded local snapshots.
        Format: timestamp, version, test_name, duration, min, max, runs

        Args:
            output_file: Path to output CSV file
            include_current: If True, append current unrecorded performance.json data
        """
        if output_file is None:
            output_file = Path(__file__).parent / "test-output" / "performance-history.csv"

        output_file.parent.mkdir(parents=True, exist_ok=True)

        history = self.load_history()
        if not history:
            print(f"No recorded snapshots in {self.history_file}", file=sys.stderr)

        rows = []
        for idx, perf_data in enumerate(history):
            rows.extend(self._rows_for(perf_data, f"rec{idx}", 'unknown'))

        # Optionally include current unrecorded data
        if include_current:
            current_data = self._read_current()
            recorded = {s.get('timestamp') for s in history}
            if current_data is not None and current_data.get('timestamp') not in recorded:
                rows.extend(self._rows_for(current_data, 'current', 'uncommitted'))
                print(f"[OK] Included current unrecorded data (v{current_data.get('version', 'uncommitted')})")

        # Write CSV
        if rows:
            with open(output_file, 'w', newline='') as f:
                writer = csv.DictWriter(f, fieldnames=rows[0].keys())
                writer.writeheader()
                writer.writerows(rows)
            print(f"[OK] Generated {output_file.name} with {len(rows)} data points")

        return output_file

    def generate_visualization(self, output_html: Path = None, include_current: bool = False) -> Path:
        """
        Generate HTML visualization of performance trends.
        Requires matplotlib and plotly.
        """
        if output_html is None:
            output_html = Path(__file__).parent / "test-output" / "performance-trends.html"

        output_html.parent.mkdir(parents=True, exist_ok=True)

        try:
            import plotly.graph_objects as go
            import plotly.express  # noqa: F401  — availability probe
            from plotly.subplots import make_subplots
        except ImportError:
            print("❌ plotly not installed. Install with: pip install plotly")
            print("   For now, view trends in CSV: tests/test-output/performance-history.csv")
            return output_html

        # Generate CSV first
        csv_file = self.generate_csv_trends(include_current=include_current)

        # Read CSV data
        try:
            import pandas as pd
            df = pd.read_csv(csv_file)
        except ImportError:
            print("❌ pandas not installed. Install with: pip install pandas")
            return output_html

        if df.empty:
            print("No performance data found")
            return output_html

        # Group by test name and create subplots
        test_names = sorted(df['test'].unique())
        n_tests = len(test_names)

        fig = make_subplots(
            rows=(n_tests + 1) // 2,
            cols=2,
            subplot_titles=test_names,
            specs=[[{'secondary_y': False}] * 2 for _ in range((n_tests + 1) // 2)]
        )

        for idx, test_name in enumerate(test_names):
            test_data = df[df['test'] == test_name].sort_values('timestamp')

            # Use version only for x-axis
            x_labels = test_data['version']

            row = (idx // 2) + 1
            col = (idx % 2) + 1

            # Add line trace for duration
            fig.add_trace(
                go.Scatter(
                    x=x_labels,
                    y=test_data['duration'],
                    name=test_name,
                    mode='lines+markers',
                    line=dict(width=2),
                    hovertemplate='<b>Version: %{x}<br>Duration: %{y:.2f}s</b><extra></extra>'
                ),
                row=row, col=col
            )

            # Add min/max range
            fig.add_trace(
                go.Scatter(
                    x=x_labels,
                    y=test_data['min'],
                    fill=None,
                    mode='lines',
                    line_color='rgba(0,0,0,0)',
                    showlegend=False,
                    hoverinfo='skip'
                ),
                row=row, col=col
            )

            fig.add_trace(
                go.Scatter(
                    x=x_labels,
                    y=test_data['max'],
                    fill='tonexty',
                    mode='lines',
                    line_color='rgba(0,0,0,0)',
                    name=f'{test_name} (min-max range)',
                    fillcolor='rgba(0,100,200,0.1)',
                    hovertemplate='<extra></extra>'
                ),
                row=row, col=col
            )

        # Update layout
        fig.update_layout(
            title_text="Test Performance Trends Over Time",
            height=300 * ((n_tests + 1) // 2),
            showlegend=False,
            hovermode='x unified'
        )

        # Update y-axes
        fig.update_yaxes(title_text="Duration (seconds)", row=1, col=1)

        # Save HTML
        fig.write_html(str(output_html))
        print(f"[OK] Generated {output_html.name}")

        return output_html


def main():
    """CLI for performance tracking."""
    import argparse

    parser = argparse.ArgumentParser(description="Analyze test performance trends")
    parser.add_argument(
        '--csv',
        action='store_true',
        help='Generate CSV from the local performance history'
    )
    parser.add_argument(
        '--record',
        action='store_true',
        help='Append current performance.json to the local history'
    )
    parser.add_argument(
        '--chart',
        action='store_true',
        help='Generate HTML chart visualization'
    )
    parser.add_argument(
        '--all',
        action='store_true',
        help='Generate both CSV and chart'
    )
    parser.add_argument(
        '--include-current',
        action='store_true',
        help='Include current unrecorded performance.json data in output'
    )

    args = parser.parse_args()

    tracker = PerformanceTracker()

    if args.record:
        if not tracker.record_current():
            sys.exit(1)
        if not (args.all or args.csv or args.chart):
            return

    if args.all or (not args.csv and not args.chart):
        # Default: generate both
        tracker.generate_csv_trends(include_current=args.include_current)
        tracker.generate_visualization(include_current=args.include_current)
    else:
        if args.csv:
            tracker.generate_csv_trends(include_current=args.include_current)
        if args.chart:
            tracker.generate_visualization(include_current=args.include_current)


if __name__ == "__main__":
    main()
