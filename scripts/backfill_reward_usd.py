#!/usr/bin/env python3
"""
Backfill USD prices for boundary_reward_snapshots and boundary_reward_samples rows
where usd_price IS NULL or total_usd = 0.

Run after the B2 fix to repair historical data.

Usage:
  venv/bin/python scripts/backfill_reward_usd.py
  venv/bin/python scripts/backfill_reward_usd.py --epoch 1776902400
  venv/bin/python scripts/backfill_reward_usd.py --all --dry-run
"""

import argparse
import os
import sqlite3
import sys
from typing import Dict, List, Optional, Tuple

from dotenv import load_dotenv
from rich.console import Console
from rich.table import Table

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config.settings import DATABASE_PATH
from src.price_feed import PriceFeed

load_dotenv()
console = Console()

DEFAULT_DECIMALS = 18  # fallback when token_decimals is NULL


def load_token_decimals(conn: sqlite3.Connection) -> Dict[str, int]:
    """Load token decimals from token_metadata table."""
    try:
        rows = conn.execute(
            "SELECT LOWER(token_address), decimals FROM token_metadata WHERE decimals IS NOT NULL"
        ).fetchall()
        return {addr: int(dec) for addr, dec in rows}
    except sqlite3.OperationalError:
        return {}


def load_boundary_timestamps(conn: sqlite3.Connection) -> Dict[int, int]:
    """Return {epoch: boundary_timestamp} for all known epochs."""
    try:
        rows = conn.execute(
            "SELECT epoch, boundary_timestamp FROM epoch_boundaries"
        ).fetchall()
        return {epoch: ts for epoch, ts in rows}
    except sqlite3.OperationalError:
        return {}


def collect_null_rows(
    conn: sqlite3.Connection,
    epoch: Optional[int],
    table: str,
) -> List[Dict]:
    """Return rows from `table` where usd_price IS NULL or total_usd = 0."""
    epoch_filter = "AND epoch = ?" if epoch else ""
    params = (epoch,) if epoch else ()

    query = f"""
        SELECT rowid, epoch, reward_token, rewards_raw, token_decimals
        FROM {table}
        WHERE (usd_price IS NULL OR total_usd = 0.0 OR total_usd IS NULL)
        {epoch_filter}
    """
    rows = conn.execute(query, params).fetchall()
    return [
        {
            "rowid": r[0],
            "epoch": r[1],
            "reward_token": r[2].lower() if r[2] else None,
            "rewards_raw": r[3],
            "token_decimals": r[4],
        }
        for r in rows
        if r[2]
    ]


def compute_total_usd(
    rewards_raw: str,
    token_dec: Optional[int],
    usd_price: float,
) -> float:
    try:
        raw = int(rewards_raw)
        dec = token_dec if token_dec is not None else DEFAULT_DECIMALS
        return (raw / (10**dec)) * usd_price
    except (ValueError, TypeError, ZeroDivisionError):
        return 0.0


