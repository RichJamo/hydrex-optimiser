"""Phase 5/6 of claim_and_swap_rewards.py: run persistence and the weekly rollup report."""

import csv
import json
import sqlite3
import time
from typing import Dict, List

from rich.console import Console
from rich.table import Table

console = Console()


def ensure_claim_swap_log_table(conn: sqlite3.Connection) -> None:
    """Create claim/swap execution log table if missing."""
    cursor = conn.cursor()
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS claim_swap_execution_log (
            run_ts INTEGER NOT NULL,
            epoch INTEGER NOT NULL,
            phase TEXT NOT NULL,
            action_type TEXT,
            token_address TEXT,
            token_symbol TEXT,
            bribe_count INTEGER,
            token_count INTEGER,
            amount_in_raw TEXT,
            usd_value REAL,
            slippage_pct REAL,
            status TEXT NOT NULL,
            tx_hash TEXT,
            error_text TEXT,
            metadata_json TEXT,
            PRIMARY KEY (run_ts, phase, action_type, token_address, tx_hash)
        )
        """
    )
    conn.commit()


def persist_phase_results(
    conn: sqlite3.Connection,
    run_ts: int,
    epoch: int,
    claim_results: List[Dict],
    swap_results: List[Dict],
) -> None:
    """Persist claim and swap outputs for weekly review analytics."""
    ensure_claim_swap_log_table(conn)
    cursor = conn.cursor()

    for r in claim_results:
        cursor.execute(
            """
            INSERT OR REPLACE INTO claim_swap_execution_log (
                run_ts, epoch, phase, action_type, token_address, token_symbol,
                bribe_count, token_count, amount_in_raw, usd_value, slippage_pct,
                status, tx_hash, error_text, metadata_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_ts,
                epoch,
                "phase3_claim",
                r.get("action"),
                None,
                None,
                len(r.get("bribes", [])),
                r.get("token_count", 0),
                None,
                None,
                None,
                r.get("status", "unknown"),
                r.get("tx_hash"),
                r.get("error"),
                json.dumps(r, sort_keys=True),
            ),
        )

    for r in swap_results:
        cursor.execute(
            """
            INSERT OR REPLACE INTO claim_swap_execution_log (
                run_ts, epoch, phase, action_type, token_address, token_symbol,
                bribe_count, token_count, amount_in_raw, usd_value, slippage_pct,
                status, tx_hash, error_text, metadata_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_ts,
                epoch,
                "phase4_swap",
                "swap",
                r.get("token"),
                r.get("symbol"),
                None,
                None,
                str(r.get("amount_in", "")),
                r.get("usd_value"),
                r.get("slippage_pct"),
                r.get("status", "unknown"),
                r.get("tx_hash"),
                r.get("error"),
                json.dumps(r, sort_keys=True),
            ),
        )

    cursor.execute(
        """
        INSERT OR REPLACE INTO claim_swap_execution_log (
            run_ts, epoch, phase, action_type, token_address, token_symbol,
            bribe_count, token_count, amount_in_raw, usd_value, slippage_pct,
            status, tx_hash, error_text, metadata_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            run_ts,
            epoch,
            "phase5_summary",
            "run_summary",
            "__run__",
            "RUN",
            len(claim_results),
            len(swap_results),
            None,
            None,
            None,
            "ok",
            None,
            None,
            json.dumps(
                {
                    "claim_results_count": len(claim_results),
                    "swap_results_count": len(swap_results),
                },
                sort_keys=True,
            ),
        ),
    )

    conn.commit()


