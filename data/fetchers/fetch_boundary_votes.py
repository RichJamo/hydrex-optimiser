#!/usr/bin/env python3
"""
Backfill boundary_gauge_values (vote-side) using canonical boundary blocks.

Fast path:
- Only gauges with positive rewards in boundary_reward_snapshots (SUM(total_usd) > 0)
- Query weightsAt(pool, vote_epoch) at boundary_block
"""

import argparse
import os
import sqlite3
import time
from typing import Dict, List, Optional, Tuple

from dotenv import load_dotenv
from rich.console import Console
from web3 import Web3

from config.settings import DATABASE_PATH, ONE_E18, VOTER_ADDRESS, WEEK

load_dotenv()
console = Console()

VOTER_ABI = [
    {
        "inputs": [
            {"internalType": "address", "name": "_pool", "type": "address"},
            {"internalType": "uint256", "name": "_time", "type": "uint256"},
        ],
        "name": "weightsAt",
        "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [{"internalType": "address", "name": "_gauge", "type": "address"}],
        "name": "isAlive",
        "outputs": [{"internalType": "bool", "name": "", "type": "bool"}],
        "stateMutability": "view",
        "type": "function",
    },
]


def ensure_boundary_gauge_values(conn: sqlite3.Connection) -> None:
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS boundary_gauge_values (
            epoch INTEGER NOT NULL,
            vote_epoch INTEGER NOT NULL,
            active_only INTEGER NOT NULL,
            boundary_block INTEGER NOT NULL,
            gauge_address TEXT NOT NULL,
            pool_address TEXT NOT NULL,
            votes_raw REAL NOT NULL,
            total_usd REAL NOT NULL,
            computed_at INTEGER NOT NULL,
            PRIMARY KEY (epoch, vote_epoch, active_only, gauge_address)
        )
        """
    )
    cur.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_boundary_gauge_values_lookup
        ON boundary_gauge_values(epoch, vote_epoch, active_only, total_usd DESC)
        """
    )
    conn.commit()


def ensure_boundary_vote_samples(conn: sqlite3.Connection) -> None:
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS boundary_vote_samples (
            epoch INTEGER NOT NULL,
            vote_epoch INTEGER NOT NULL,
            active_only INTEGER NOT NULL,
            boundary_block INTEGER NOT NULL,
            query_block INTEGER NOT NULL,
            blocks_before_boundary INTEGER NOT NULL,
            gauge_address TEXT NOT NULL,
            pool_address TEXT NOT NULL,
            votes_raw REAL NOT NULL,
            total_usd REAL NOT NULL,
            computed_at INTEGER NOT NULL,
            PRIMARY KEY (epoch, vote_epoch, active_only, blocks_before_boundary, gauge_address)
        )
        """
    )
    cur.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_boundary_vote_samples_lookup
        ON boundary_vote_samples(epoch, vote_epoch, active_only, blocks_before_boundary)
        """
    )
    conn.commit()


def parse_offset_blocks(offsets_raw: str) -> List[int]:
    if not offsets_raw:
        return []
    offsets = sorted({int(x.strip()) for x in offsets_raw.split(",") if x.strip()})
    return [x for x in offsets if x > 0]


def find_block_at_timestamp(w3: Web3, target_timestamp: int, tolerance: int = 60) -> int:
    latest_block = w3.eth.block_number
    latest_ts = w3.eth.get_block(latest_block)["timestamp"]

    if target_timestamp > latest_ts:
        return latest_block

    blocks_back = int((latest_ts - target_timestamp) / 2)
    left = max(0, latest_block - blocks_back - 2000)
    right = latest_block
    best = left

    while left <= right:
        mid = (left + right) // 2
        blk = w3.eth.get_block(mid)
        ts = blk["timestamp"]

        if abs(ts - target_timestamp) <= tolerance:
            return mid

        if ts < target_timestamp:
            best = mid
            left = mid + 1
        else:
            right = mid - 1

    return best


def load_epoch_boundary(conn: sqlite3.Connection, epoch: int) -> Tuple[int, int]:
    cur = conn.cursor()
    try:
        row = cur.execute(
            """
            SELECT boundary_block, vote_epoch
            FROM epoch_boundaries
            WHERE epoch = ?
            """,
            (int(epoch),),
        ).fetchone()
    except sqlite3.OperationalError:
        return -1, -1

    if not row or row[0] is None or row[1] is None:
        return -1, -1
    return int(row[0]), int(row[1])


def load_reward_positive_gauges(conn: sqlite3.Connection, epoch: int) -> Tuple[List[str], Dict[str, float]]:
    cur = conn.cursor()
    rows = cur.execute(
        """
        SELECT lower(gauge_address) AS gauge_address,
               SUM(COALESCE(total_usd, 0.0)) AS total_usd
        FROM boundary_reward_snapshots
        WHERE epoch = ? AND active_only = 1
        GROUP BY lower(gauge_address)
        HAVING SUM(COALESCE(total_usd, 0.0)) > 0
        ORDER BY total_usd DESC, lower(gauge_address)
        """,
        (int(epoch),),
    ).fetchall()
    gauges = [str(r[0]) for r in rows if r and r[0]]
    totals = {str(r[0]): float(r[1] or 0.0) for r in rows if r and r[0]}
    return gauges, totals