def backfill_table(
    conn: sqlite3.Connection,
    table: str,
    epoch: Optional[int],
    price_feed: PriceFeed,
    boundary_timestamps: Dict[int, int],
    dry_run: bool,
) -> Tuple[int, int]:
    """Backfill a single table using historical prices per epoch. Returns (rows_updated, rows_skipped)."""
    rows = collect_null_rows(conn, epoch, table)
    if not rows:
        console.print(f"  [green]✓ {table}: nothing to backfill[/green]")
        return 0, 0

    console.print(f"  [cyan]{table}: {len(rows)} rows need USD values[/cyan]")

    # Group rows by epoch so we fetch prices at the correct historical timestamp
    from collections import defaultdict
    rows_by_epoch: Dict[int, List[Dict]] = defaultdict(list)
    for r in rows:
        rows_by_epoch[r["epoch"]].append(r)

    summary_stats: Dict[int, Dict] = defaultdict(lambda: {"updated": 0, "skipped": 0, "total_usd": 0.0})
    total_updated = 0
    total_skipped = 0
    cur = conn.cursor()

    for ep, ep_rows in sorted(rows_by_epoch.items()):
        boundary_ts = boundary_timestamps.get(ep)
        unique_tokens = list({r["reward_token"] for r in ep_rows if r["reward_token"]})

        if boundary_ts:
            console.print(
                f"    Epoch {ep}: {len(ep_rows)} rows, {len(unique_tokens)} tokens "
                f"— fetching historical prices at boundary ts={boundary_ts}"
            )
            try:
                prices = price_feed.get_batch_prices_for_timestamp(unique_tokens, boundary_ts)
            except Exception as e:
                console.print(f"    [yellow]⚠ Historical price fetch failed for epoch {ep} ({e}); falling back to current[/yellow]")
                prices = {}

            # If historical lookup returned nothing (subgraph unavailable), fall back to current prices
            priced_historical = sum(1 for p in prices.values() if p and p > 0)
            if priced_historical == 0 and unique_tokens:
                console.print(
                    f"    [yellow]Historical prices unavailable — using current prices as approximation[/yellow]"
                )
                try:
                    prices = price_feed.fetch_batch_prices_by_address(unique_tokens)
                except Exception as e2:
                    console.print(f"    [red]Current price fetch also failed ({e2}); skipping epoch {ep}[/red]")
                    total_skipped += len(ep_rows)
                    for r in ep_rows:
                        summary_stats[ep]["skipped"] += 1
                    continue
        else:
            console.print(
                f"    Epoch {ep}: no boundary_timestamp in epoch_boundaries — using current prices"
            )
            try:
                prices = price_feed.fetch_batch_prices_by_address(unique_tokens)
            except Exception as e:
                console.print(f"    [red]Price fetch failed ({e}); skipping epoch {ep}[/red]")
                total_skipped += len(ep_rows)
                for r in ep_rows:
                    summary_stats[ep]["skipped"] += 1
                continue

        priced = sum(1 for p in prices.values() if p and p > 0)
        missing = [t for t in unique_tokens if not prices.get(t)]
        if missing:
            console.print(
                f"    [yellow]No price for {len(missing)}/{len(unique_tokens)} token(s)[/yellow]"
            )
        else:
            console.print(f"    All {priced} token(s) priced")

        for r in ep_rows:
            token = r["reward_token"]
            usd_price = prices.get(token)
            if not usd_price:
                summary_stats[ep]["skipped"] += 1
                total_skipped += 1
                continue

            total_usd = compute_total_usd(r["rewards_raw"], r["token_decimals"], usd_price)

            if not dry_run:
                cur.execute(
                    f"UPDATE {table} SET usd_price = ?, total_usd = ? WHERE rowid = ?",
                    (usd_price, total_usd, r["rowid"]),
                )

            summary_stats[ep]["updated"] += 1
            summary_stats[ep]["total_usd"] += total_usd
            total_updated += 1

    if not dry_run:
        conn.commit()

    # Print per-epoch summary
    tbl = Table(show_header=True, header_style="bold")
    tbl.add_column("Epoch")
    tbl.add_column("Updated", justify="right")
    tbl.add_column("Skipped", justify="right")
    tbl.add_column("Total USD", justify="right")
    for ep, stats in sorted(summary_stats.items()):
        tbl.add_row(
            str(ep),
            str(stats["updated"]),
            str(stats["skipped"]),
            f"${stats['total_usd']:,.2f}",
        )
    console.print(tbl)

    if dry_run:
        console.print(f"  [yellow]DRY RUN — no changes written[/yellow]")

    return total_updated, total_skipped


