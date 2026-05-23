"""
Runtime performance trend utilities for release and preview charts.

This script aggregates runtime run_*.json files from docs/performance and emits:
- Aggregate CSV artifacts
- Plotly HTML charts (when plotly is available)

Modes:
- release: aggregate release run history only
- preview: release history + overlay of aggregated current runs
"""

import argparse
import csv
import json
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional


CROPS = ("incoming", "respawn", "health", "ammo_flares", "ammo_missiles")


@dataclass
class PointData:
    mode: str
    version: str
    point_label: str
    session_count: int
    incoming_cycles: int
    reaction_events: int
    incoming_mean: float
    respawn_mean: float
    health_mean: float
    ammo_flares_mean: float
    ammo_missiles_mean: float
    reaction_mean: float
    source_run_count: int
    generated_at: str


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _perf_root() -> Path:
    return _repo_root() / "docs" / "performance"


def _load_json(path: Path) -> Optional[dict]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"WARNING: failed to read {path}: {exc}", file=sys.stderr)
        return None


def _version_sort_key(version: str) -> tuple:
    parts = []
    for token in version.split("."):
        if token.isdigit():
            parts.append(int(token))
        else:
            parts.append(token)
    return tuple(parts)


def _mean_weighted(items: List[tuple]) -> float:
    total_weight = sum(weight for _, weight in items)
    if total_weight <= 0:
        return 0.0
    return sum(value * weight for value, weight in items) / total_weight


def _build_points_from_runs(run_files: List[Path], mode: str, current_overlay: bool = False) -> List[PointData]:
    by_version: Dict[str, List[dict]] = defaultdict(list)
    for run_file in run_files:
        data = _load_json(run_file)
        if not data:
            continue
        version = data.get("version", "?")
        by_version[version].append(data)

    generated_at = datetime.now(timezone.utc).isoformat()
    points: List[PointData] = []

    for version in sorted(by_version.keys(), key=_version_sort_key):
        runs = by_version[version]
        session_count = len(runs)
        incoming_cycles = 0
        reaction_events = 0

        crop_means: Dict[str, float] = {}
        for crop in CROPS:
            weighted_values = []
            for run in runs:
                crop_data = run.get("ocr_crops", {}).get(crop)
                if not crop_data:
                    continue
                n = int(crop_data.get("n", 0))
                mean = float(crop_data.get("mean", 0.0))
                if n > 0:
                    weighted_values.append((mean, n))
                    if crop == "incoming":
                        incoming_cycles += n
            crop_means[crop] = _mean_weighted(weighted_values)

        reaction_values = []
        for run in runs:
            reaction = run.get("reaction", {})
            n = int(reaction.get("n", 0))
            mean = float(reaction.get("mean", 0.0))
            if n > 0:
                reaction_values.append((mean, n))
                reaction_events += n
        reaction_mean = _mean_weighted(reaction_values)

        point_label = version + (" (current)" if current_overlay else "")

        points.append(
            PointData(
                mode=mode,
                version=version,
                point_label=point_label,
                session_count=session_count,
                incoming_cycles=incoming_cycles,
                reaction_events=reaction_events,
                incoming_mean=round(crop_means.get("incoming", 0.0), 4),
                respawn_mean=round(crop_means.get("respawn", 0.0), 4),
                health_mean=round(crop_means.get("health", 0.0), 4),
                ammo_flares_mean=round(crop_means.get("ammo_flares", 0.0), 4),
                ammo_missiles_mean=round(crop_means.get("ammo_missiles", 0.0), 4),
                reaction_mean=round(reaction_mean, 4),
                source_run_count=session_count,
                generated_at=generated_at,
            )
        )

    return points


def _write_csv(points: List[PointData], out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "mode",
        "version",
        "point_label",
        "session_count",
        "incoming_cycles",
        "reaction_events",
        "incoming_mean",
        "respawn_mean",
        "health_mean",
        "ammo_flares_mean",
        "ammo_missiles_mean",
        "reaction_mean",
        "source_run_count",
        "generated_at",
    ]
    with out_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for p in points:
            writer.writerow(
                {
                    "mode": p.mode,
                    "version": p.version,
                    "point_label": p.point_label,
                    "session_count": p.session_count,
                    "incoming_cycles": p.incoming_cycles,
                    "reaction_events": p.reaction_events,
                    "incoming_mean": p.incoming_mean,
                    "respawn_mean": p.respawn_mean,
                    "health_mean": p.health_mean,
                    "ammo_flares_mean": p.ammo_flares_mean,
                    "ammo_missiles_mean": p.ammo_missiles_mean,
                    "reaction_mean": p.reaction_mean,
                    "source_run_count": p.source_run_count,
                    "generated_at": p.generated_at,
                }
            )


