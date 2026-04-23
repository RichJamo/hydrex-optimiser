#!/usr/bin/env python3
"""Export the boundary-optimal allocation for a single epoch."""

import argparse
import csv
import logging
import os
import sqlite3
import sys
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.preboundary_epoch_review import (
    auto_select_k,
    load_boundary_states,
    load_executed_votes,
    subtract_executed_votes,
)


logger = logging.getLogger(__name__)
console = Console()


def configure_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(level=level, format="%(asctime)s | %(levelname)s | %(message)s")


def resolve_k_from_review_csv(review_csv: Path, epoch: int) -> Optional[int]:
    if not review_csv.exists():
        return None

    with review_csv.open("r", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            try:
                if int(row.get("epoch", "0") or 0) != int(epoch):
                    continue
                resolved = int(row.get("boundary_opt_k", "0") or 0)
                return resolved if resolved > 0 else None
            except (TypeError, ValueError):
                continue
    return None


def build_output_path(output_csv: str, epoch: int, top_k: int) -> Path:
    if output_csv:
        return Path(output_csv)
    return Path("analysis/pre_boundary") / f"epoch_{epoch}_boundary_opt_alloc_k{top_k}.csv"


def write_allocation_csv(path: Path, allocation) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["rank", "pool", "votes", "expected_usd"])
        for index, row in enumerate(allocation, start=1):
            writer.writerow([index, row.pool, int(row.alloc_votes), f"{row.expected_usd:.6f}"])


def render_summary(epoch: int, top_k: int, best_expected: float, allocation, top_n_summary: int) -> None:
    summary_n = max(1, min(int(top_n_summary), len(allocation)))
    top_rows = allocation[:summary_n]
    cumulative_votes = sum(int(row.alloc_votes) for row in top_rows)
    cumulative_expected = sum(float(row.expected_usd) for row in top_rows)

    console.print(
        Panel.fit(
            f"epoch={epoch} | best_k={top_k} | expected_return=${best_expected:.2f}",
            title="Boundary Optimal Allocation",
            border_style="cyan",
        )
    )

    table = Table(title=f"Top {summary_n} Pools", header_style="bold cyan")
    table.add_column("Rank", justify="right")
    table.add_column("Pool")
    table.add_column("Votes", justify="right")
    table.add_column("Expected USD", justify="right")
    table.add_column("Cumulative USD", justify="right")

    running_expected = 0.0
    for index, row in enumerate(top_rows, start=1):
        running_expected += float(row.expected_usd)
        table.add_row(
            str(index),
            str(row.pool),
            f"{int(row.alloc_votes):,}",
            f"${float(row.expected_usd):,.2f}",
            f"${running_expected:,.2f}",
        )

    console.print(table)
    console.print(
        f"Top {summary_n} cumulative votes={cumulative_votes:,} | cumulative expected=${cumulative_expected:,.2f}"
    )


def main() -> None:
    load_dotenv()

    parser = argparse.ArgumentParser(
        description="Export the boundary-optimal allocation CSV and top-N summary for a single epoch"
    )
    parser.add_argument("--epoch", type=int, required=True, help="Target epoch timestamp")
    parser.add_argument("--db-path", default="data/db/data.db", help="Main DB path")
    parser.add_argument(
        "--review-csv",
        default="analysis/pre_boundary/epoch_boundary_vs_t1_review_all.csv",
        help="Review CSV used to resolve boundary_opt_k when --top-k is omitted",
    )
    parser.add_argument(
        "--output-csv",
        default="",
        help="Allocation output CSV path; default analysis/pre_boundary/epoch_<epoch>_boundary_opt_alloc_k<k>.csv",
    )
    parser.add_argument(
        "--voting-power",
        type=int,
        default=int(os.getenv("YOUR_VOTING_POWER", "0")),
        help="Voting power used for allocation",
    )
    parser.add_argument("--candidate-pools", type=int, default=60, help="Candidate pool cap")
    parser.add_argument(
        "--min-votes-per-pool",
        type=int,
        default=int(os.getenv("MIN_VOTE_ALLOCATION", "1000")),
        help="Minimum votes per selected pool",
    )
    parser.add_argument("--top-k", type=int, default=0, help="Force a fixed k instead of auto-resolving from review CSV")
    parser.add_argument("--k-min", type=int, default=1, help="Minimum k if sweep is needed")
    parser.add_argument("--k-max", type=int, default=50, help="Maximum k if sweep is needed")
    parser.add_argument("--k-step", type=int, default=1, help="k step if sweep is needed")
    parser.add_argument("--progress-every-k", type=int, default=10, help="Sweep heartbeat frequency")
    parser.add_argument("--top-n-summary", type=int, default=10, help="Number of rows to print in the console summary")
    parser.add_argument("--verbose", action="store_true", help="Verbose logging")
    args = parser.parse_args()

    configure_logging(args.verbose)

    if int(args.epoch) <= 0:
        raise SystemExit("--epoch must be > 0")
    if int(args.voting_power) <= 0:
        raise SystemExit("--voting-power must be > 0")

    db_path = Path(args.db_path)
    if not db_path.exists():
        raise SystemExit(f"DB not found: {db_path}")

    review_csv = Path(args.review_csv)
    resolved_k = int(args.top_k) if int(args.top_k) > 0 else resolve_k_from_review_csv(review_csv, int(args.epoch))

    conn = sqlite3.connect(str(db_path))
    try:
        boundary_states = load_boundary_states(conn, int(args.epoch))
        if not boundary_states:
            raise SystemExit(f"No boundary states with rewards found for epoch {args.epoch}")
        executed_votes = load_executed_votes(conn, int(args.epoch))
        boundary_states = subtract_executed_votes(boundary_states, executed_votes)

        if resolved_k and resolved_k > 0:
            k_min = resolved_k
            k_max = resolved_k
            logger.info("Using boundary_opt_k=%s for epoch %s", resolved_k, args.epoch)
        else:
            k_min = int(args.k_min)
            k_max = int(args.k_max)
            logger.info("Review CSV missing epoch %s; running local k sweep [%s..%s]", args.epoch, k_min, k_max)

        best_k, allocation, best_expected = auto_select_k(
            states=boundary_states,
            voting_power=int(args.voting_power),
            candidate_pools=int(args.candidate_pools),
            min_votes_per_pool=int(args.min_votes_per_pool),
            k_min=int(k_min),
            k_max=int(k_max),
            k_step=int(args.k_step),
            logger=logger,
            context_label="boundary_export",
            epoch=int(args.epoch),
            progress_every_k=int(args.progress_every_k),
        )
    finally:
        conn.close()

    if not allocation:
        raise SystemExit(f"No allocation produced for epoch {args.epoch}")

    output_path = build_output_path(args.output_csv, int(args.epoch), int(best_k))
    write_allocation_csv(output_path, allocation)
    render_summary(int(args.epoch), int(best_k), float(best_expected), allocation, int(args.top_n_summary))
    console.print(f"Wrote allocation CSV: {output_path}")


if __name__ == "__main__":
    main()