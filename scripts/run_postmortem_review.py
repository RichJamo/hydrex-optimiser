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
from web3 import Web3

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.settings import DATABASE_PATH, WEEK


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

    # Auto-derive epoch from boundary block via RPC when --epoch is not given.
    # The canonical epoch key is floor(block_timestamp / WEEK) * WEEK; the
    # corresponding vote_epoch = epoch - WEEK is the active bribe period key.
    rpc_url = os.getenv("RPC_URL", "")
    derived_epoch_from_block: int = 0
    boundary_block_ts: int = 0
    if int(args.boundary_block) > 0 and int(args.epoch) <= 0:
        if not rpc_url:
            raise SystemExit(
                "RPC_URL must be set (or pass --epoch explicitly) when auto-deriving epoch from --boundary-block"
            )
        w3 = Web3(Web3.HTTPProvider(rpc_url))
        if not w3.is_connected():
            raise SystemExit("Failed to connect to RPC; cannot auto-derive epoch from --boundary-block")
        boundary_block_ts = int(w3.eth.get_block(int(args.boundary_block))["timestamp"])
        derived_epoch_from_block = int((boundary_block_ts // WEEK) * WEEK)
        console.print(
            f"[cyan]Auto-derived epoch {derived_epoch_from_block} from block {args.boundary_block} "
            f"(block ts={boundary_block_ts}, vote_epoch={derived_epoch_from_block - WEEK})[/cyan]"
        )

    epoch = int(derived_epoch_from_block) if derived_epoch_from_block > 0 else resolve_epoch(db_path, int(args.epoch))
    if int(args.voting_power) <= 0:
        raise SystemExit("--voting-power must be > 0")

    # Validate that auto_voter_snap prices exist for this epoch so the post-mortem
    # uses the same prices that informed the allocation decision, not today's prices.
    if db_path.exists():
        _snap_conn = sqlite3.connect(str(db_path))
        try:
            _snap_row = _snap_conn.execute(
                """
                SELECT MAX(timestamp), COUNT(*)
                FROM historical_token_prices
                WHERE granularity = 'auto_voter_snap'
                  AND timestamp <= ?
                """,
                (int(epoch),),
            ).fetchone()
        except Exception:
            _snap_row = None
        finally:
            _snap_conn.close()

        if _snap_row and _snap_row[0] is not None and int(_snap_row[1] or 0) > 0:
            _snap_ts = int(_snap_row[0])
            _snap_count = int(_snap_row[1])
            _delta_mins = (int(epoch) - _snap_ts) // 60
            console.print(
                f"[green]✓ Auto-voter price snapshot found: {_snap_count} tokens at ts={_snap_ts} "
                f"(~{_delta_mins} min before epoch boundary)[/green]"
            )
        else:
            console.print(
                "[bold red]No auto_voter_snap prices found in historical_token_prices for epoch "
                f"{epoch}. The post-mortem will fall back to current live prices, which may "
                "differ from what the auto-voter used. Run auto_voter.py before the boundary "
                "to lock prices.[/bold red]"
            )

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

    existing_pythonpath = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = str(ROOT_DIR) + (":" + existing_pythonpath if existing_pythonpath else "")

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

    # Data provenance panel — makes the comparison basis explicit for every run.
    if db_path.exists():
        _prov_conn = sqlite3.connect(str(db_path))
        try:
            _prov_snap = _prov_conn.execute(
                """
                SELECT MAX(timestamp), COUNT(*)
                FROM historical_token_prices
                WHERE granularity = 'auto_voter_snap'
                  AND timestamp <= ?
                """,
                (int(epoch),),
            ).fetchone()
            _prov_block = _prov_conn.execute(
                "SELECT boundary_block FROM epoch_boundaries WHERE epoch = ?",
                (int(epoch),),
            ).fetchone()
        except Exception:
            _prov_snap = None
            _prov_block = None
        finally:
            _prov_conn.close()

        _bb = int(_prov_block[0]) if _prov_block and _prov_block[0] else args.boundary_block
        if _prov_snap and _prov_snap[0] is not None:
            _ps_ts = int(_prov_snap[0])
            _ps_n = int(_prov_snap[1] or 0)
            _ps_delta = (int(epoch) - _ps_ts) // 60
            prices_line = f"auto_voter_snap ts={_ps_ts} ({_ps_n} tokens, ~{_ps_delta} min before boundary)"
        else:
            prices_line = "auto_voter_snap NOT found — live prices used (may differ from auto-voter)"

        console.print(
            Panel.fit(
                f"[bold]Data sources for epoch {epoch}[/bold]\n"
                f"Reward amounts : block {_bb} (T-0, final on-chain)\n"
                f"Vote weights   : block {_bb} (T-0, final on-chain)\n"
                f"Token prices   : {prices_line}",
                title="Post-Mortem Data Provenance",
                border_style="green",
            )
        )


if __name__ == "__main__":
    main()