def load_pool_map(conn: sqlite3.Connection, gauges: List[str]) -> Dict[str, str]:
    if not gauges:
        return {}

    cur = conn.cursor()
    placeholders = ",".join(["?"] * len(gauges))
    rows = cur.execute(
        f"""
        SELECT lower(address) AS gauge_address,
               lower(COALESCE(pool, address)) AS pool_address
        FROM gauges
        WHERE lower(address) IN ({placeholders})
        """,
        [g.lower() for g in gauges],
    ).fetchall()

    pool_map = {str(r[0]).lower(): str(r[1]).lower() for r in rows if r and r[0]}
    return pool_map


def load_all_gauges(conn: sqlite3.Connection) -> List[Tuple[str, str]]:
    cur = conn.cursor()
    try:
        rows = cur.execute(
            """
            SELECT lower(address) AS gauge_address,
                   lower(COALESCE(pool, address)) AS pool_address
            FROM gauges
            ORDER BY lower(address)
            """,
        ).fetchall()
    except sqlite3.OperationalError:
        return []

    return [(str(r[0]), str(r[1])) for r in rows if r and r[0]]


def filter_active_onchain(
    voter,
    gauges: List[Tuple[str, str]],
    boundary_block: int,
    progress_every: int,
) -> List[Tuple[str, str]]:
    active: List[Tuple[str, str]] = []
    for idx, (gauge_addr, pool_addr) in enumerate(gauges, start=1):
        try:
            alive = bool(voter.functions.isAlive(Web3.to_checksum_address(gauge_addr)).call(block_identifier=boundary_block))
            if alive:
                active.append((gauge_addr, pool_addr))
        except Exception:
            continue
        if progress_every > 0 and idx % progress_every == 0:
            console.print(f"[dim]isAlive progress: {idx}/{len(gauges)}[/dim]")
    return active