def generate_weekly_rollup(
    conn: sqlite3.Connection,
    lookback_days: int,
) -> Dict:
    """Aggregate recent claim/swap run data for weekly review."""
    cutoff_ts = int(time.time()) - (lookback_days * 24 * 60 * 60)
    cursor = conn.cursor()

    cursor.execute(
        """
        SELECT phase, status, COUNT(*)
        FROM claim_swap_execution_log
        WHERE run_ts >= ?
        GROUP BY phase, status
        ORDER BY phase, status
        """,
        (cutoff_ts,),
    )
    phase_status_counts = [
        {"phase": row[0], "status": row[1], "count": int(row[2])}
        for row in cursor.fetchall()
    ]

    cursor.execute(
        """
        SELECT
            COALESCE(token_symbol, 'UNKNOWN') AS token_symbol,
            COALESCE(token_address, '') AS token_address,
            COUNT(*) AS swaps,
            SUM(COALESCE(usd_value, 0.0)) AS total_usd,
            SUM(CASE WHEN status = 'success' THEN 1 ELSE 0 END) AS success_count,
            SUM(CASE WHEN status = 'error' THEN 1 ELSE 0 END) AS error_count,
            SUM(CASE WHEN status = 'dry_run' THEN 1 ELSE 0 END) AS dry_run_count
        FROM claim_swap_execution_log
        WHERE run_ts >= ?
          AND phase = 'phase4_swap'
        GROUP BY token_symbol, token_address
        ORDER BY total_usd DESC
        """,
        (cutoff_ts,),
    )
    swap_rollup = [
        {
            "token_symbol": row[0],
            "token_address": row[1],
            "swaps": int(row[2]),
            "total_usd": float(row[3] or 0.0),
            "success_count": int(row[4]),
            "error_count": int(row[5]),
            "dry_run_count": int(row[6]),
        }
        for row in cursor.fetchall()
    ]

    cursor.execute(
        """
        SELECT run_ts, epoch, bribe_count, token_count
        FROM claim_swap_execution_log
        WHERE run_ts >= ?
          AND phase = 'phase5_summary'
          AND action_type = 'run_summary'
        ORDER BY run_ts DESC
        """,
        (cutoff_ts,),
    )
    run_summaries = [
        {
            "run_ts": int(row[0]),
            "epoch": int(row[1]),
            "claim_results_count": int(row[2] or 0),
            "swap_results_count": int(row[3] or 0),
        }
        for row in cursor.fetchall()
    ]

    return {
        "generated_ts": int(time.time()),
        "lookback_days": lookback_days,
        "cutoff_ts": cutoff_ts,
        "phase_status_counts": phase_status_counts,
        "swap_rollup": swap_rollup,
        "run_summaries": run_summaries,
    }


def write_weekly_rollup_json(report: Dict, output_path: str) -> None:
    """Write rollup JSON file."""
    with open(output_path, "w") as f:
        json.dump(report, f, indent=2, sort_keys=True)


def write_weekly_rollup_csv(report: Dict, output_path: str) -> None:
    """Write swap rollup section as CSV."""
    with open(output_path, "w", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "token_symbol",
                "token_address",
                "swaps",
                "total_usd",
                "success_count",
                "error_count",
                "dry_run_count",
            ],
        )
        writer.writeheader()
        for row in report.get("swap_rollup", []):
            writer.writerow(row)


def print_weekly_rollup(report: Dict) -> None:
    """Render weekly rollup in Rich tables."""
    phase_table = Table(
        title="Phase 6 Weekly Rollup: Phase/Status", header_style="bold cyan"
    )
    phase_table.add_column("Phase")
    phase_table.add_column("Status")
    phase_table.add_column("Count", justify="right")
    for row in report.get("phase_status_counts", []):
        phase_table.add_row(row["phase"], row["status"], str(row["count"]))
    console.print(phase_table)

    swap_table = Table(
        title="Phase 6 Weekly Rollup: Swap Tokens", header_style="bold cyan"
    )
    swap_table.add_column("Token")
    swap_table.add_column("Swaps", justify="right")
    swap_table.add_column("Total USD", justify="right")
    swap_table.add_column("Success", justify="right")
    swap_table.add_column("Errors", justify="right")
    swap_table.add_column("Dry-Run", justify="right")
    for row in report.get("swap_rollup", []):
        swap_table.add_row(
            row["token_symbol"],
            str(row["swaps"]),
            f"{row['total_usd']:.2f}",
            str(row["success_count"]),
            str(row["error_count"]),
            str(row["dry_run_count"]),
        )
    console.print(swap_table)
