#!/usr/bin/env python3
"""
Boundary-based theoretical max return analysis.

Uses on-chain state at/for the closed epoch boundary:
- Vote totals per gauge: VoterV5.weightsAt(pool, vote_epoch) where vote_epoch is the CLOSED epoch
- Reward totals per bribe token: Bribe.rewardData(token, vote_epoch) at the SAME vote_epoch

═══ CANONICAL APPROACH (per Smart Contract Reference) ═══

1. VOTE EPOCH DEFINITION:
   - vote_epoch = timestamp of the CLOSED epoch (e.g., 1709251200 for Thu 2024-02-29 00:00 UTC)
   - At flip, _epochTimestamp() changes to vote_epoch + WEEK, but historical queries use vote_epoch
   - Never query at _epochTimestamp() when calculating historical rewards

2. AUTHORITATIVE DATA SOURCES:
   - Votes: VoterV5.weightsAt(pool_address, vote_epoch) → canonical vote snapshot
   - Rewards: Bribe.rewardData(token, vote_epoch) → canonical reward snapshot
   - CRITICAL: Use the SAME epoch timestamp for both queries

3. GUARDRAILS (Implemented):
   - All-zero weights → vote_epoch is misaligned (fail fast with recommendations)
   - Sparse weights (< 1/3 nonzero) → warn of partial epoch coverage
   - All-zero rewards despite valid votes → rewards queried at wrong epoch (warn + recommend wait)
   - Auto-detection provides diagnostics; respects manual --vote-epoch override

4. MULTI-EPOCH GENERALIZATION:
   - No hardcoding of epoch offsets (1-week or otherwise)
   - vote_epoch auto-detected via weightsAt sampling across recent days
   - Each epoch can have different vote/reward structure

Assumptions:
- Other voters' allocations are fixed at boundary
- Your allocation is added on top of boundary totals (same assumption as prior optimizer scripts)
- Rewards finalized after epoch close (rewardData immutable post-periodFinish)
"""

import argparse
import json
import math
import os
import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from itertools import combinations
from typing import Dict, List, Tuple

from dotenv import load_dotenv
from rich.console import Console
from rich.panel import Panel
from rich.progress import track
from rich.table import Table
from web3 import Web3

load_dotenv()
console = Console()

ONE_E18 = 10**18
WEEK_SECONDS = 7 * 24 * 60 * 60

