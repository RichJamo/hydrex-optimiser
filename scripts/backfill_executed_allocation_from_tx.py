#!/usr/bin/env python3

import argparse
import os
import re
import sqlite3
import sys
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

from dotenv import load_dotenv
from web3 import Web3

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config.settings import DATABASE_PATH, WEEK
from src.allocation_tracking import save_executed_allocation


PARTNER_ESCROW_ABI = [
    {
        "inputs": [
            {"internalType": "address[]", "name": "_poolVote", "type": "address[]"},
            {"internalType": "uint256[]", "name": "_voteProportions", "type": "uint256[]"},
        ],
        "name": "vote",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    }
]


def _load_run(conn: sqlite3.Connection, run_id: int) -> Optional[Tuple[int, int, Optional[str], Optional[int], Optional[int], str]]:
    row = conn.execute(
        """
        SELECT id, vote_epoch, tx_hash, snapshot_ts, vote_sent_at, status
        FROM auto_vote_runs
        WHERE id = ?
        LIMIT 1
        """,
        (int(run_id),),
    ).fetchone()
    if not row:
        return None
    return int(row[0]), int(row[1] or 0), (str(row[2]) if row[2] else None), (int(row[3]) if row[3] else None), (int(row[4]) if row[4] else None), str(row[5] or "")


def _find_logs_for_run(log_dir: Path, run_id: int) -> List[Path]:
    pattern = re.compile(rf"run_id={int(run_id)}\b")
    matches: List[Path] = []
    if not log_dir.exists():
        return matches
    for log_path in sorted(log_dir.glob("*.log")):
        try:
            text = log_path.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        if pattern.search(text):
            matches.append(log_path)
    return matches


def _extract_total_votes_from_log(text: str) -> Optional[int]:
    m = re.search(r"Allocation validated:\s*([0-9,]+)\s*/\s*([0-9,]+)\s*votes", text)
    if not m:
        return None
    lhs = int(m.group(1).replace(",", ""))
    rhs = int(m.group(2).replace(",", ""))
    return lhs if lhs == rhs else lhs


def _infer_total_votes(log_paths: Sequence[Path], fallback: int) -> int:
    for p in log_paths:
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        inferred = _extract_total_votes_from_log(text)
        if inferred and inferred > 0:
            return int(inferred)
    return int(max(0, fallback))


def _decode_vote_tx(rpc_url: str, tx_hash: str) -> Tuple[List[str], List[int]]:
    w3 = Web3(Web3.HTTPProvider(rpc_url))
    if not w3.is_connected():
        raise RuntimeError("Failed to connect to RPC")

    tx = w3.eth.get_transaction(tx_hash)
    to_addr = tx.get("to")
    if not to_addr:
        raise RuntimeError("Transaction has no recipient address")

    contract = w3.eth.contract(address=Web3.to_checksum_address(to_addr), abi=PARTNER_ESCROW_ABI)
    _func, decoded = contract.decode_function_input(tx["input"])

    pools_raw = decoded.get("_poolVote") or decoded.get("poolVote") or []
    weights_raw = decoded.get("_voteProportions") or decoded.get("voteProportions") or []
    pools = [str(p).lower() for p in pools_raw]
    weights = [int(w) for w in weights_raw]

    if len(pools) != len(weights):
        raise RuntimeError("Decoded vote arrays have mismatched lengths")
    if not pools:
        raise RuntimeError("Decoded vote arrays are empty")

    return pools, weights


def _weights_to_votes(weights: Sequence[int], total_votes: int) -> List[int]:
    if total_votes <= 0:
        return [0 for _ in weights]

    total_weight = int(sum(max(0, int(w)) for w in weights))
    if total_weight <= 0:
        raise RuntimeError("Vote proportions sum to zero")

    raw = [float(int(w)) * float(total_votes) / float(total_weight) for w in weights]
    base = [int(v) for v in raw]
    remainder = int(total_votes) - int(sum(base))

    order = sorted(range(len(raw)), key=lambda i: (raw[i] - base[i]), reverse=True)
    for idx in order[:max(0, remainder)]:
        base[idx] += 1
    return base


