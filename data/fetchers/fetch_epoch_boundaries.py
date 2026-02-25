#!/usr/bin/env python3
"""
Fetch canonical epoch boundary blocks from Minter Mint events.

Canonical boundary definition:
- boundary_block is the first successful update_period() tx, emitted via Mint event
- epoch timestamp is floor(block.timestamp / WEEK) * WEEK
- vote_epoch = epoch - WEEK
"""

import argparse
import os
import sqlite3
import time
from datetime import datetime
from typing import Dict, Iterable, List, Optional, Tuple

from dotenv import load_dotenv
from rich.console import Console
from web3 import Web3

from config.settings import DATABASE_PATH, VOTER_ADDRESS, WEEK

load_dotenv()
console = Console()

VOTER_ABI = [
    {
        "inputs": [],
        "name": "minter",
        "outputs": [{"internalType": "address", "name": "", "type": "address"}],
        "stateMutability": "view",
        "type": "function",
    }
]

MINT_EVENT_V2 = "Mint(address,uint256,uint256)"
MINT_EVENT_V3 = "Mint(address,uint256,uint256,uint256)"


def ensure_epoch_boundaries_table(conn: sqlite3.Connection) -> None:
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS epoch_boundaries (
            epoch INTEGER NOT NULL PRIMARY KEY,
            boundary_block INTEGER NOT NULL,
            boundary_timestamp INTEGER NOT NULL,
            vote_epoch INTEGER NOT NULL,
            reward_epoch INTEGER NOT NULL,
            source_tag TEXT,
            computed_at INTEGER NOT NULL
        )
        """
    )
    cur.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_epoch_boundaries_block
        ON epoch_boundaries(boundary_block)
        """
    )
    conn.commit()


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


def resolve_epoch_range(
    conn: sqlite3.Connection,
    start_epoch: Optional[int],
    end_epoch: Optional[int],
) -> Tuple[int, int]:
    if start_epoch is not None and end_epoch is not None:
        return int(start_epoch), int(end_epoch)

    cur = conn.cursor()
    row = cur.execute(
        """
        SELECT MIN(epoch), MAX(epoch)
        FROM boundary_reward_snapshots
        WHERE active_only = 1
        """
    ).fetchone()
    if not row or row[0] is None or row[1] is None:
        raise ValueError("Could not infer epoch range from boundary_reward_snapshots; pass --start-epoch/--end-epoch")

    return int(row[0]), int(row[1])


def resolve_minter_address(w3: Web3, voter_address: str, override: Optional[str]) -> str:
    if override:
        return Web3.to_checksum_address(override)

    voter = w3.eth.contract(address=Web3.to_checksum_address(voter_address), abi=VOTER_ABI)
    minter = voter.functions.minter().call()
    return Web3.to_checksum_address(minter)


def _epochs_between(start_epoch: int, end_epoch: int) -> List[int]:
    out: List[int] = []
    current = int(start_epoch)
    while current <= int(end_epoch):
        out.append(int(current))
        current += int(WEEK)
    return out


def resolve_epoch_block_hints(conn: sqlite3.Connection, start_epoch: int, end_epoch: int) -> Dict[int, int]:
    """Use existing snapshot tables to find approximate boundary blocks per epoch."""
    cur = conn.cursor()
    hints: Dict[int, int] = {}

    for table_name in ("boundary_reward_snapshots", "boundary_gauge_values"):
        try:
            rows = cur.execute(
                f"""
                SELECT epoch, MAX(COALESCE(boundary_block, 0)) AS boundary_block
                FROM {table_name}
                WHERE active_only = 1
                  AND epoch BETWEEN ? AND ?
                  AND COALESCE(boundary_block, 0) > 0
                GROUP BY epoch
                """,
                (int(start_epoch), int(end_epoch)),
            ).fetchall()
        except sqlite3.OperationalError:
            continue

        for epoch, block in rows:
            epoch_i = int(epoch)
            block_i = int(block or 0)
            if block_i <= 0:
                continue
            existing = hints.get(epoch_i, 0)
            if existing <= 0 or block_i > 0:
                hints[epoch_i] = block_i

    return hints