def refresh_gauge_values(
    conn: sqlite3.Connection,
    epoch: Optional[int],
    dry_run: bool,
) -> int:
    """
    Update boundary_gauge_values.total_usd by summing boundary_reward_snapshots.total_usd
    per gauge for each epoch.  Returns the number of gauge rows updated.
    """
    epoch_filter = "AND brs.epoch = ?" if epoch else ""
    params = (epoch,) if epoch else ()

    # Count how many gauge rows will be touched
    count = conn.execute(f"""
        SELECT COUNT(DISTINCT bgv.rowid)
        FROM boundary_gauge_values bgv
        WHERE EXISTS (
            SELECT 1 FROM boundary_reward_snapshots brs
            WHERE brs.gauge_address = bgv.gauge_address
              AND brs.epoch = bgv.epoch
              AND brs.active_only = bgv.active_only
              {epoch_filter}
        )
    """, params).fetchone()[0]

    if count == 0:
        console.print("  [green]✓ boundary_gauge_values: nothing to update[/green]")
        return 0

    if not dry_run:
        conn.execute(f"""
            UPDATE boundary_gauge_values
            SET total_usd = (
                SELECT COALESCE(SUM(brs.total_usd), 0.0)
                FROM boundary_reward_snapshots brs
                WHERE brs.gauge_address = boundary_gauge_values.gauge_address
                  AND brs.epoch = boundary_gauge_values.epoch
                  AND brs.active_only = boundary_gauge_values.active_only
            )
            WHERE EXISTS (
                SELECT 1 FROM boundary_reward_snapshots brs
                WHERE brs.gauge_address = boundary_gauge_values.gauge_address
                  AND brs.epoch = boundary_gauge_values.epoch
                  AND brs.active_only = boundary_gauge_values.active_only
                  {epoch_filter}
            )
        """, params)
        conn.commit()
        console.print(f"  [green]✓ boundary_gauge_values: updated {count} gauge rows[/green]")
    else:
        console.print(f"  DRY RUN — would update {count} gauge rows in boundary_gauge_values")

    return count


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill USD prices for reward snapshot tables")
    parser.add_argument("--db-path", default=DATABASE_PATH, help="Path to data.db")
    parser.add_argument("--epoch", type=int, help="Backfill a single epoch only")
    parser.add_argument("--all", dest="all_epochs", action="store_true",
                        help="Backfill all epochs (default if --epoch not given)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would change without writing to DB")
    parser.add_argument("--snapshots-only", action="store_true",
                        help="Only backfill boundary_reward_snapshots (skip samples)")
    parser.add_argument("--samples-only", action="store_true",
                        help="Only backfill boundary_reward_samples (skip snapshots)")
    args = parser.parse_args()

    epoch = args.epoch if args.epoch else None

    console.print(f"[bold]Backfill reward USD prices[/bold]")
    console.print(f"  DB: {args.db_path}")
    console.print(f"  Epoch: {epoch or 'all'}")
    console.print(f"  Dry run: {args.dry_run}")
    console.print()

    conn = sqlite3.connect(args.db_path)

    # Load shared token decimals map for fallback
    decimals_map = load_token_decimals(conn)
    console.print(f"Loaded {len(decimals_map)} token decimal entries from token_metadata")

    boundary_timestamps = load_boundary_timestamps(conn)
    console.print(f"Loaded {len(boundary_timestamps)} epoch boundary timestamps")

    price_feed = PriceFeed(allow_coingecko_fallback=True)

    tables = []
    if not args.samples_only:
        tables.append("boundary_reward_snapshots")
    if not args.snapshots_only:
        try:
            conn.execute("SELECT 1 FROM boundary_reward_samples LIMIT 1")
            tables.append("boundary_reward_samples")
        except sqlite3.OperationalError:
            pass  # table may not exist on older DBs

    total_updated = 0
    total_skipped = 0

    for table in tables:
        console.print(f"\n[bold]Table: {table}[/bold]")
        updated, skipped = backfill_table(conn, table, epoch, price_feed, boundary_timestamps, args.dry_run)
        total_updated += updated
        total_skipped += skipped

    # Refresh boundary_gauge_values from the now-correct snapshot totals
    console.print("\n[bold]Refreshing boundary_gauge_values.total_usd[/bold]")
    refresh_gauge_values(conn, epoch, args.dry_run)

    conn.close()

    console.print()
    console.print(f"[bold green]Done.[/bold green]  Updated: {total_updated}  Skipped (no price): {total_skipped}")
    if args.dry_run:
        console.print("[yellow]DRY RUN — rerun without --dry-run to apply changes[/yellow]")


if __name__ == "__main__":
    main()
