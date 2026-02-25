#!/usr/bin/env python3
"""Initialize pre-boundary schema and run completeness checks.

Usage:
  python -m data.fetchers.init_preboundary_schema
  python -m data.fetchers.init_preboundary_schema --check-epoch 1771372800
"""

import argparse
import json
import sqlite3

from rich.console import Console
from rich.table import Table

from config.settings import DATABASE_PATH
from src.preboundary_store import (
    ensure_preboundary_tables,
    get_preboundary_completeness,
    get_truth_label_coverage,
    upsert_truth_labels_from_boundary,
)

console = Console()


def main() -> None:
    parser = argparse.ArgumentParser(description="Initialize pre-boundary schema")
    parser.add_argument("--check-epoch", type=int, default=None, help="Optional epoch to run completeness checks")
    parser.add_argument("--vote-epoch", type=int, default=None, help="Vote epoch used for truth-label sync/check")
    parser.add_argument(
        "--sync-truth-labels",
        action="store_true",
        help="Materialize preboundary_truth_labels from boundary tables for --check-epoch/--vote-epoch",
    )
    args = parser.parse_args()

    conn = sqlite3.connect(DATABASE_PATH)

    console.print("[cyan]Initializing pre-boundary tables and indexes...[/cyan]")
    ensure_preboundary_tables(conn)
    console.print("[green]✅ Schema ready[/green]")

    if args.check_epoch is not None:
        summary = get_preboundary_completeness(conn, int(args.check_epoch))

        table = Table(show_header=True, header_style="bold cyan")
        table.add_column("Check", width=28)
        table.add_column("Value", width=60)
        table.add_row("Epoch", str(summary["epoch"]))
        table.add_row("Snapshots complete", str(summary["snapshots_complete"]))
        table.add_row("Forecasts complete", str(summary["forecasts_complete"]))
        table.add_row("Recommendations complete", str(summary["recommendations_complete"]))
        table.add_row("Epoch complete", str(summary["epoch_complete"]))
        table.add_row("Snapshot windows", ", ".join(summary["snapshots_windows_present"]) or "-")
        table.add_row("Forecast windows", ", ".join(summary["forecasts_windows_present"]) or "-")
        table.add_row("Recommendation windows", ", ".join(summary["recommendations_windows_present"]) or "-")
        table.add_row("Forecast scenario counts", json.dumps(summary["forecast_scenario_counts"], sort_keys=True))

        console.print()
        console.print(table)

        if args.vote_epoch is not None and args.sync_truth_labels:
            inserted = upsert_truth_labels_from_boundary(conn, int(args.check_epoch), int(args.vote_epoch), active_only=1)
            coverage = get_truth_label_coverage(conn, int(args.check_epoch), int(args.vote_epoch))

            truth_table = Table(show_header=True, header_style="bold green")
            truth_table.add_column("Truth Labels", width=28)
            truth_table.add_column("Value", width=60)
            truth_table.add_row("Rows upserted", str(inserted))
            truth_table.add_row("Label rows", str(coverage["truth_label_rows"]))
            truth_table.add_row("Boundary gauge rows", str(coverage["boundary_gauge_rows"]))
            truth_table.add_row("Coverage ratio", f"{coverage['coverage_ratio']:.3f}")
            truth_table.add_row("Labels complete", str(coverage["labels_complete"]))

            console.print()
            console.print(truth_table)

    conn.close()


if __name__ == "__main__":
    main()