def _map_pool_to_gauge(conn: sqlite3.Connection, snapshot_ts: Optional[int], pool: str) -> str:
    pool_l = str(pool).lower()
    if snapshot_ts is None:
        row = conn.execute(
            """
            SELECT lower(gauge_address)
            FROM live_gauge_snapshots
            WHERE lower(pool_address) = ?
            ORDER BY snapshot_ts DESC
            LIMIT 1
            """,
            (pool_l,),
        ).fetchone()
    else:
        row = conn.execute(
            """
            SELECT lower(gauge_address)
            FROM live_gauge_snapshots
            WHERE snapshot_ts = ? AND lower(pool_address) = ?
            LIMIT 1
            """,
            (int(snapshot_ts), pool_l),
        ).fetchone()

    if row and row[0]:
        return str(row[0]).lower()
    return pool_l


def main() -> None:
    load_dotenv()

    parser = argparse.ArgumentParser(description="Backfill executed_allocations for an auto_vote_runs entry from tx calldata")
    parser.add_argument("--run-id", type=int, required=True, help="auto_vote_runs.id to backfill")
    parser.add_argument("--db-path", default=DATABASE_PATH, help="SQLite DB path")
    parser.add_argument("--rpc", default=os.getenv("RPC_URL", ""), help="RPC URL (defaults to RPC_URL env)")
    parser.add_argument("--total-votes", type=int, default=int(os.getenv("YOUR_VOTING_POWER", "0")), help="Fallback total votes if log inference is unavailable")
    parser.add_argument("--log-dir", default="logs/auto_voter", help="Directory to scan for run logs")
    args = parser.parse_args()

    if not args.rpc:
        raise SystemExit("--rpc is required (or set RPC_URL)")

    conn = sqlite3.connect(args.db_path)
    try:
        run = _load_run(conn, int(args.run_id))
        if not run:
            raise SystemExit(f"run_id={args.run_id} not found")

        run_id, vote_epoch, tx_hash, snapshot_ts, _vote_sent_at, status = run
        if status != "tx_success":
            raise SystemExit(f"run_id={run_id} status={status} (requires tx_success)")
        if not tx_hash:
            raise SystemExit(f"run_id={run_id} has no tx_hash")
        if vote_epoch <= 0:
            raise SystemExit(f"run_id={run_id} has invalid vote_epoch={vote_epoch}")

        log_paths = _find_logs_for_run(Path(args.log_dir), run_id)
        inferred_total_votes = _infer_total_votes(log_paths=log_paths, fallback=int(args.total_votes))
        if inferred_total_votes <= 0:
            raise SystemExit("Could not infer total votes from logs and fallback --total-votes is not > 0")

        pools, weights = _decode_vote_tx(rpc_url=args.rpc, tx_hash=tx_hash)
        votes = _weights_to_votes(weights=weights, total_votes=inferred_total_votes)

        rows = []
        for idx, (pool, vote_amt) in enumerate(zip(pools, votes), start=1):
            gauge = _map_pool_to_gauge(conn=conn, snapshot_ts=snapshot_ts, pool=pool)
            rows.append((int(idx), str(gauge), str(pool), int(vote_amt)))

        target_epoch = int(vote_epoch) + int(WEEK)
        strategy_tag = f"auto_voter_run_{run_id}"
        source = f"backfill_tx:{tx_hash[:12]}"

        inserted = save_executed_allocation(
            conn=conn,
            epoch=int(target_epoch),
            strategy_tag=strategy_tag,
            rows=rows,
            source=source,
            tx_hash=str(tx_hash),
        )

        print("✓ Backfill complete")
        print(f"run_id={run_id}")
        print(f"strategy_tag={strategy_tag}")
        print(f"target_epoch={target_epoch}")
        print(f"rows={inserted}")
        print(f"inferred_total_votes={inferred_total_votes}")
        print(f"snapshot_ts={snapshot_ts}")
        print(f"tx_hash={tx_hash}")
        if log_paths:
            print(f"log_source={log_paths[0]}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