# Minimal ABIs
VOTER_ABI = [
    {
        "inputs": [{"internalType": "address", "name": "_gauge", "type": "address"}],
        "name": "isAlive",
        "outputs": [{"internalType": "bool", "name": "", "type": "bool"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [
            {"internalType": "address", "name": "_pool", "type": "address"},
            {"internalType": "uint256", "name": "_time", "type": "uint256"},
        ],
        "name": "weightsAt",
        "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    }
]

BRIBE_ABI = [
    {
        "inputs": [],
        "name": "WEEK",
        "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
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

POOL_ABI = [
    {
        "inputs": [],
        "name": "token0",
        "outputs": [{"internalType": "address", "name": "", "type": "address"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "token1",
        "outputs": [{"internalType": "address", "name": "", "type": "address"}],
        "stateMutability": "view",
        "type": "function",
    },
]

ERC20_SYMBOL_ABI = [
    {
        "inputs": [],
        "name": "symbol",
        "outputs": [{"internalType": "string", "name": "", "type": "string"}],
        "stateMutability": "view",
        "type": "function",
    }
]

ERC20_SYMBOL_BYTES32_ABI = [
    {
        "inputs": [],
        "name": "symbol",
        "outputs": [{"internalType": "bytes32", "name": "", "type": "bytes32"}],
        "stateMutability": "view",
        "type": "function",
    }
]


@dataclass
class GaugeBoundaryState:
    gauge: str
    pool: str
    votes_raw: float
    total_usd: float


def ensure_boundary_cache_table(conn: sqlite3.Connection) -> None:
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS boundary_gauge_values (
            epoch INTEGER NOT NULL,
            vote_epoch INTEGER,
            boundary_block INTEGER NOT NULL,
            gauge_address TEXT NOT NULL,
            pool_address TEXT NOT NULL,
            votes_raw TEXT NOT NULL,
            total_usd REAL NOT NULL,
            computed_at INTEGER NOT NULL,
            PRIMARY KEY (epoch, boundary_block, gauge_address)
        )
        """
    )
    cur.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_boundary_gauge_values_epoch_block
        ON boundary_gauge_values(epoch, boundary_block)
        """
    )
    cur.execute("PRAGMA table_info(boundary_gauge_values)")
    columns = [r[1] for r in cur.fetchall()]
    if "vote_epoch" not in columns:
        cur.execute("ALTER TABLE boundary_gauge_values ADD COLUMN vote_epoch INTEGER")
    cur.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_boundary_gauge_values_epoch_vote_epoch_block
        ON boundary_gauge_values(epoch, vote_epoch, boundary_block)
        """
    )
    conn.commit()


def load_cached_states(
    cur: sqlite3.Cursor, epoch: int, vote_epoch: int, boundary_block: int, active_only: bool
) -> List[GaugeBoundaryState]:
    active_clause = "AND COALESCE(g.is_alive, 1) = 1" if active_only else ""
    cur.execute(
        f"""
        SELECT
            c.gauge_address,
            c.pool_address,
            c.votes_raw,
            c.total_usd
        FROM boundary_gauge_values c
        LEFT JOIN gauges g ON lower(g.address) = lower(c.gauge_address)
        WHERE c.epoch = ?
                    AND c.vote_epoch = ?
          AND c.boundary_block = ?
          {active_clause}
          AND c.total_usd > 0
        """,
                (epoch, vote_epoch, boundary_block),
    )
    rows = cur.fetchall()

    states: List[GaugeBoundaryState] = []
    for gauge, pool, votes_raw, total_usd in rows:
        states.append(
            GaugeBoundaryState(
                gauge=str(gauge).lower(),
                pool=str(pool).lower(),
                votes_raw=float(votes_raw or 0),
                total_usd=float(total_usd or 0),
            )
        )

    return states


def find_best_cached_block_for_epoch(cur: sqlite3.Cursor, epoch: int, vote_epoch: int) -> int:
    cur.execute(
        """
        SELECT boundary_block
        FROM boundary_gauge_values
                WHERE epoch = ?
                    AND vote_epoch = ?
        GROUP BY boundary_block
        ORDER BY COUNT(*) DESC, boundary_block DESC
        LIMIT 1
        """,
                (epoch, vote_epoch),
    )
    row = cur.fetchone()
    return int(row[0]) if row else -1


def save_states_to_cache(
    conn: sqlite3.Connection,
    states: List[GaugeBoundaryState],
    epoch: int,
    vote_epoch: int,
    boundary_block: int,
) -> None:
    if not states:
        return

    cur = conn.cursor()
    now_ts = int(datetime.utcnow().timestamp())

    cur.executemany(
        """
        INSERT OR REPLACE INTO boundary_gauge_values (
            epoch, vote_epoch, boundary_block, gauge_address, pool_address, votes_raw, total_usd, computed_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                epoch,
                vote_epoch,
                boundary_block,
                s.gauge.lower(),
                str(s.pool).lower(),
                str(float(s.votes_raw)),
                float(s.total_usd),
                now_ts,
            )
            for s in states
        ],
    )
    conn.commit()


def autodetect_vote_epoch(
    voter_contract,
    pool_set: List[str],
    boundary_block: int,
    epoch_hint: int,
    scan_days: int,
    sample_pools: int,
) -> int:
    """
    Auto-detect the correct vote_epoch by sampling pools across recent epochs.
    
    Canonical principle: weightsAt(pool, E) returns nonzero votes only when queried at epoch E
    (the CLOSED epoch where voting occurred). If all sampled pools return 0, the epoch is misaligned.
    
    Returns: epoch timestamp with best nonzero vote alignment, or epoch_hint if detection inconclusive.
    """
    pools = [p for p in pool_set if p][: max(1, sample_pools)]
    if not pools:
        return epoch_hint

    candidates = [int(epoch_hint - d * 86400) for d in range(max(0, scan_days) + 1)]
    
    # Track statistics for each candidate
    results: Dict[int, Tuple[int, int]] = {}  # epoch -> (nonzero_count, total_votes)
    
    for candidate in candidates:
        nonzero = 0
        total_votes = 0
        for pool in pools:
            try:
                v = int(
                    voter_contract.functions.weightsAt(Web3.to_checksum_address(pool), candidate).call(
                        block_identifier=boundary_block
                    )
                )
                if v > 0:
                    nonzero += 1
                    total_votes += v
            except Exception:
                continue
        results[candidate] = (nonzero, total_votes)

    # Sort by (highest nonzero_count, then highest total_votes)
    ranked = sorted(results.items(), key=lambda x: (x[1][0], x[1][1]), reverse=True)
    
    best_epoch, (best_nonzero, best_total) = ranked[0]
    
    median_nonzero = sorted([x[1][0] for x in ranked])[len(ranked) // 2] if ranked else 0
    
    # Log diagnostics
    console.print(f"[cyan]Vote-epoch auto-detection (sampled {len(pools)} pools over {len(candidates)} epoch candidates):[/cyan]")
    for i, (ep, (nz, tv)) in enumerate(ranked[:3]):
        ep_str = datetime.utcfromtimestamp(ep).isoformat() if ep > 0 else "N/A"
        console.print(f"  [{i+1}] epoch {ep} ({ep_str}): {nz}/{len(pools)} nonzero, {tv:,} total votes")
    
    if best_nonzero == 0:
        console.print(
            f"[bold yellow]⚠️  AUTODETECT FAILED:[/bold yellow] No candidate epoch had any nonzero votes. "
            f"Falling back to hint {epoch_hint}. Recommend manual --vote-epoch specification."
        )
        return epoch_hint
    
    if best_nonzero < max(1, len(pools) // 2):
        console.print(
            f"[bold yellow]⚠️  AUTODETECT WEAK:[/bold yellow] Best candidate {best_epoch} had only {best_nonzero}/{len(pools)} "
            f"nonzero pools. Results may be unreliable; consider --vote-epoch override."
        )
    
    return best_epoch


def refresh_active_status(
    conn: sqlite3.Connection,
    voter_contract,
    gauge_addresses: List[str],
    block_identifier: int,
) -> Tuple[int, int]:
    cur = conn.cursor()
    checked = 0
    updated = 0

    for gauge in track(gauge_addresses, description="Refreshing gauge isAlive status"):
        try:
            is_alive = int(bool(voter_contract.functions.isAlive(Web3.to_checksum_address(gauge)).call(block_identifier=block_identifier)))
            checked += 1
            cur.execute(
                """
                UPDATE gauges
                SET is_alive = ?
                WHERE lower(address) = lower(?)
                  AND COALESCE(is_alive, 1) != ?
                """,
                (is_alive, gauge, is_alive),
            )
            if cur.rowcount > 0:
                updated += cur.rowcount
        except Exception:
            continue

    conn.commit()
    return checked, updated


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


def expected_return(total_usd: float, base_votes: float, your_votes: float) -> float:
    if your_votes <= 0:
        return 0.0
    denom = base_votes + your_votes
    if denom <= 0:
        return 0.0
    return total_usd * (your_votes / denom)


def fetch_token_symbol(w3: Web3, token: str, symbol_cache: Dict[str, str]) -> str:
    token_l = token.lower()
    if token_l in symbol_cache:
        return symbol_cache[token_l]

    symbol = token[:6] + ".."
    try:
        erc20 = w3.eth.contract(address=Web3.to_checksum_address(token), abi=ERC20_SYMBOL_ABI)
        value = erc20.functions.symbol().call()
        if isinstance(value, str) and value.strip():
            symbol = value.strip()
    except Exception:
        try:
            erc20 = w3.eth.contract(address=Web3.to_checksum_address(token), abi=ERC20_SYMBOL_BYTES32_ABI)
            value = erc20.functions.symbol().call()
            if isinstance(value, bytes):
                decoded = value.decode("utf-8", errors="ignore").rstrip("\x00").strip()
                if decoded:
                    symbol = decoded
        except Exception:
            pass

    symbol_cache[token_l] = symbol
    return symbol


def resolve_pool_label(
    w3: Web3,
    pool_address: str,
    pool_label_cache: Dict[str, str],
    symbol_cache: Dict[str, str],
) -> str:
    pool_l = str(pool_address).lower()
    if pool_l in pool_label_cache:
        return pool_label_cache[pool_l]

    fallback = str(pool_address)
    if not (pool_l.startswith("0x") and len(pool_l) == 42):
        pool_label_cache[pool_l] = fallback
        return fallback

    try:
        pool = w3.eth.contract(address=Web3.to_checksum_address(pool_address), abi=POOL_ABI)
        token0 = pool.functions.token0().call()
        token1 = pool.functions.token1().call()
        sym0 = fetch_token_symbol(w3, token0, symbol_cache)
        sym1 = fetch_token_symbol(w3, token1, symbol_cache)
        pool_label_cache[pool_l] = f"{sym0}/{sym1}"
    except Exception:
        pool_label_cache[pool_l] = fallback

    return pool_label_cache[pool_l]


def solve_alloc_for_set(states: List[GaugeBoundaryState], total_votes: int, min_per_pool: int) -> List[float]:
    k = len(states)
    if k * min_per_pool > total_votes:
        raise ValueError("Infeasible: k * min_per_pool > voting power")

    floors = [float(min_per_pool)] * k
    if k == 0:
        return []

    remaining = float(total_votes - k * min_per_pool)
    if remaining <= 0:
        return floors

    B = [max(s.total_usd, 0.0) for s in states]
    V = [max(float(s.votes_raw), 0.0) for s in states]

    def alloc_for_lambda(lmbd: float) -> List[float]:
        out = []
        for i in range(k):
            if B[i] <= 0 or V[i] <= 0:
                out.append(floors[i])
                continue
            x = math.sqrt((B[i] * V[i]) / lmbd) - V[i]
            out.append(max(x, floors[i]))
        return out

    lo = 1e-18
    hi = 1.0
    for _ in range(120):
        if sum(alloc_for_lambda(hi)) <= total_votes:
            break
        hi *= 2.0

    for _ in range(160):
        mid = (lo + hi) / 2.0
        if sum(alloc_for_lambda(mid)) > total_votes:
            lo = mid
        else:
            hi = mid

    alloc = alloc_for_lambda(hi)
    s = sum(alloc)
    if s <= 0:
        return floors

    # Normalize minor numerical drift
    if abs(s - total_votes) > 1e-8:
        scale = total_votes / s
        alloc = [max(f, a * scale) for a, f in zip(alloc, floors)]

    return alloc


def main() -> None:
    parser = argparse.ArgumentParser(description="Boundary max return analysis")
    parser.add_argument("--epoch", type=int, default=1771372800)
    parser.add_argument("--db", default="data.db")
    parser.add_argument("--voting-power", type=int, default=1_183_272)
    parser.add_argument("--voter", default=os.getenv("VOTER_ADDRESS", "0xc69E3eF39E3fFBcE2A1c570f8d3ADF76909ef17b"))
    parser.add_argument("--rpc", default=os.getenv("RPC_URL") or os.getenv("BASE_RPC_URL", "https://mainnet.base.org"))
    parser.add_argument("--k", type=int, default=5)
    parser.add_argument("--min-votes-per-pool", type=int, default=50_000)
    parser.add_argument("--candidate-pools", type=int, default=30)
    parser.add_argument(
        "--max-gauges",
        type=int,
        default=0,
        help="If > 0, only analyze the first N gauges (sorted by address) for quick checks",
    )
    parser.add_argument("--baseline-return", type=float, default=954.53)
    parser.add_argument("--block-tolerance", type=int, default=60)
    parser.add_argument(
        "--vote-epoch",
        type=int,
        default=None,
        help="Epoch timestamp to use for weightsAt(pool, vote_epoch). If omitted, auto-detected from recent days.",
    )
    parser.add_argument(
        "--vote-epoch-offset-weeks",
        type=int,
        default=1,
        help="Fallback only if auto-detection is disabled. vote_epoch = epoch - offset*week (default 1)",
    )
    parser.add_argument(
        "--disable-vote-epoch-autodetect",
        action="store_true",
        help="Disable auto-detection and use --vote-epoch (or week offset fallback)",
    )
    parser.add_argument(
        "--vote-epoch-scan-days",
        type=int,
        default=14,
        help="How many days back to scan when auto-detecting vote epoch",
    )
    parser.add_argument(
        "--vote-epoch-sample-pools",
        type=int,
        default=24,
        help="How many pools to sample for vote-epoch auto-detection",
    )
    parser.add_argument(
        "--include-inactive",
        action="store_true",
        help="Include gauges marked inactive in DB (default: active-only)",
    )
    parser.add_argument(
        "--refresh-active-status",
        action="store_true",
        help="Refresh gauges.is_alive from VoterV5.isAlive(...) before analysis",
    )
    parser.add_argument(
        "--no-cache",
        action="store_true",
        help="Disable boundary cache reads/writes",
    )
    args = parser.parse_args()

    active_only = not args.include_inactive
    use_cache = not args.no_cache
    vote_epoch = int(args.vote_epoch) if args.vote_epoch is not None else -1

    # DB rows to define gauge/token universe for epoch
    conn = sqlite3.connect(args.db)
    cur = conn.cursor()
    ensure_boundary_cache_table(conn)

    # RPC + boundary block
    w3 = Web3(Web3.HTTPProvider(args.rpc))
    if not w3.is_connected():
        console.print("[red]Failed to connect to RPC[/red]")
        return

    boundary_block = find_block_at_timestamp(w3, args.epoch, args.block_tolerance)
    boundary_ts = w3.eth.get_block(boundary_block)["timestamp"]
    console.print(f"[cyan]Boundary block:[/cyan] {boundary_block} @ {datetime.utcfromtimestamp(boundary_ts).isoformat()} UTC")

    voter = w3.eth.contract(address=Web3.to_checksum_address(args.voter), abi=VOTER_ABI)

    if args.refresh_active_status:
        cur.execute("SELECT DISTINCT lower(gauge_address) FROM bribes WHERE epoch = ?", (args.epoch,))
        refresh_targets = [r[0] for r in cur.fetchall() if r and r[0]]
        checked, updated = refresh_active_status(conn, voter, refresh_targets, boundary_block)
        console.print(
            f"[cyan]isAlive refresh:[/cyan] checked={checked}, updated={updated}, epoch_gauges={len(refresh_targets)}"
        )

    active_clause = "AND COALESCE(g.is_alive, 1) = 1" if active_only else ""

    cur.execute(
        f"""
        SELECT
            b.gauge_address,
            COALESCE(g.pool, b.gauge_address) AS pool,
            b.bribe_contract,
            b.reward_token,
            MAX(COALESCE(b.token_decimals, 18)) AS token_decimals,
            MAX(COALESCE(b.usd_price, 0)) AS usd_price
        FROM bribes b
        LEFT JOIN gauges g ON lower(g.address) = lower(b.gauge_address)
        WHERE b.epoch = ?
        {active_clause}
        GROUP BY b.gauge_address, COALESCE(g.pool, b.gauge_address), b.bribe_contract, b.reward_token
        """,
        (args.epoch,),
    )
    rows = cur.fetchall()

    if args.max_gauges and args.max_gauges > 0:
        cur.execute(
            f"""
            SELECT b.gauge_address
            FROM bribes b
            LEFT JOIN gauges g ON lower(g.address) = lower(b.gauge_address)
            WHERE b.epoch = ?
            {active_clause}
            GROUP BY b.gauge_address
            ORDER BY SUM(COALESCE(b.usd_value, 0)) DESC, lower(b.gauge_address) ASC
            LIMIT ?
            """,
            (args.epoch, args.max_gauges),
        )
        selected = [str(r[0]).lower() for r in cur.fetchall() if r and r[0]]
        if not selected:
            selected = sorted({str(r[0]).lower() for r in rows})[: args.max_gauges]
        selected_set = set(selected)
        rows = [r for r in rows if str(r[0]).lower() in selected_set]

    if not rows:
        console.print("[red]No bribe rows found for epoch in DB[/red]")
        return

    gauge_set = sorted({r[0].lower() for r in rows})
    pool_by_gauge = {r[0].lower(): r[1] for r in rows}
    pool_set = sorted({str(r[1]).lower() for r in rows if r[1]})

    if vote_epoch < 0:
        if not args.disable_vote_epoch_autodetect:
            vote_epoch = autodetect_vote_epoch(
                voter,
                pool_set,
                boundary_block,
                args.epoch,
                args.vote_epoch_scan_days,
                args.vote_epoch_sample_pools,
            )
        else:
            vote_epoch = int(args.epoch - (max(args.vote_epoch_offset_weeks, 0) * WEEK_SECONDS))

    console.print(
        Panel.fit(
            "[bold cyan]Boundary Theoretical Maximum Return[/bold cyan]\n"
            f"Epoch: {args.epoch} ({datetime.utcfromtimestamp(args.epoch).isoformat()} UTC)\n"
            f"Vote epoch for weightsAt: {vote_epoch} ({datetime.utcfromtimestamp(vote_epoch).isoformat()} UTC)\n"
            f"Gauge universe from DB: {len(gauge_set)} gauges | Token rows: {len(rows)}\n"
            f"Filter: {'active-only' if active_only else 'active+inactive'} | Cache: {'on' if use_cache else 'off'}",
            border_style="cyan",
        )
    )

    vote_failures = 0
    reward_failures = 0
    states: List[GaugeBoundaryState] = []

    if use_cache:
        states = load_cached_states(cur, args.epoch, vote_epoch, boundary_block, active_only)
        if states:
            console.print(
                f"[green]Loaded {len(states)} cached boundary states from DB (block {boundary_block}, vote_epoch {vote_epoch})[/green]"
            )
        else:
            fallback_block = find_best_cached_block_for_epoch(cur, args.epoch, vote_epoch)
            if fallback_block > 0 and fallback_block != boundary_block:
                fallback_states = load_cached_states(cur, args.epoch, vote_epoch, fallback_block, active_only)
                if fallback_states:
                    states = fallback_states
                    console.print(
                        f"[yellow]Using cached boundary states from block {fallback_block} "
                        f"(current boundary block {boundary_block} had no cache rows, vote_epoch {vote_epoch})[/yellow]"
                    )

    if not states:
        # Query boundary votes per pool via weightsAt(pool, vote_epoch)
        votes_by_pool_raw: Dict[str, int] = {}
        for pool in track(pool_set, description="Querying pool weightsAt at boundary"):
            try:
                v = voter.functions.weightsAt(Web3.to_checksum_address(pool), vote_epoch).call(block_identifier=boundary_block)
                votes_by_pool_raw[pool] = int(v)
            except Exception:
                vote_failures += 1
                votes_by_pool_raw[pool] = 0

        votes_by_pool: Dict[str, float] = {
            pool: (votes_wei / ONE_E18) for pool, votes_wei in votes_by_pool_raw.items()
        }

        nonzero_pool_votes = sum(1 for v in votes_by_pool_raw.values() if v > 0)
        max_pool_votes = max(votes_by_pool.values()) if votes_by_pool else 0
        console.print(
            f"[cyan]Boundary vote query stats:[/cyan] nonzero_pools={nonzero_pool_votes}/{len(votes_by_pool)}, "
            f"max_pool_votes={max_pool_votes:,}, vote_failures={vote_failures}"
        )

        # ═══ GUARDRAIL: Vote Epoch Alignment Check ═══
        # Per canonical reference: if weightsAt returns 0 for all/nearly all pools, vote_epoch is likely misaligned
        if nonzero_pool_votes == 0:
            console.print(
                f"[bold red]⚠️  CRITICAL GUARDRAIL:[/bold red] All {len(pool_set)} pools returned weightsAt(pool, "
                f"{vote_epoch}) = 0. This suggests vote_epoch {vote_epoch} is misaligned with the closed epoch {args.epoch}."
            )
            console.print(
                f"[red]Recommendations:[/red]\n"
                f"  1) Verify vote_epoch is the CLOSED epoch timestamp (not E+WEEK)\n"
                f"  2) Use --vote-epoch to explicitly set a different timestamp\n"
                f"  3) Check if epoch {args.epoch} has actually closed (is past update_period() call)\n"
                f"  4) Examine contract events for actual flip block (Mint event with week_number)"
            )
            return

        if nonzero_pool_votes < max(3, len(pool_set) // 3):
            console.print(
                f"[bold yellow]⚠️  SPARSE WEIGHTS WARNING:[/bold yellow] Only {nonzero_pool_votes}/{len(pool_set)} pools have nonzero votes. "
                f"This could indicate:\n"
                f"  • Vote epoch ({vote_epoch}) is slightly off (try adjacent days)\n"
                f"  • Most gauges were not voted in the queried epoch\n"
                f"  Proceeding with caution; results may underestimate pool returns."
            )

        # Query boundary rewards per (bribe_contract, token)
        # CANONICAL: Query rewardData at the SAME vote_epoch used for weightsAt
        reward_cache: Dict[Tuple[str, str, int], int] = {}
        gauge_total_usd: Dict[str, float] = defaultdict(float)
        reward_query_epoch = vote_epoch  # ← Use vote_epoch, not calc_epoch

        for gauge, pool, bribe_contract, reward_token, token_decimals, usd_price in track(
            rows, description="Querying rewardData at boundary"
        ):
            gauge_l = gauge.lower()
            bribe_l = bribe_contract.lower()
            token_l = reward_token.lower()

            try:
                bribe_c = w3.eth.contract(address=Web3.to_checksum_address(bribe_contract), abi=BRIBE_ABI)
                ckey = (bribe_l, token_l, reward_query_epoch)

                if ckey not in reward_cache:
                    rd = bribe_c.functions.rewardData(Web3.to_checksum_address(reward_token), reward_query_epoch).call(
                        block_identifier=boundary_block
                    )
                    reward_cache[ckey] = int(rd[1])

                rewards_raw = reward_cache[ckey]
                decimals = int(token_decimals or 18)
                price = float(usd_price or 0)
                amount = rewards_raw / (10 ** decimals)
                gauge_total_usd[gauge_l] += amount * price

            except Exception as e:
                reward_failures += 1

        # Build states
        for gauge in gauge_set:
            total_usd = gauge_total_usd.get(gauge, 0.0)
            if total_usd <= 0:
                continue
            pool_addr = str(pool_by_gauge.get(gauge, gauge)).lower()
            states.append(
                GaugeBoundaryState(
                    gauge=gauge,
                    pool=pool_by_gauge.get(gauge, gauge),
                    votes_raw=votes_by_pool.get(pool_addr, 0),
                    total_usd=total_usd,
                )
            )

        # ═══ GUARDRAIL: Rewards Consistency Check ═══
        total_usd_all_gauges = sum(gauge_total_usd.values())
        if total_usd_all_gauges == 0 and vote_failures == 0 and reward_failures < len(rows) / 2:
            console.print(
                f"[bold yellow]⚠️  EMPTY REWARDS WARNING:[/bold yellow] All gauges have zero USD rewards despite "
                f"vote queries working. This suggests:\n"
                f"  • rewardData query epoch ({reward_query_epoch}) has no bribes/rewards registered\n"
                f"  • Bribes were deposited at a DIFFERENT epoch timestamp\n"
                f"  • rewardData was queried BEFORE bribes were deposited\n"
                f"  Canonical fix: Ensure bribes deposited DURING epoch {args.epoch} are visible at epoch {reward_query_epoch}"
            )
            console.print(f"[cyan]Reward query details:[/cyan] reward_query_epoch={reward_query_epoch}, "
                          f"reward_failures={reward_failures}/{len(rows)}")
        elif nonzero_pool_votes > 0 and total_usd_all_gauges > 0:
            console.print(
                f"[green]✓ Rewards consistency OK:[/green] {len(states)} gauges with USD rewards, "
                f"query_epoch={reward_query_epoch}, total_usd=${total_usd_all_gauges:,.2f}"
            )

        if use_cache and states:
            save_states_to_cache(conn, states, args.epoch, vote_epoch, boundary_block)
            console.print(f"[green]Cached {len(states)} boundary states to DB (vote_epoch {vote_epoch})[/green]")

    if not states:
        console.print("[red]No positive-USD gauge states computed at boundary[/red]")
        return

    # 1-pool max
    best_state = max(states, key=lambda s: expected_return(s.total_usd, s.votes_raw, args.voting_power))
    one_pool_return = expected_return(best_state.total_usd, best_state.votes_raw, args.voting_power)

    # K-pool max over top candidate pools by 1-pool score
    ranked = sorted(states, key=lambda s: expected_return(s.total_usd, s.votes_raw, args.voting_power), reverse=True)
    candidates = ranked[: max(args.k, min(args.candidate_pools, len(ranked)))]

    effective_k = min(args.k, len(candidates))
    if effective_k <= 0:
        console.print("[red]No candidates available for constrained allocation[/red]")
        return

    best_combo = None
    best_alloc = None
    best_k_return = -1.0
    combos = 0

    for combo in combinations(candidates, effective_k):
        combos += 1
        alloc = solve_alloc_for_set(list(combo), args.voting_power, args.min_votes_per_pool)
        ret = sum(expected_return(s.total_usd, s.votes_raw, x) for s, x in zip(combo, alloc))
        if ret > best_k_return:
            best_k_return = ret
            best_combo = list(combo)
            best_alloc = alloc

    baseline = args.baseline_return
    one_vs = ((one_pool_return / baseline) - 1) * 100 if baseline > 0 else 0.0
    k_vs = ((best_k_return / baseline) - 1) * 100 if baseline > 0 else 0.0

    summary = Table(show_header=True, header_style="bold cyan")
    summary.add_column("Scenario", width=25)
    summary.add_column("Expected Return", justify="right", width=18)
    summary.add_column("vs Baseline", justify="right", width=14)
    summary.add_column("Notes", width=52)

    summary.add_row(
        "1-pool maximum",
        f"${one_pool_return:,.2f}",
        f"{one_vs:+.2f}%",
        f"All {args.voting_power:,} votes to one pool (boundary data)",
    )
    summary.add_row(
        f"{effective_k}-pool maximum",
        f"${best_k_return:,.2f}",
        f"{k_vs:+.2f}%",
        f"x_i >= {args.min_votes_per_pool:,}, searched {combos:,} combos over top {len(candidates)}",
    )

    console.print()
    console.print(summary)

    symbol_cache: Dict[str, str] = {}
    pool_label_cache: Dict[str, str] = {}
    best_pool_label = resolve_pool_label(w3, str(best_state.pool), pool_label_cache, symbol_cache)

    one_tbl = Table(show_header=True, header_style="bold yellow")
    one_tbl.add_column("Best Pool", width=44)
    one_tbl.add_column("Gauge", width=16)
    one_tbl.add_column("Boundary Votes", justify="right", width=16)
    one_tbl.add_column("Boundary USD", justify="right", width=14)
    one_tbl.add_column("Expected", justify="right", width=14)
    one_tbl.add_row(
        best_pool_label,
        best_state.gauge[:14] + "..",
        f"{best_state.votes_raw:,.0f}",
        f"${best_state.total_usd:,.2f}",
        f"${one_pool_return:,.2f}",
    )

    console.print()
    console.print("[bold yellow]Best 1-pool allocation (boundary)[/bold yellow]")
    console.print(one_tbl)

    k_tbl = Table(show_header=True, header_style="bold green")
    k_tbl.add_column("Pool", width=44)
    k_tbl.add_column("Gauge", width=16)
    k_tbl.add_column("Alloc Votes", justify="right", width=14)
    k_tbl.add_column("Boundary Votes", justify="right", width=16)
    k_tbl.add_column("Boundary USD", justify="right", width=14)
    k_tbl.add_column("Expected", justify="right", width=12)

    for s, x in zip(best_combo, best_alloc):
        ret = expected_return(s.total_usd, s.votes_raw, x)
        pool_label = resolve_pool_label(w3, str(s.pool), pool_label_cache, symbol_cache)
        k_tbl.add_row(
            pool_label,
            s.gauge[:14] + "..",
            f"{x:,.0f}",
            f"{s.votes_raw:,.0f}",
            f"${s.total_usd:,.2f}",
            f"${ret:,.2f}",
        )

    console.print()
    console.print(f"[bold green]Best {effective_k}-pool allocation (boundary)[/bold green]")
    console.print(k_tbl)

    console.print(
        f"\n[cyan]Diagnostics:[/cyan] gauges_with_usd={len(states)}, vote_failures={vote_failures}, reward_failures={reward_failures}"
    )
    console.print(
        "[dim]Boundary method uses on-chain rewardData+weights at epoch close; "
        "USD conversion uses token prices stored in DB for that epoch.[/dim]"
    )

    conn.close()


if __name__ == "__main__":
    main()