def _load_csv_rows(csv_path: Path) -> List[dict]:
    if not csv_path.exists():
        return []
    with csv_path.open("r", newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _emit_preview_data_warnings(current_points: List[PointData]) -> None:
    sessions = sum(p.session_count for p in current_points)
    incoming_cycles = sum(p.incoming_cycles for p in current_points)
    if sessions < 5 or incoming_cycles < 1000:
        print(
            "WARNING: runtime preview has thin data "
            f"(sessions={sessions}, incoming_cycles={incoming_cycles}; "
            "recommended >=5 sessions and >=1000 cycles)"
        )


def _build_chart(rows: List[dict], output_html: Path, title: str) -> bool:
    try:
        import plotly.graph_objects as go
        from plotly.subplots import make_subplots
    except ImportError:
        print("WARNING: plotly not installed - skipping runtime performance chart generation")
        return False

    if not rows:
        print("WARNING: no runtime aggregate rows available - chart not generated")
        return False

    output_html.parent.mkdir(parents=True, exist_ok=True)

    x_labels = [r["point_label"] for r in rows]

    metrics = [
        ("incoming_mean", "incoming"),
        ("respawn_mean", "respawn"),
        ("health_mean", "health"),
        ("ammo_flares_mean", "ammo_flares"),
        ("ammo_missiles_mean", "ammo_missiles"),
        ("reaction_mean", "reaction"),
    ]

    fig = make_subplots(rows=3, cols=2, subplot_titles=[m[1] for m in metrics])

    for idx, (col, label) in enumerate(metrics):
        row = (idx // 2) + 1
        col_idx = (idx % 2) + 1

        y = [float(r.get(col, 0.0) or 0.0) for r in rows]
        point_labels = [
            (
                f"c:{int(float(r.get('incoming_cycles', 0) or 0))} "
                f"r:{int(float(r.get('reaction_events', 0) or 0))}"
            )
            for r in rows
        ]
        custom_data = [
            [
                r.get("mode", ""),
                r.get("version", ""),
                int(float(r.get("incoming_cycles", 0) or 0)),
                int(float(r.get("session_count", 0) or 0)),
                int(float(r.get("reaction_events", 0) or 0)),
            ]
            for r in rows
        ]

        fig.add_trace(
            go.Scatter(
                x=x_labels,
                y=y,
                mode="lines+markers",
                name=label,
                text=point_labels,
                textposition="top center",
                textfont={"size": 9},
                customdata=custom_data,
                hovertemplate=(
                    "<b>%{x}</b><br>"
                    "mode=%{customdata[0]}<br>"
                    "version=%{customdata[1]}<br>"
                    "value=%{y:.4f}s<br>"
                    "incoming_cycles=%{customdata[2]}<br>"
                    "session_count=%{customdata[3]}<br>"
                    "reaction_events=%{customdata[4]}<extra></extra>"
                ),
            ),
            row=row,
            col=col_idx,
        )

    fig.update_layout(
        title_text=title,
        height=950,
        hovermode="x unified",
        showlegend=False,
    )

    fig.write_html(str(output_html))
    print(f"[OK] Generated {output_html}")
    return True


def _release_mode(generate_csv: bool, generate_chart: bool) -> int:
    perf_root = _perf_root()
    release_files = sorted((perf_root / "release").glob("run_*.json"))

    points = _build_points_from_runs(release_files, mode="release", current_overlay=False)
    release_csv = perf_root / "release" / "runtime-performance-history.csv"

    if generate_csv:
        _write_csv(points, release_csv)
        print(f"[OK] Generated {release_csv}")

    if generate_chart:
        rows = _load_csv_rows(release_csv)
        _build_chart(rows, perf_root / "runtime-performance-trends.html", "Runtime Performance Trends (Release)")

    return 0


def _preview_mode(generate_csv: bool, generate_chart: bool) -> int:
    perf_root = _perf_root()
    release_files = sorted((perf_root / "release").glob("run_*.json"))
    current_files = sorted((perf_root / "current").glob("run_*.json"))

    release_points = _build_points_from_runs(release_files, mode="release", current_overlay=False)
    current_points = _build_points_from_runs(current_files, mode="preview", current_overlay=True)

    _emit_preview_data_warnings(current_points)

    preview_rows = release_points + current_points
    # Sort by version key, then keep release before preview for same version.
    preview_rows.sort(key=lambda p: (_version_sort_key(p.version), 1 if p.mode == "preview" else 0))

    preview_csv = perf_root / "current" / "runtime-performance-preview.csv"
    if generate_csv:
        _write_csv(preview_rows, preview_csv)
        print(f"[OK] Generated {preview_csv}")

    if generate_chart:
        rows = _load_csv_rows(preview_csv)
        _build_chart(rows, perf_root / "runtime-performance-trends.preview.html", "Runtime Performance Trends (Preview)")

    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate runtime performance aggregates and charts")
    parser.add_argument("--mode", choices=["release", "preview"], required=True)
    parser.add_argument("--csv", action="store_true", help="Generate aggregate CSV artifact")
    parser.add_argument("--chart", action="store_true", help="Generate chart HTML")
    parser.add_argument("--all", action="store_true", help="Generate both CSV and chart")
    args = parser.parse_args()

    generate_csv = args.csv or args.all or (not args.csv and not args.chart)
    generate_chart = args.chart or args.all or (not args.csv and not args.chart)

    if args.mode == "release":
        return _release_mode(generate_csv, generate_chart)
    return _preview_mode(generate_csv, generate_chart)


if __name__ == "__main__":
    raise SystemExit(main())
