#!/usr/bin/env python3
"""
Collect boundary snapshot data for max-return analysis.

This is the DATA COLLECTION step (online):
- Active gauges only
- Approved reward tokens only (from each bribe contract rewards list)
- Stores DB snapshots for later OFFLINE analysis

Usage:
  python -m data.fetchers.fetch_boundary_snapshots --end-epoch 1771372800 --weeks 13
  python -m data.fetchers.fetch_boundary_snapshots --end-epoch 1771372800 --weeks 13 --vote-epoch-offset-weeks 1
"""

import argparse
import os
import sqlite3
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Tuple

from dotenv import load_dotenv
from rich.console import Console
from web3 import Web3

from config.settings import DATABASE_PATH, ONE_E18, VOTER_ADDRESS, WEEK
from src.database import Database

load_dotenv()
console = Console()


def _format_eta(seconds: float) -> str:
    if seconds < 0:
        return "n/a"
    total = int(seconds)
    hrs = total // 3600
    mins = (total % 3600) // 60
    secs = total % 60
    if hrs > 0:
        return f"{hrs}h {mins}m {secs}s"
    if mins > 0:
        return f"{mins}m {secs}s"
    return f"{secs}s"

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

BRIBE_ABI = [
    {
        "inputs": [],
        "name": "rewardsListLength",
        "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
        "name": "rewardTokens",
        "outputs": [{"internalType": "address", "name": "", "type": "address"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [{"internalType": "address", "name": "", "type": "address"}],
        "name": "isRewardToken",
        "outputs": [{"internalType": "bool", "name": "", "type": "bool"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [
            {"internalType": "address", "name": "", "type": "address"},
            {"internalType": "uint256", "name": "", "type": "uint256"},
        ],
        "name": "rewardData",
        "outputs": [
            {"internalType": "uint256", "name": "periodFinish", "type": "uint256"},
            {"internalType": "uint256", "name": "rewardsPerEpoch", "type": "uint256"},
            {"internalType": "uint256", "name": "lastUpdateTime", "type": "uint256"},
        ],
        "stateMutability": "view",
        "type": "function",
    },
]


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


def ensure_snapshot_tables(conn: sqlite3.Connection) -> None:
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

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS boundary_reward_snapshots (
            epoch INTEGER NOT NULL,
            vote_epoch INTEGER NOT NULL,
            active_only INTEGER NOT NULL,
            boundary_block INTEGER NOT NULL,
            gauge_address TEXT NOT NULL,
            bribe_contract TEXT NOT NULL,
            reward_token TEXT NOT NULL,
            rewards_raw TEXT NOT NULL,
            token_decimals INTEGER,
            usd_price REAL,
            total_usd REAL NOT NULL,
            computed_at INTEGER NOT NULL,
            PRIMARY KEY (epoch, vote_epoch, active_only, bribe_contract, reward_token)
        )
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS bribe_reward_tokens (
            bribe_contract TEXT NOT NULL,
            reward_token TEXT NOT NULL,
            is_reward_token INTEGER NOT NULL,
            updated_at INTEGER NOT NULL,
            PRIMARY KEY (bribe_contract, reward_token)
        )
        """
    )
    conn.commit()


def load_epoch_boundary(conn: sqlite3.Connection, epoch: int) -> Tuple[int, int]:
    """Return (boundary_block, vote_epoch) from epoch_boundaries when available."""
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


def get_active_gauge_rows(conn: sqlite3.Connection, epoch: int) -> List[Tuple[str, str, str, str]]:
    """
    Load historically-active gauges from gauge_bribe_mapping.
    Falls back to bribes table join if mapping table doesn't exist.
    """
    cur = conn.cursor()
    
    # Try to use gauge_bribe_mapping table (new approach)
    try:
        cur.execute(
            """
            SELECT 
                m.gauge_address,
                lower(COALESCE(g.pool, g.address)) AS pool_address,
                m.internal_bribe,
                m.external_bribe
            FROM gauge_bribe_mapping m
            JOIN gauges g ON lower(g.address) = m.gauge_address
            ORDER BY lower(g.address)
            """
        )
        rows = cur.fetchall()
        if rows:
            return [r for r in rows if r and r[0]]
    except sqlite3.OperationalError:
        pass
    
    # Fallback: use bribes table join (legacy approach)
    cur.execute(
        """
        SELECT DISTINCT
            lower(g.address) AS gauge_address,
            lower(COALESCE(g.pool, g.address)) AS pool_address,
            lower(COALESCE(g.internal_bribe, '')) AS internal_bribe,
            lower(COALESCE(g.external_bribe, '')) AS external_bribe
        FROM gauges g
        JOIN bribes b ON lower(b.gauge_address) = lower(g.address)
        WHERE b.epoch = ?
        """,
        (epoch,),
    )
    return [r for r in cur.fetchall() if r and r[0]]


def filter_active_gauge_rows_onchain(
    voter,
    gauge_rows: List[Tuple[str, str, str, str]],
    boundary_block: int,
    progress_every: int,
) -> List[Tuple[str, str, str, str]]:
    active_rows: List[Tuple[str, str, str, str]] = []
    start = time.time()
    total = len(gauge_rows)
    for idx, row in enumerate(gauge_rows, start=1):
        gauge_addr = row[0]
        try:
            alive = bool(voter.functions.isAlive(Web3.to_checksum_address(gauge_addr)).call(block_identifier=boundary_block))
            if alive:
                active_rows.append(row)
        except Exception:
            pass

        if progress_every > 0 and idx % progress_every == 0:
            elapsed = max(time.time() - start, 1e-9)
            rate = idx / elapsed
            remaining = max(0, total - idx)
            eta = remaining / rate if rate > 0 else -1
            console.print(
                f"[dim]active-filter progress: {idx}/{total} | {rate:.2f}/s | ETA {_format_eta(eta)}[/dim]"
            )

    return active_rows


def limit_gauge_rows_for_smoke(
    conn: sqlite3.Connection,
    epoch: int,
    gauge_rows: List[Tuple[str, str, str, str]],
    max_gauges: int,
) -> List[Tuple[str, str, str, str]]:
    if max_gauges <= 0 or not gauge_rows:
        return gauge_rows

    cur = conn.cursor()
    cur.execute(
        """
        SELECT lower(gauge_address), SUM(COALESCE(usd_value, 0)) AS total_usd
        FROM bribes
        WHERE epoch = ?
        GROUP BY lower(gauge_address)
        ORDER BY total_usd DESC, lower(gauge_address) ASC
        LIMIT ?
        """,
        (epoch, max_gauges),
    )
    preferred = {row[0] for row in cur.fetchall() if row and row[0]}
    if not preferred:
        return gauge_rows[:max_gauges]

    limited = [r for r in gauge_rows if r[0].lower() in preferred]
    return limited[:max_gauges]


def load_token_metadata_map(conn: sqlite3.Connection, epoch: int) -> Dict[Tuple[str, str], Tuple[int, float]]:
    cur = conn.cursor()
    cur.execute(
        """
        SELECT lower(bribe_contract), lower(reward_token),
               MAX(COALESCE(token_decimals, 18)) AS token_decimals,
               MAX(COALESCE(usd_price, 0)) AS usd_price
        FROM bribes
        WHERE epoch = ?
        GROUP BY lower(bribe_contract), lower(reward_token)
        """,
        (epoch,),
    )
    return {(r[0], r[1]): (int(r[2] or 18), float(r[3] or 0.0)) for r in cur.fetchall()}


def enumerate_approved_tokens(
    conn: sqlite3.Connection,
    bribe_contract,
    bribe_addr: str,
    block_identifier: int,
    progress_every: int,
) -> List[str]:
    cur = conn.cursor()
    try:
        length = int(bribe_contract.functions.rewardsListLength().call(block_identifier=block_identifier))
    except Exception:
        return []

    tokens: List[str] = []
    now_ts = int(datetime.utcnow().timestamp())
    enum_start = time.time()
    for idx in range(length):
        try:
            token = str(bribe_contract.functions.rewardTokens(idx).call(block_identifier=block_identifier)).lower()
            if not token or not token.startswith("0x"):
                continue
            is_reward = bool(bribe_contract.functions.isRewardToken(Web3.to_checksum_address(token)).call(block_identifier=block_identifier))
            cur.execute(
                """
                INSERT OR REPLACE INTO bribe_reward_tokens(bribe_contract, reward_token, is_reward_token, updated_at)
                VALUES (?, ?, ?, ?)
                """,
                (bribe_addr.lower(), token, 1 if is_reward else 0, now_ts),
            )
            if is_reward:
                tokens.append(token)
        except Exception:
            continue
        if progress_every > 0 and (idx + 1) % progress_every == 0:
            elapsed = max(time.time() - enum_start, 1e-9)
            rate = (idx + 1) / elapsed
            remaining = max(0, length - (idx + 1))
            eta = remaining / rate if rate > 0 else -1
            console.print(
                f"[dim]approved-token progress for {bribe_addr[:10]}...: {idx + 1}/{length} | {rate:.2f} tokens/s | ETA {_format_eta(eta)}[/dim]"
            )

    conn.commit()
    return tokens


def upsert_snapshots(
    conn: sqlite3.Connection,
    epoch: int,
    vote_epoch: int,
    boundary_block: int,
    gauge_states: List[Tuple[str, str, float, float]],
    reward_rows: List[Tuple[str, str, str, str, int, float, float]],
) -> None:
    now_ts = int(datetime.utcnow().timestamp())
    cur = conn.cursor()

    cur.execute(
        """
        DELETE FROM boundary_gauge_values
        WHERE epoch = ? AND vote_epoch = ? AND active_only = 1
        """,
        (epoch, vote_epoch),
    )
    cur.execute(
        """
        DELETE FROM boundary_reward_snapshots
        WHERE epoch = ? AND vote_epoch = ? AND active_only = 1
        """,
        (epoch, vote_epoch),
    )

    cur.executemany(
        """
        INSERT OR REPLACE INTO boundary_gauge_values(
            epoch, vote_epoch, active_only, boundary_block, gauge_address, pool_address,
            votes_raw, total_usd, computed_at
        ) VALUES (?, ?, 1, ?, ?, ?, ?, ?, ?)
        """,
        [
            (epoch, vote_epoch, boundary_block, g, p, float(v), float(u), now_ts)
            for g, p, v, u in gauge_states
        ],
    )

    cur.executemany(
        """
        INSERT OR REPLACE INTO boundary_reward_snapshots(
            epoch, vote_epoch, active_only, boundary_block, gauge_address, bribe_contract,
            reward_token, rewards_raw, token_decimals, usd_price, total_usd, computed_at
        ) VALUES (?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (epoch, vote_epoch, boundary_block, g, b, t, rw, d, px, usd, now_ts)
            for g, b, t, rw, d, px, usd in reward_rows
        ],
    )

    conn.commit()


def main() -> None:
    parser = argparse.ArgumentParser(description="Collect boundary snapshots for offline max-return analysis")
    parser.add_argument("--end-epoch", type=int, required=True, help="Most recent closed epoch to collect")
    parser.add_argument("--weeks", type=int, default=13, help="How many weekly epochs to collect (default: 13 ~= 3 months)")
    parser.add_argument("--vote-epoch-offset-weeks", type=int, default=1, help="vote_epoch = epoch - offset*WEEK (default: 1)")
    parser.add_argument("--vote-epoch", type=int, default=None, help="Explicit vote_epoch override (recommended for single-epoch smoke tests)")
    parser.add_argument("--db", default=DATABASE_PATH, help="SQLite DB path")
    parser.add_argument("--rpc", default=os.getenv("RPC_URL") or "https://mainnet.base.org", help="RPC URL")
    parser.add_argument("--block-tolerance", type=int, default=60)
    parser.add_argument("--progress-every", type=int, default=100, help="Emit heartbeat logs every N items in long loops")
    parser.add_argument("--max-gauges", type=int, default=0, help="If > 0, limit active gauges for quick smoke tests")
    parser.add_argument(
        "--active-source",
        choices=["onchain", "db"],
        default="onchain",
        help="How to determine active gauges: onchain isAlive at boundary block (default) or db is_alive flag",
    )
    args = parser.parse_args()

    if not args.rpc:
        console.print("[red]RPC_URL missing[/red]")
        return

    w3 = Web3(Web3.HTTPProvider(args.rpc))
    if not w3.is_connected():
        console.print("[red]Failed to connect to RPC[/red]")
        return

    db = Database(args.db)
    db.create_tables()
    conn = sqlite3.connect(args.db)
    ensure_snapshot_tables(conn)

    voter = w3.eth.contract(address=Web3.to_checksum_address(VOTER_ADDRESS), abi=VOTER_ABI)

    epochs = [int(args.end_epoch - i * WEEK) for i in range(max(1, args.weeks))]

    console.print(f"[cyan]Collecting {len(epochs)} epochs ending at {args.end_epoch}[/cyan]")
    if args.vote_epoch is not None and len(epochs) > 1:
        console.print("[yellow]--vote-epoch override provided with multiple weeks; using same vote_epoch for every epoch.[/yellow]")

    for epoch_idx, epoch in enumerate(epochs, start=1):
        console.print(f"[bold cyan]Epoch {epoch_idx}/{len(epochs)}[/bold cyan] epoch={epoch}: start")
        boundary_block, inferred_vote_epoch = load_epoch_boundary(conn, epoch)
        if args.vote_epoch is not None:
            vote_epoch = int(args.vote_epoch)
        elif inferred_vote_epoch > 0:
            vote_epoch = inferred_vote_epoch
        else:
            vote_epoch = int(epoch - max(0, args.vote_epoch_offset_weeks) * WEEK)

        if boundary_block <= 0:
            boundary_block = find_block_at_timestamp(w3, epoch, args.block_tolerance)
        console.print(f"[cyan]Epoch {epoch}: boundary_block={boundary_block}, vote_epoch={vote_epoch}[/cyan]")

        gauge_rows = get_active_gauge_rows(conn, epoch)
        console.print(f"[cyan]Epoch {epoch}: gauges from DB before active-filter = {len(gauge_rows)}[/cyan]")
        if args.active_source == "onchain":
            gauge_rows = filter_active_gauge_rows_onchain(
                voter,
                gauge_rows,
                boundary_block,
                progress_every=max(0, args.progress_every),
            )
            console.print(f"[cyan]Epoch {epoch}: active gauges after on-chain filter = {len(gauge_rows)}[/cyan]")
        else:
            cur = conn.cursor()
            cur.execute("SELECT lower(address) FROM gauges WHERE COALESCE(is_alive, 1) = 1")
            db_active = {r[0] for r in cur.fetchall() if r and r[0]}
            gauge_rows = [r for r in gauge_rows if r[0].lower() in db_active]
            console.print(f"[cyan]Epoch {epoch}: active gauges after DB filter = {len(gauge_rows)}[/cyan]")

        gauge_rows = limit_gauge_rows_for_smoke(conn, epoch, gauge_rows, max(0, args.max_gauges))
        if not gauge_rows:
            console.print(f"[yellow]No active gauge rows found in DB for epoch {epoch}; skipping[/yellow]")
            continue

        pool_votes: Dict[str, float] = {}
        pool_vote_start = time.time()
        unique_pools = len({r[1] for r in gauge_rows if r and r[1]})
        console.print(f"[cyan]Epoch {epoch}: phase=pool_votes unique_pools={unique_pools}[/cyan]")
        for idx, (_gauge, pool_addr, _ib, _eb) in enumerate(gauge_rows, start=1):
            if pool_addr in pool_votes:
                continue
            try:
                weight = int(voter.functions.weightsAt(Web3.to_checksum_address(pool_addr), vote_epoch).call(block_identifier=boundary_block))
                pool_votes[pool_addr] = float(weight) / ONE_E18
            except Exception:
                pool_votes[pool_addr] = 0.0
            if args.progress_every > 0 and idx % args.progress_every == 0:
                elapsed = max(time.time() - pool_vote_start, 1e-9)
                rate = idx / elapsed
                remaining = max(0, len(gauge_rows) - idx)
                eta = remaining / rate if rate > 0 else -1
                console.print(
                    f"[dim]Epoch {epoch}: pool-vote progress {idx}/{len(gauge_rows)} | {rate:.2f}/s | ETA {_format_eta(eta)}[/dim]"
                )
        console.print(f"[cyan]Epoch {epoch}: pool_votes complete fetched={len(pool_votes)}[/cyan]")

        metadata_map = load_token_metadata_map(conn, epoch)

        gauge_total_usd: Dict[str, float] = defaultdict(float)
        reward_rows: List[Tuple[str, str, str, str, int, float, float]] = []
        console.print(f"[cyan]Epoch {epoch}: phase=rewards active_gauges={len(gauge_rows)}[/cyan]")

        reward_phase_start = time.time()
        bribes_scanned = 0
        tokens_checked = 0
        reward_calls = 0
        for gauge_idx, (gauge_addr, _pool_addr, internal_bribe, external_bribe) in enumerate(gauge_rows, start=1):
            for bribe_addr in (internal_bribe, external_bribe):
                if not bribe_addr or bribe_addr == "0x0000000000000000000000000000000000000000":
                    continue
                bribes_scanned += 1
                try:
                    bribe_contract = w3.eth.contract(address=Web3.to_checksum_address(bribe_addr), abi=BRIBE_ABI)
                except Exception:
                    continue

                approved_tokens = enumerate_approved_tokens(
                    conn,
                    bribe_contract,
                    bribe_addr,
                    boundary_block,
                    progress_every=max(0, args.progress_every),
                )
                for token_idx, token in enumerate(approved_tokens, start=1):
                    tokens_checked += 1
                    try:
                        rd = bribe_contract.functions.rewardData(Web3.to_checksum_address(token), vote_epoch).call(block_identifier=boundary_block)
                        rewards_raw = int(rd[1])
                        reward_calls += 1
                    except Exception:
                        continue

                    token_decimals, usd_price = metadata_map.get((bribe_addr.lower(), token.lower()), (18, 0.0))
                    amount_human = rewards_raw / (10 ** max(0, token_decimals))
                    token_usd = amount_human * float(usd_price)

                    gauge_total_usd[gauge_addr] += token_usd
                    reward_rows.append(
                        (
                            gauge_addr.lower(),
                            bribe_addr.lower(),
                            token.lower(),
                            str(rewards_raw),
                            int(token_decimals),
                            float(usd_price),
                            float(token_usd),
                        )
                    )
                    if args.progress_every > 0 and token_idx % args.progress_every == 0:
                        elapsed = max(time.time() - reward_phase_start, 1e-9)
                        rate = token_idx / elapsed
                        remaining = max(0, len(approved_tokens) - token_idx)
                        eta = remaining / rate if rate > 0 else -1
                        console.print(
                            f"[dim]Epoch {epoch}: rewardData progress gauge {gauge_idx}/{len(gauge_rows)}, token {token_idx}/{len(approved_tokens)} | {rate:.2f}/s | ETA {_format_eta(eta)}[/dim]"
                        )
            if args.progress_every > 0 and gauge_idx % args.progress_every == 0:
                elapsed = max(time.time() - reward_phase_start, 1e-9)
                rate = gauge_idx / elapsed
                remaining = max(0, len(gauge_rows) - gauge_idx)
                eta = remaining / rate if rate > 0 else -1
                console.print(
                    f"[dim]Epoch {epoch}: gauge progress {gauge_idx}/{len(gauge_rows)} | {rate:.2f}/s | ETA {_format_eta(eta)}[/dim]"
                )
            if args.progress_every > 0 and gauge_idx % max(1, args.progress_every // 2) == 0:
                elapsed = max(time.time() - reward_phase_start, 1e-9)
                gauge_rate = gauge_idx / elapsed
                console.print(
                    f"[dim]Epoch {epoch}: reward phase heartbeat gauges={gauge_idx}/{len(gauge_rows)} bribes={bribes_scanned} tokens={tokens_checked} reward_calls={reward_calls} gauge_rate={gauge_rate:.2f}/s[/dim]"
                )

        state_rows: List[Tuple[str, str, float, float]] = []
        for gauge_addr, pool_addr, _ib, _eb in gauge_rows:
            total_usd = float(gauge_total_usd.get(gauge_addr.lower(), 0.0))
            if total_usd <= 0:
                continue
            state_rows.append(
                (
                    gauge_addr.lower(),
                    pool_addr.lower(),
                    float(pool_votes.get(pool_addr.lower(), 0.0)),
                    total_usd,
                )
            )

        upsert_snapshots(conn, epoch, vote_epoch, boundary_block, state_rows, reward_rows)

        cur = conn.cursor()
        cur.execute(
            """
            SELECT COUNT(*)
            FROM boundary_gauge_values
            WHERE epoch = ? AND vote_epoch = ? AND active_only = 1
            """,
            (epoch, vote_epoch),
        )
        gauge_rows_written = int(cur.fetchone()[0] or 0)
        cur.execute(
            """
            SELECT COUNT(*)
            FROM boundary_reward_snapshots
            WHERE epoch = ? AND vote_epoch = ? AND active_only = 1
            """,
            (epoch, vote_epoch),
        )
        reward_rows_written = int(cur.fetchone()[0] or 0)
        cur.execute(
            """
            SELECT COUNT(DISTINCT epoch)
            FROM boundary_gauge_values
            WHERE active_only = 1
            """
        )
        distinct_epochs = int(cur.fetchone()[0] or 0)

        console.print(
            f"[green]epoch={epoch} vote_epoch={vote_epoch} block={boundary_block} "
            f"gauges={len(state_rows)} rewards={len(reward_rows)}[/green]"
        )
        console.print(
            f"[cyan]DB summary: boundary_gauge_values={gauge_rows_written} rows, "
            f"boundary_reward_snapshots={reward_rows_written} rows for epoch={epoch}/vote_epoch={vote_epoch}; "
            f"distinct epochs in cache={distinct_epochs}[/cyan]"
        )

    cur = conn.cursor()
    cur.execute(
        """
        SELECT COUNT(DISTINCT epoch)
        FROM boundary_gauge_values
        WHERE active_only = 1
        """
    )
    final_epochs = int(cur.fetchone()[0] or 0)
    conn.close()
    console.print(f"[cyan]Final cache coverage: {final_epochs} distinct epochs[/cyan]")
    console.print("[bold green]✅ Boundary snapshot collection complete[/bold green]")


if __name__ == "__main__":
    main()
