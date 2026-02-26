#!/usr/bin/env python3
"""
Record manual predicted/executed allocations into allocation tracking tables.

Usage examples:

  # Record executed allocation for an epoch
  venv/bin/python scripts/record_manual_allocation.py \
    --type executed \
    --epoch 1772064000 \
    --strategy-tag manual_4pool \
    --item 0xcecf4d16114e601276ba7e8c39a309fbfc605f0e,0xcecf4d16114e601276ba7e8c39a309fbfc605f0e,295818 \
    --item 0x66352585bad83da857a020f739f1f7ca93209a1b,0x66352585bad83da857a020f739f1f7ca93209a1b,295818

  # Record predicted allocation for an epoch (vote_epoch inferred as epoch-WEEK)
  venv/bin/python scripts/record_manual_allocation.py \
    --type predicted \
    --epoch 1772064000 \
    --strategy-tag preboundary_manual \
    --item <gauge>,<pool>,<votes>
"""

import argparse
import os
import sqlite3
import sys
from typing import List, Tuple

from dotenv import load_dotenv
from rich.console import Console

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config.settings import DATABASE_PATH, WEEK
from src.allocation_tracking import (
    ensure_allocation_tracking_tables,
    save_executed_allocation,
    save_predicted_allocation,
)

load_dotenv()
console = Console()


def parse_items(items: List[str]) -> List[Tuple[int, str, str, int]]:
    parsed: List[Tuple[int, str, str, int]] = []
    for idx, raw in enumerate(items, start=1):
        parts = [p.strip() for p in str(raw).split(",")]
        if len(parts) != 3:
            raise ValueError(f"Invalid --item format: {raw}. Expected gauge,pool,votes")
        gauge, pool, votes = parts
        parsed.append((idx, gauge.lower(), pool.lower(), int(votes)))
    return parsed


def main() -> None:
    parser = argparse.ArgumentParser(description="Record manual predicted/executed allocations")
    parser.add_argument("--db-path", default=DATABASE_PATH, help="SQLite database path")
    parser.add_argument("--type", choices=["predicted", "executed"], required=True)
    parser.add_argument("--epoch", type=int, required=True, help="Reward epoch timestamp")
    parser.add_argument("--strategy-tag", default="manual", help="Strategy tag label")
    parser.add_argument("--source", default="manual", help="Source label for executed allocations")
    parser.add_argument("--tx-hash", default="", help="Optional transaction hash for executed allocations")
    parser.add_argument(
        "--item",
        action="append",
        default=[],
        help="Allocation row in format gauge,pool,votes (repeat this flag)",
    )
    args = parser.parse_args()

    if not args.item:
        console.print("[red]At least one --item is required[/red]")
        sys.exit(1)

    rows = parse_items(args.item)
    conn = sqlite3.connect(args.db_path)

    try:
        ensure_allocation_tracking_tables(conn)

        if args.type == "predicted":
            vote_epoch = int(args.epoch - WEEK)
            inserted = save_predicted_allocation(
                conn=conn,
                vote_epoch=vote_epoch,
                snapshot_ts=0,
                query_block=0,
                strategy_tag=args.strategy_tag,
                rows=rows,
            )
            console.print(
                f"[green]✓ Saved {inserted} predicted rows for epoch={args.epoch} strategy={args.strategy_tag}[/green]"
            )
        else:
            inserted = save_executed_allocation(
                conn=conn,
                epoch=args.epoch,
                strategy_tag=args.strategy_tag,
                rows=rows,
                source=args.source,
                tx_hash=(args.tx_hash or None),
            )
            console.print(
                f"[green]✓ Saved {inserted} executed rows for epoch={args.epoch} strategy={args.strategy_tag}[/green]"
            )

    finally:
        conn.close()


if __name__ == "__main__":
    main()
