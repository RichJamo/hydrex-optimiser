#!/usr/bin/env python3
"""
B4+B5: Merge preboundary_dev.db into data.db.

What this does
--------------
1. Applies schema v4 (creates 7 preboundary tables + epoch_pool_realisation view
   in data.db if not already present).
2. Copies all data from the 7 preboundary-only tables:
     preboundary_snapshots, preboundary_truth_labels, preboundary_forecasts,
     preboundary_recommendations, preboundary_backtest_gauge_results,
     preboundary_backtest_results, preboundary_backtest_portfolio_results
3. Merges boundary_reward_snapshots rows from epochs that exist in dev but not main
   (INSERT OR IGNORE — safe to re-run).
4. Prints a row-count summary.

This script is idempotent — safe to run multiple times.
"""

import argparse
import sqlite3
import sys
import os
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rich.console import Console
from rich.table import Table

from src.db import apply_schema

console = Console()

DEV_DB_DEFAULT  = "data/db/preboundary_dev.db"
MAIN_DB_DEFAULT = "data/db/data.db"

# Tables to copy wholesale (only exist in dev)
PREBOUNDARY_TABLES = [
    "preboundary_snapshots",
    "preboundary_truth_labels",
    "preboundary_forecasts",
    "preboundary_recommendations",
    "preboundary_backtest_gauge_results",
    "preboundary_backtest_results",
    "preboundary_backtest_portfolio_results",
]


def _count(conn: sqlite3.Connection, table: str) -> int:
    try:
        return conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
    except sqlite3.OperationalError:
        return 0


def _columns(conn: sqlite3.Connection, table: str) -> list:
    rows = conn.execute(f'PRAGMA table_info("{table}")').fetchall()
    return [r[1] for r in rows]


def migrate(dev_path: str, main_path: str, dry_run: bool) -> None:
    if not Path(dev_path).exists():
        console.print(f"[red]Dev DB not found: {dev_path}[/red]")
        sys.exit(1)

    console.print(f"[cyan]Applying schema v4 to {main_path}…[/cyan]")
    apply_schema(main_path)  # always idempotent — safe in dry-run too

    dev  = sqlite3.connect(dev_path)
    main = sqlite3.connect(main_path)

    results = []

    # ── 1. Preboundary-only tables ──────────────────────────────────────────
    for table in PREBOUNDARY_TABLES:
        before = _count(main, table)
        dev_total = _count(dev, table)

        if dev_total == 0:
            results.append((table, 0, 0, before, "skipped (dev empty)"))
            continue

        # Get column intersection (schema may differ slightly between dbs)
        dev_cols  = _columns(dev, table)
        main_cols = _columns(main, table)
        common    = [c for c in dev_cols if c in main_cols]
        col_list  = ", ".join(f'"{c}"' for c in common)

        rows = dev.execute(f'SELECT {col_list} FROM "{table}"').fetchall()
        if not dry_run:
            main.executemany(
                f'INSERT OR IGNORE INTO "{table}" ({col_list}) VALUES ({",".join("?"*len(common))})',
                rows,
            )
            main.commit()

        after    = _count(main, table)
        inserted = after - before
        results.append((table, dev_total, inserted, after, "ok" if not dry_run else "dry-run"))

    # ── 2. boundary_reward_snapshots — only epochs absent from main ─────────
    brs_table = "boundary_reward_snapshots"
    dev_epochs  = {r[0] for r in dev.execute(f"SELECT DISTINCT epoch FROM {brs_table}").fetchall()}
    main_epochs = {r[0] for r in main.execute(f"SELECT DISTINCT epoch FROM {brs_table}").fetchall()}
    new_epochs  = sorted(dev_epochs - main_epochs)

    brs_before = _count(main, brs_table)
    brs_dev    = _count(dev, brs_table)
    if new_epochs:
        common_brs = [c for c in _columns(dev, brs_table) if c in _columns(main, brs_table)]
        col_list_brs = ", ".join(f'"{c}"' for c in common_brs)
        placeholders = ",".join("?" * len(common_brs))
        for ep in new_epochs:
            rows = dev.execute(
                f'SELECT {col_list_brs} FROM "{brs_table}" WHERE epoch = ?', (ep,)
            ).fetchall()
            if not dry_run:
                main.executemany(
                    f'INSERT OR IGNORE INTO "{brs_table}" ({col_list_brs}) VALUES ({placeholders})',
                    rows,
                )
        if not dry_run:
            main.commit()

    brs_after    = _count(main, brs_table)
    brs_inserted = brs_after - brs_before
    results.append((
        brs_table,
        brs_dev,
        brs_inserted,
        brs_after,
        f"merged {len(new_epochs)} new epochs" + (" (dry-run)" if dry_run else ""),
    ))

    dev.close()
    main.close()

    # ── Summary table ──────────────────────────────────────────────────────
    tbl = Table(
        title="B4+B5 Migration" + (" — DRY RUN" if dry_run else ""),
        header_style="bold cyan",
    )
    tbl.add_column("Table")
    tbl.add_column("Dev rows", justify="right")
    tbl.add_column("Inserted", justify="right")
    tbl.add_column("Main total", justify="right")
    tbl.add_column("Status")
    for table, dev_n, ins, total, status in results:
        tbl.add_row(table, str(dev_n), str(ins), str(total), status)
    console.print(tbl)

    if dry_run:
        console.print("[yellow]Dry-run complete — no data written.[/yellow]")
    else:
        console.print("[green]✓ Migration complete.[/green]")


def main() -> None:
    parser = argparse.ArgumentParser(description="Merge preboundary_dev.db into data.db")
    parser.add_argument("--dev-db",  default=DEV_DB_DEFAULT,  help="Source dev DB path")
    parser.add_argument("--main-db", default=MAIN_DB_DEFAULT, help="Target main DB path")
    parser.add_argument("--dry-run", action="store_true", help="Preview without writing")
    args = parser.parse_args()
    migrate(args.dev_db, args.main_db, args.dry_run)


if __name__ == "__main__":
    main()
