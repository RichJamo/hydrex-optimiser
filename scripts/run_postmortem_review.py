#!/usr/bin/env python3
"""Run the canonical single-command post-mortem review flow for one epoch."""

import argparse
import csv
import logging
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.settings import DATABASE_PATH


logger = logging.getLogger(__name__)
console = Console()
ROOT_DIR = Path(__file__).resolve().parent.parent


def configure_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(level=level, format="%(asctime)s | %(levelname)s | %(message)s")


def resolve_epoch(db_path: Path, explicit_epoch: int) -> int:
    if explicit_epoch > 0:
        return int(explicit_epoch)

    if not db_path.exists():
        raise SystemExit(f"DB not found while resolving latest epoch: {db_path}")

    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute("SELECT MAX(epoch) FROM epoch_boundaries").fetchone()
    finally:
        conn.close()

    if not row or row[0] is None:
        raise SystemExit("Could not resolve latest epoch from epoch_boundaries; pass --epoch explicitly")
    return int(row[0])


def run_subprocess(command, env, dry_run: bool) -> None:
    rendered = " ".join(str(part) for part in command)
    console.print(f"[cyan]$ {rendered}[/cyan]")
    if dry_run:
        return
    subprocess.run(command, cwd=str(ROOT_DIR), env=env, check=True)