def _log_chunks(start: int, end: int, size: int) -> Iterable[Tuple[int, int]]:
    cur = int(start)
    while cur <= end:
        to_block = min(end, cur + size - 1)
        yield cur, to_block
        cur = to_block + 1


def fetch_mint_logs(
    w3: Web3,
    minter_address: str,
    start_block: int,
    end_block: int,
    chunk_size: int,
    max_retries: int,
    min_split_span: int,
    heartbeat_seconds: int,
) -> List[dict]:
    topic_v2 = w3.keccak(text=MINT_EVENT_V2).hex()
    topic_v3 = w3.keccak(text=MINT_EVENT_V3).hex()

    logs: List[dict] = []
    total_chunks = ((end_block - start_block) // max(1, chunk_size)) + 1
    processed = 0

    last_heartbeat = time.time()

    def _fetch_range(from_block: int, to_block: int, topic: str) -> List[dict]:
        nonlocal last_heartbeat
        now = time.time()
        if now - last_heartbeat >= max(1, int(heartbeat_seconds)):
            console.print(
                f"[dim]Log scan heartbeat: querying {from_block}-{to_block} topic={topic[:10]}..[/dim]"
            )
            last_heartbeat = now

        last_error: Optional[Exception] = None
        for _ in range(max(1, int(max_retries))):
            try:
                return w3.eth.get_logs(
                    {
                        "fromBlock": int(from_block),
                        "toBlock": int(to_block),
                        "address": minter_address,
                        "topics": [topic],
                    }
                )
            except Exception as exc:
                last_error = exc
                time.sleep(0.35)

        span = int(to_block) - int(from_block)
        if from_block >= to_block or span <= max(0, int(min_split_span)):
            if last_error is not None:
                console.print(
                    f"[yellow]Log query dropped range {from_block}-{to_block} topic={topic[:10]}..: {last_error}[/yellow]"
                )
            return []

        mid = (int(from_block) + int(to_block)) // 2
        left_logs = _fetch_range(int(from_block), int(mid), topic)
        right_logs = _fetch_range(int(mid) + 1, int(to_block), topic)
        return left_logs + right_logs

    for from_block, to_block in _log_chunks(start_block, end_block, chunk_size):
        for topic in (topic_v2, topic_v3):
            logs.extend(_fetch_range(from_block, to_block, topic))
        processed += 1
        if processed % max(1, total_chunks // 10) == 0:
            console.print(f"[dim]Log scan progress: {processed}/{total_chunks} chunks[/dim]")

    return logs


def fetch_mint_logs_around_hints(
    w3: Web3,
    minter_address: str,
    epoch_block_hints: Dict[int, int],
    half_window_blocks: int,
    max_retries: int,
    min_split_span: int,
    heartbeat_seconds: int,
) -> List[dict]:
    topic_v2 = w3.keccak(text=MINT_EVENT_V2).hex()
    topic_v3 = w3.keccak(text=MINT_EVENT_V3).hex()

    latest_block = int(w3.eth.block_number)
    logs: List[dict] = []

    last_heartbeat = time.time()

    def _fetch_range(from_block: int, to_block: int, topic: str) -> List[dict]:
        nonlocal last_heartbeat
        now = time.time()
        if now - last_heartbeat >= max(1, int(heartbeat_seconds)):
            console.print(
                f"[dim]Hint-scan heartbeat: querying {from_block}-{to_block} topic={topic[:10]}..[/dim]"
            )
            last_heartbeat = now

        last_error: Optional[Exception] = None
        for _ in range(max(1, int(max_retries))):
            try:
                return w3.eth.get_logs(
                    {
                        "fromBlock": int(from_block),
                        "toBlock": int(to_block),
                        "address": minter_address,
                        "topics": [topic],
                    }
                )
            except Exception as exc:
                last_error = exc
                time.sleep(0.35)

        span = int(to_block) - int(from_block)
        if from_block >= to_block or span <= max(0, int(min_split_span)):
            if last_error is not None:
                console.print(
                    f"[yellow]Hint-range log query dropped {from_block} topic={topic[:10]}..: {last_error}[/yellow]"
                )
            return []

        mid = (int(from_block) + int(to_block)) // 2
        left = _fetch_range(int(from_block), int(mid), topic)
        right = _fetch_range(int(mid) + 1, int(to_block), topic)
        return left + right

    epochs = sorted(epoch_block_hints.keys())
    total = len(epochs)
    for idx, epoch in enumerate(epochs, start=1):
        hint_block = int(epoch_block_hints[epoch])
        from_block = max(0, hint_block - int(half_window_blocks))
        to_block = min(latest_block, hint_block + int(half_window_blocks))

        for topic in (topic_v2, topic_v3):
            logs.extend(_fetch_range(from_block, to_block, topic))

        if total > 0 and idx % max(1, total // 10) == 0:
            console.print(f"[dim]Hint log scan progress: {idx}/{total} epochs[/dim]")

    return logs


def upsert_epoch_boundaries(
    conn: sqlite3.Connection,
    entries: Dict[int, Tuple[int, int, int]],
) -> int:
    cur = conn.cursor()
    now_ts = int(time.time())
    inserted = 0

    for epoch_ts, (block_number, log_index, block_ts) in sorted(entries.items()):
        vote_epoch = int(epoch_ts - WEEK)
        reward_epoch = int(epoch_ts)
        if vote_epoch <= 0:
            continue

        existing = cur.execute(
            "SELECT boundary_block FROM epoch_boundaries WHERE epoch = ?",
            (int(epoch_ts),),
        ).fetchone()
        if existing and existing[0] is not None and int(existing[0]) <= int(block_number):
            continue

        cur.execute(
            """
            INSERT OR REPLACE INTO epoch_boundaries(
                epoch, boundary_block, boundary_timestamp,
                vote_epoch, reward_epoch, source_tag, computed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                int(epoch_ts),
                int(block_number),
                int(block_ts),
                int(vote_epoch),
                int(reward_epoch),
                "minter_mint_event",
                now_ts,
            ),
        )
        inserted += 1

    conn.commit()
    return inserted


def main() -> None:
    parser = argparse.ArgumentParser(description="Fetch canonical epoch boundaries from Minter Mint events")
    parser.add_argument("--db", default=DATABASE_PATH, help="SQLite DB path")
    parser.add_argument("--rpc", default=os.getenv("RPC_URL"), help="RPC URL")
    parser.add_argument("--minter", default=None, help="Minter address override")
    parser.add_argument("--start-epoch", type=int, default=None, help="Earliest epoch timestamp to cover")
    parser.add_argument("--end-epoch", type=int, default=None, help="Latest epoch timestamp to cover")
    parser.add_argument("--start-block", type=int, default=None, help="Optional start block for log scan")
    parser.add_argument("--end-block", type=int, default=None, help="Optional end block for log scan")
    parser.add_argument("--block-tolerance", type=int, default=120, help="Timestamp tolerance for block lookup")
    parser.add_argument("--chunk-size", type=int, default=5000, help="Log query chunk size")
    parser.add_argument("--buffer-blocks", type=int, default=5000, help="Block buffer for log scan range")
    parser.add_argument("--max-retries", type=int, default=3, help="Retries per get_logs request before splitting range")
    parser.add_argument("--min-split-span", type=int, default=20, help="Stop recursive range splitting at this span (blocks) and skip")
    parser.add_argument("--heartbeat-seconds", type=int, default=10, help="Emit heartbeat logs every N seconds during deep retries")
    parser.add_argument(
        "--scan-strategy",
        choices=["around-hints", "wide-range"],
        default="around-hints",
        help="around-hints: scan per-epoch around estimated blocks (faster). wide-range: scan one broad block range.",
    )
    parser.add_argument(
        "--hint-window-blocks",
        type=int,
        default=3000,
        help="Half-window block radius around each estimated epoch boundary block when using around-hints",
    )
    args = parser.parse_args()

    if not args.rpc:
        console.print("[red]RPC_URL missing[/red]")
        return

    conn = sqlite3.connect(args.db)
    ensure_epoch_boundaries_table(conn)

    start_epoch, end_epoch = resolve_epoch_range(conn, args.start_epoch, args.end_epoch)
    console.print(
        f"[cyan]Epoch range: {start_epoch} ({datetime.utcfromtimestamp(start_epoch).isoformat()} UTC) -> "
        f"{end_epoch} ({datetime.utcfromtimestamp(end_epoch).isoformat()} UTC)[/cyan]"
    )

    try:
        rpc_timeout = int(os.getenv("RPC_TIMEOUT", "30"))
    except Exception:
        rpc_timeout = 30
    rpc_timeout = max(5, rpc_timeout)

    w3 = Web3(Web3.HTTPProvider(args.rpc, request_kwargs={"timeout": rpc_timeout}))
    if not w3.is_connected():
        console.print("[red]Failed to connect to RPC[/red]")
        return

    minter_address = resolve_minter_address(w3, VOTER_ADDRESS, args.minter)
    console.print(f"[cyan]Minter address: {minter_address}[/cyan]")

    if args.start_block is not None and args.end_block is not None:
        start_block = int(args.start_block)
        end_block = int(args.end_block)
    else:
        start_block = max(
            0,
            find_block_at_timestamp(w3, int(start_epoch), args.block_tolerance) - int(args.buffer_blocks),
        )
        end_block = (
            find_block_at_timestamp(w3, int(end_epoch + WEEK), args.block_tolerance)
            + int(args.buffer_blocks)
        )

    console.print(f"[cyan]Scanning logs blocks {start_block} -> {end_block}[/cyan]")

    if args.scan_strategy == "around-hints":
        hint_map = resolve_epoch_block_hints(conn, start_epoch, end_epoch)
        for epoch in _epochs_between(start_epoch, end_epoch):
            if epoch in hint_map and int(hint_map[epoch]) > 0:
                continue
            hint_map[epoch] = int(find_block_at_timestamp(w3, int(epoch), args.block_tolerance))

        console.print(
            f"[cyan]Hint scan mode: epochs={len(hint_map)}, half_window={int(args.hint_window_blocks)} blocks[/cyan]"
        )
        logs = fetch_mint_logs_around_hints(
            w3,
            minter_address,
            hint_map,
            int(args.hint_window_blocks),
            int(args.max_retries),
            int(args.min_split_span),
            int(args.heartbeat_seconds),
        )
    else:
        logs = fetch_mint_logs(
            w3,
            minter_address,
            start_block,
            end_block,
            int(args.chunk_size),
            int(args.max_retries),
            int(args.min_split_span),
            int(args.heartbeat_seconds),
        )
    if not logs:
        console.print("[yellow]No Mint logs found in range[/yellow]")
        return

    logs_sorted = sorted(logs, key=lambda l: (l["blockNumber"], l.get("logIndex", 0)))
    block_ts_cache: Dict[int, int] = {}
    entries: Dict[int, Tuple[int, int, int]] = {}

    for log in logs_sorted:
        block_number = int(log["blockNumber"])
        log_index = int(log.get("logIndex", 0))
        if block_number in block_ts_cache:
            block_ts = block_ts_cache[block_number]
        else:
            block_ts = int(w3.eth.get_block(block_number)["timestamp"])
            block_ts_cache[block_number] = block_ts

        epoch_ts = int((block_ts // WEEK) * WEEK)
        if epoch_ts < start_epoch or epoch_ts > end_epoch:
            continue

        existing = entries.get(epoch_ts)
        if existing is None or (block_number, log_index) < (existing[0], existing[1]):
            entries[epoch_ts] = (block_number, log_index, block_ts)

    if not entries:
        console.print("[yellow]No Mint logs matched epoch range[/yellow]")
        return

    inserted = upsert_epoch_boundaries(conn, entries)
    console.print(f"[green]✓ Upserted {inserted} epoch boundary rows[/green]")

    conn.close()


if __name__ == "__main__":
    main()
