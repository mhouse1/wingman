"""
Performance tracking utilities for test performance trends.

Features:
- Extract performance.json from git history
- Generate CSV trends over versions
- Visualize performance degradation with charts
"""

import json
import csv
import subprocess
import sys
from pathlib import Path
from datetime import datetime
from typing import Dict, List, Tuple, Optional


class PerformanceTracker:
    """Track and analyze test performance over time."""
    
    def __init__(self, repo_root: Path = None):
        if repo_root is None:
            repo_root = Path(__file__).resolve().parent.parent
        self.repo_root = repo_root
        self.perf_file_path = "tests/test-output/performance.json"
    
    def get_git_commits_with_file(self) -> List[Tuple[str, str, str]]:
        """
        Get all commits that modified performance.json.
        Returns: List of (commit_hash, version, timestamp)
        """
        try:
            result = subprocess.run(
                ["git", "log", "--follow", "--pretty=format:%H|%ae|%aI", 
                 "--", self.perf_file_path],
                cwd=self.repo_root,
                capture_output=True,
                text=True,
                check=False
            )
            
            commits = []
            for line in result.stdout.strip().split('\n'):
                if not line:
                    continue
                parts = line.split('|')
                if len(parts) >= 3:
                    commits.append((parts[0], parts[1], parts[2]))
            
            return commits
        except Exception as e:
            print(f"Error getting git commits: {e}", file=sys.stderr)
            return []
    
    def get_performance_data_at_commit(self, commit_hash: str) -> Optional[Dict]:
        """Get performance.json content at specific commit."""
        try:
            result = subprocess.run(
                ["git", "show", f"{commit_hash}:{self.perf_file_path}"],
                cwd=self.repo_root,
                capture_output=True,
                text=True,
                check=False
            )
            
            if result.returncode == 0:
                return json.loads(result.stdout)
            return None
        except Exception as e:
            print(f"Error reading performance data: {e}", file=sys.stderr)
            return None
    
    def generate_csv_trends(self, output_file: Path = None) -> Path:
        """
        Generate CSV with performance trends over commits.
        Format: timestamp, version, test_name, duration, min, max, runs
        """
        if output_file is None:
            output_file = Path(__file__).parent / "test-output" / "performance-history.csv"
        
        output_file.parent.mkdir(parents=True, exist_ok=True)
        
        commits = self.get_git_commits_with_file()
        if not commits:
            print("No commits with performance.json found", file=sys.stderr)
            return output_file
        
        rows = []
        for commit_hash, author, timestamp in commits:
            perf_data = self.get_performance_data_at_commit(commit_hash)
            if not perf_data:
                continue
            
            version = perf_data.get('version', 'unknown')
            perf_timestamp = perf_data.get('timestamp', timestamp)
            
            for test_name, metrics in perf_data.get('tests', {}).items():
                rows.append({
                    'timestamp': perf_timestamp,
                    'commit': commit_hash[:8],
                    'version': version,
                    'test': test_name,
                    'duration': metrics.get('duration', 0),
                    'min': metrics.get('min', 0),
                    'max': metrics.get('max', 0),
                    'runs': metrics.get('runs', 1)
                })
        
        # Write CSV
        if rows:
            with open(output_file, 'w', newline='') as f:
                writer = csv.DictWriter(f, fieldnames=rows[0].keys())
                writer.writeheader()
                writer.writerows(rows)
            print(f"✓ Generated {output_file.name} with {len(rows)} data points")
        
        return output_file
    
    def generate_visualization(self, output_html: Path = None) -> Path:
        """
        Generate HTML visualization of performance trends.
        Requires matplotlib and plotly.
        """
        if output_html is None:
            output_html = Path(__file__).parent / "test-output" / "performance-trends.html"
        
        output_html.parent.mkdir(parents=True, exist_ok=True)
        
        try:
            import plotly.graph_objects as go
            import plotly.express as px
            from plotly.subplots import make_subplots
        except ImportError:
            print("❌ plotly not installed. Install with: pip install plotly")
            print("   For now, view trends in CSV: tests/test-output/performance-history.csv")
            return output_html
        
        # Generate CSV first
        csv_file = self.generate_csv_trends()
        
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
        test_names = df['test'].unique()
        n_tests = len(test_names)
        
        fig = make_subplots(
            rows=(n_tests + 1) // 2,
            cols=2,
            subplot_titles=test_names,
            specs=[[{'secondary_y': False}] * 2 for _ in range((n_tests + 1) // 2)]
        )
        
        for idx, test_name in enumerate(sorted(test_names)):
            test_data = df[df['test'] == test_name].sort_values('timestamp')
            
            row = (idx // 2) + 1
            col = (idx % 2) + 1
            
            # Add line trace for duration
            fig.add_trace(
                go.Scatter(
                    x=test_data['timestamp'],
                    y=test_data['duration'],
                    name=test_name,
                    mode='lines+markers',
                    line=dict(width=2),
                    hovertemplate='<b>%{x|%Y-%m-%d %H:%M}</b><br>Duration: %{y:.2f}s<extra></extra>'
                ),
                row=row, col=col
            )
            
            # Add min/max range
            fig.add_trace(
                go.Scatter(
                    x=test_data['timestamp'],
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
                    x=test_data['timestamp'],
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
            showlegend=True,
            hovermode='x unified'
        )
        
        # Update y-axes
        fig.update_yaxes(title_text="Duration (seconds)", row=1, col=1)
        
        # Save HTML
        fig.write_html(str(output_html))
        print(f"✓ Generated {output_html.name}")
        
        return output_html


def main():
    """CLI for performance tracking."""
    import argparse
    
    parser = argparse.ArgumentParser(description="Analyze test performance trends")
    parser.add_argument(
        '--csv',
        action='store_true',
        help='Generate CSV from git history'
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
    
    args = parser.parse_args()
    
    tracker = PerformanceTracker()
    
    if args.all or (not args.csv and not args.chart):
        # Default: generate both
        tracker.generate_csv_trends()
        tracker.generate_visualization()
    else:
        if args.csv:
            tracker.generate_csv_trends()
        if args.chart:
            tracker.generate_visualization()


if __name__ == "__main__":
    main()