def load_review_row(review_csv: Path, epoch: int) -> Optional[dict]:
    if not review_csv.exists():
        return None

    with review_csv.open("r", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            try:
                if int(row.get("epoch", "0") or 0) == int(epoch):
                    return row
            except (TypeError, ValueError):
                continue
    return None


def render_final_summary(epoch: int, review_row: Optional[dict], review_csv: Path) -> None:
    if not review_row:
        console.print(f"Review CSV not found or missing epoch {epoch}: {review_csv}")
        return

    summary = Table(title=f"Post-Mortem Summary (epoch={epoch})", header_style="bold cyan")
    summary.add_column("Metric")
    summary.add_column("Value", justify="right")
    summary.add_row("boundary_opt_k", str(review_row.get("boundary_opt_k", "")))
    summary.add_row("boundary_opt_expected_usd", f"${float(review_row.get('boundary_opt_expected_usd', '0') or 0):,.2f}")
    summary.add_row("t1_pred_k", str(review_row.get("t1_pred_k", "")))
    summary.add_row("t1_pred_expected_usd", f"${float(review_row.get('t1_pred_expected_usd', '0') or 0):,.2f}")
    summary.add_row(
        "t1_realized_at_boundary_usd",
        f"${float(review_row.get('t1_realized_at_boundary_usd', '0') or 0):,.2f}",
    )
    summary.add_row("opportunity_gap_usd", f"${float(review_row.get('opportunity_gap_usd', '0') or 0):,.2f}")
    summary.add_row("opportunity_gap_pct", f"{float(review_row.get('opportunity_gap_pct', '0') or 0):,.2f}%")
    console.print(summary)


def main() -> None:
    load_dotenv()

    parser = argparse.ArgumentParser(
        description="Canonical one-command wrapper for Hydrex epoch post-mortem review"
    )
    parser.add_argument("--epoch", type=int, default=0, help="Target epoch timestamp; defaults to latest epoch_boundaries row")
    parser.add_argument("--boundary-block", type=int, default=0, help="Optional boundary block to upsert before review")
    parser.add_argument("--vote-epoch", type=int, default=0, help="Optional override for set_epoch_boundary_manual.py")
    parser.add_argument("--boundary-timestamp", type=int, default=0, help="Optional override for set_epoch_boundary_manual.py")
    parser.add_argument("--reward-epoch", type=int, default=0, help="Optional override for set_epoch_boundary_manual.py")
    parser.add_argument("--source-tag", default="manual_explorer_boundary", help="Boundary source tag when upserting")
    parser.add_argument("--db-path", default=DATABASE_PATH, help="Main DB path")
    parser.add_argument("--preboundary-db-path", default="data/db/preboundary_dev.db", help="Preboundary DB path")
    parser.add_argument(
        "--review-csv",
        default="analysis/pre_boundary/epoch_boundary_vs_t1_review_all.csv",
        help="Output review CSV path",
    )
    parser.add_argument(
        "--voting-power",
        type=int,
        default=int(os.getenv("YOUR_VOTING_POWER", "0")),
        help="Voting power used for review and allocation export",
    )
    parser.add_argument("--candidate-pools", type=int, default=60, help="Candidate pool cap for k sweep")
    parser.add_argument(
        "--min-votes-per-pool",
        type=int,
        default=int(os.getenv("MIN_VOTE_ALLOCATION", "1000")),
        help="Minimum votes per selected pool",
    )
    parser.add_argument("--k-min", type=int, default=1, help="Minimum k for review sweep")
    parser.add_argument("--k-max", type=int, default=50, help="Maximum k for review sweep")
    parser.add_argument("--k-step", type=int, default=1, help="k step for review sweep")
    parser.add_argument("--progress-every-k", type=int, default=10, help="Review sweep heartbeat frequency")
    parser.add_argument(
        "--run-boundary-refresh",
        action="store_true",
        help="Refresh boundary reward snapshots before review",
    )
    parser.add_argument(
        "--run-boundary-votes-refresh",
        choices=["auto", "true", "false"],
        default="auto",
        help="Boundary vote cache refresh policy",
    )
    parser.add_argument("--actual-rewards-json", default="", help="Optional token-level reconciliation JSON")
    parser.add_argument("--top-n-summary", type=int, default=10, help="Top N pools to print after allocation export")
    parser.add_argument("--dry-run", action="store_true", help="Print subcommands without executing them")
    parser.add_argument("--verbose", action="store_true", help="Verbose logging")
    args = parser.parse_args()

    configure_logging(args.verbose)

    db_path = Path(args.db_path)
    preboundary_db_path = Path(args.preboundary_db_path)
    review_csv = Path(args.review_csv)

    epoch = resolve_epoch(db_path, int(args.epoch))
    if int(args.voting_power) <= 0:
        raise SystemExit("--voting-power must be > 0")

    console.print(
        Panel.fit(
            f"epoch={epoch} | voting_power={int(args.voting_power):,} | boundary_block={'set' if int(args.boundary_block) > 0 else 'unchanged'}",
            title="Run Post-Mortem Review",
            border_style="cyan",
        )
    )

    env = os.environ.copy()
    env.update(
        {
            "TARGET_EPOCH": str(epoch),
            "VOTING_POWER": str(int(args.voting_power)),
            "LIVE_DB_PATH": str(db_path),
            "PREBOUNDARY_DB_PATH": str(preboundary_db_path),
            "OUTPUT_CSV": str(review_csv),
            "RUN_BOUNDARY_REFRESH": "true" if args.run_boundary_refresh else "false",
            "RUN_BOUNDARY_VOTES_REFRESH": str(args.run_boundary_votes_refresh),
            "CANDIDATE_POOLS": str(int(args.candidate_pools)),
            "MIN_VOTES_PER_POOL": str(int(args.min_votes_per_pool)),
            "K_MIN": str(int(args.k_min)),
            "K_MAX": str(int(args.k_max)),
            "K_STEP": str(int(args.k_step)),
            "PROGRESS_EVERY_K": str(int(args.progress_every_k)),
        }
    )
    if args.actual_rewards_json:
        env["ACTUAL_REWARDS_JSON"] = str(args.actual_rewards_json)

    if int(args.boundary_block) > 0:
        boundary_command = [
            sys.executable,
            str(ROOT_DIR / "scripts" / "set_epoch_boundary_manual.py"),
            "--epoch",
            str(epoch),
            "--boundary-block",
            str(int(args.boundary_block)),
            "--source-tag",
            str(args.source_tag),
            "--db-path",
            str(db_path),
        ]
        if int(args.vote_epoch) > 0:
            boundary_command.extend(["--vote-epoch", str(int(args.vote_epoch))])
        if int(args.boundary_timestamp) > 0:
            boundary_command.extend(["--boundary-timestamp", str(int(args.boundary_timestamp))])
        if int(args.reward_epoch) > 0:
            boundary_command.extend(["--reward-epoch", str(int(args.reward_epoch))])
        run_subprocess(boundary_command, env=env, dry_run=bool(args.dry_run))

    pipeline_command = ["bash", str(ROOT_DIR / "scripts" / "run_preboundary_analysis_pipeline.sh")]
    run_subprocess(pipeline_command, env=env, dry_run=bool(args.dry_run))

    export_command = [
        sys.executable,
        str(ROOT_DIR / "scripts" / "export_boundary_optimal_allocation.py"),
        "--epoch",
        str(epoch),
        "--db-path",
        str(db_path),
        "--review-csv",
        str(review_csv),
        "--voting-power",
        str(int(args.voting_power)),
        "--candidate-pools",
        str(int(args.candidate_pools)),
        "--min-votes-per-pool",
        str(int(args.min_votes_per_pool)),
        "--k-min",
        str(int(args.k_min)),
        "--k-max",
        str(int(args.k_max)),
        "--k-step",
        str(int(args.k_step)),
        "--progress-every-k",
        str(int(args.progress_every_k)),
        "--top-n-summary",
        str(int(args.top_n_summary)),
    ]
    if args.verbose:
        export_command.append("--verbose")
    run_subprocess(export_command, env=env, dry_run=bool(args.dry_run))

    if args.dry_run:
        return

    render_final_summary(epoch, load_review_row(review_csv, epoch), review_csv)


if __name__ == "__main__":
    main()