def filter_active_db(conn: sqlite3.Connection, gauges: List[Tuple[str, str]]) -> List[Tuple[str, str]]:
    cur = conn.cursor()
    try:
        rows = cur.execute(
            "SELECT lower(address) FROM gauges WHERE COALESCE(is_alive, 1) = 1"
        ).fetchall()
    except sqlite3.OperationalError:
        return gauges

    active_set = {str(r[0]).lower() for r in rows if r and r[0]}
    return [(g, p) for g, p in gauges if g.lower() in active_set]


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill boundary_gauge_values using canonical boundary blocks")
    parser.add_argument("--end-epoch", type=int, required=True, help="End epoch timestamp")
    parser.add_argument("--weeks", type=int, default=1, help="Number of weeks to go back")
    parser.add_argument("--db", default=DATABASE_PATH, help="SQLite DB path")
    parser.add_argument("--rpc", default=os.getenv("RPC_URL"), help="RPC URL")
    parser.add_argument("--reward-positive-only", action="store_true", help="Only gauges with rewards > 0")
    parser.add_argument(
        "--active-source",
        choices=["onchain", "db", "none"],
        default="none",
        help="Active gauge filter for full mode",
    )
    parser.add_argument("--progress-every", type=int, default=100, help="Progress log frequency")
    parser.add_argument("--max-gauges", type=int, default=0, help="Limit gauges for smoke tests")
    parser.add_argument("--block-tolerance", type=int, default=60)
    parser.add_argument("--vote-epoch-offset-weeks", type=int, default=1)
    parser.add_argument(
        "--offset-blocks",
        type=str,
        default="",
        help="Comma-separated block offsets before boundary to sample (e.g. 1,20). Empty keeps boundary table behavior.",
    )
    args = parser.parse_args()

    if not args.rpc:
        console.print("[red]RPC_URL missing[/red]")
        return

    w3 = Web3(Web3.HTTPProvider(args.rpc))
    if not w3.is_connected():
        console.print("[red]Failed to connect to RPC[/red]")
        return

    conn = sqlite3.connect(args.db)
    ensure_boundary_gauge_values(conn)

    offsets = parse_offset_blocks(args.offset_blocks)
    if offsets:
        ensure_boundary_vote_samples(conn)
        console.print(f"[cyan]Offset sampling mode enabled for offsets: {offsets}[/cyan]")

    voter = w3.eth.contract(address=Web3.to_checksum_address(VOTER_ADDRESS), abi=VOTER_ABI)

    epochs = [int(args.end_epoch - i * WEEK) for i in range(max(1, args.weeks))]
    epochs.sort()

    console.print(f"[cyan]Backfilling votes for {len(epochs)} epochs[/cyan]")

    for epoch_idx, epoch in enumerate(epochs, start=1):
        boundary_block, vote_epoch = load_epoch_boundary(conn, epoch)
        if vote_epoch <= 0:
            vote_epoch = int(epoch - max(0, args.vote_epoch_offset_weeks) * WEEK)
        if boundary_block <= 0:
            boundary_block = find_block_at_timestamp(w3, epoch, args.block_tolerance)

        console.print(
            f"[bold cyan]Epoch {epoch_idx}/{len(epochs)}[/bold cyan] epoch={epoch} boundary_block={boundary_block} vote_epoch={vote_epoch}"
        )

        total_usd_map: Dict[str, float] = {}
        if args.reward_positive_only:
            gauges, total_usd_map = load_reward_positive_gauges(conn, epoch)
            if args.max_gauges and args.max_gauges > 0:
                gauges = gauges[: int(args.max_gauges)]
            pool_map = load_pool_map(conn, gauges)
            gauge_rows = [(g, pool_map.get(g, g)) for g in gauges]
        else:
            gauge_rows = load_all_gauges(conn)
            if args.active_source == "onchain":
                gauge_rows = filter_active_onchain(voter, gauge_rows, boundary_block, max(0, args.progress_every))
            elif args.active_source == "db":
                gauge_rows = filter_active_db(conn, gauge_rows)

            if args.max_gauges and args.max_gauges > 0:
                gauge_rows = gauge_rows[: int(args.max_gauges)]

            if gauge_rows:
                gauge_list = [g for g, _p in gauge_rows]
                _, total_usd_map = load_reward_positive_gauges(conn, epoch)
                total_usd_map = {g: total_usd_map.get(g, 0.0) for g in gauge_list}

        if not gauge_rows:
            console.print(f"[yellow]No gauges to process for epoch {epoch}[/yellow]")
            continue

        offsets_to_run = offsets if offsets else [0]
        now_ts = int(time.time())

        for block_offset in offsets_to_run:
            query_block = int(boundary_block - block_offset)
            pool_votes: Dict[str, float] = {}
            for idx, (_gauge, pool_addr) in enumerate(gauge_rows, start=1):
                pool_l = str(pool_addr).lower()
                if pool_l in pool_votes:
                    continue
                try:
                    weight = int(
                        voter.functions.weightsAt(Web3.to_checksum_address(pool_l), int(vote_epoch)).call(
                            block_identifier=int(query_block)
                        )
                    )
                    pool_votes[pool_l] = float(weight) / ONE_E18
                except Exception:
                    pool_votes[pool_l] = 0.0

                if args.progress_every > 0 and idx % args.progress_every == 0:
                    console.print(
                        f"[dim]votes progress (offset={block_offset}): {idx}/{len(gauge_rows)}[/dim]"
                    )

            cur = conn.cursor()
            if block_offset > 0:
                cur.execute(
                    "DELETE FROM boundary_vote_samples WHERE epoch = ? AND vote_epoch = ? AND active_only = 1 AND blocks_before_boundary = ?",
                    (int(epoch), int(vote_epoch), int(block_offset)),
                )
                rows = [
                    (
                        int(epoch),
                        int(vote_epoch),
                        1,
                        int(boundary_block),
                        int(query_block),
                        int(block_offset),
                        str(g).lower(),
                        str(p).lower(),
                        float(pool_votes.get(str(p).lower(), 0.0)),
                        float(total_usd_map.get(str(g).lower(), 0.0)),
                        int(now_ts),
                    )
                    for g, p in gauge_rows
                ]
                cur.executemany(
                    """
                    INSERT OR REPLACE INTO boundary_vote_samples(
                        epoch, vote_epoch, active_only, boundary_block, query_block, blocks_before_boundary,
                        gauge_address, pool_address, votes_raw, total_usd, computed_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    rows,
                )
            else:
                cur.execute(
                    "DELETE FROM boundary_gauge_values WHERE epoch = ? AND vote_epoch = ? AND active_only = 1",
                    (int(epoch), int(vote_epoch)),
                )
                rows = [
                    (
                        int(epoch),
                        int(vote_epoch),
                        1,
                        int(boundary_block),
                        str(g).lower(),
                        str(p).lower(),
                        float(pool_votes.get(str(p).lower(), 0.0)),
                        float(total_usd_map.get(str(g).lower(), 0.0)),
                        int(now_ts),
                    )
                    for g, p in gauge_rows
                ]
                cur.executemany(
                    """
                    INSERT OR REPLACE INTO boundary_gauge_values(
                        epoch, vote_epoch, active_only, boundary_block, gauge_address, pool_address,
                        votes_raw, total_usd, computed_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    rows,
                )

            conn.commit()
            console.print(
                f"[green]✓ Epoch {epoch} offset={block_offset}: {len(rows)} vote rows inserted[/green]"
            )

    conn.close()


if __name__ == "__main__":
    main()
