#!/usr/bin/env python3
"""
Boundary-based theoretical max return analysis using modular approach.

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

Uses modular approach: DataAccess layer for data fetching, ContractRewardCalculator for reward logic.
"""

import argparse
import math
import os
import sqlite3
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from itertools import combinations
from pathlib import Path
from typing import Dict, List, Optional, Tuple
import sys

from dotenv import load_dotenv
from rich.console import Console
from rich.panel import Panel
from rich.progress import track
from rich.table import Table
from web3 import Web3

# Setup paths for imports
ROOT_DIR = Path(__file__).resolve().parents[0]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

load_dotenv()
console = Console()

# Import modular components
from config.settings import (
    VOTER_ADDRESS, ONE_E18, WEEK, LEGACY_POOL_SHARES, KNOWN_POOLS, DATABASE_PATH
)

WEEK_SECONDS = 7 * 24 * 60 * 60

# ABIs
VOTER_ABI = [
    {"inputs": [], "name": "ve", "outputs": [{"internalType": "address", "name": "", "type": "address"}], "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "_ve", "outputs": [{"internalType": "address", "name": "", "type": "address"}], "stateMutability": "view", "type": "function"},
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

ERC20_ABI = [
    {"constant": True, "inputs": [{"name": "_owner", "type": "address"}], "name": "balanceOf", "outputs": [{"name": "balance", "type": "uint256"}], "type": "function"},
    {"constant": True, "inputs": [], "name": "decimals", "outputs": [{"name": "", "type": "uint8"}], "type": "function"},
    {
        "inputs": [],
        "name": "symbol",
        "outputs": [{"internalType": "string", "name": "", "type": "string"}],
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


def _format_eta(seconds: float) -> str:
    """Format seconds into human-readable ETA string."""
    if seconds == float("inf") or seconds < 0:
        return "∞"
    if seconds < 60:
        return f"{seconds:.0f}s"
    elif seconds < 3600:
        return f"{seconds/60:.1f}m"
    else:
        return f"{seconds/3600:.1f}h"


def ensure_boundary_cache_table(conn: sqlite3.Connection) -> None:
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
    cur.execute("PRAGMA table_info(boundary_gauge_values)")
    existing = {row[1] for row in cur.fetchall()}
    if "vote_epoch" not in existing:
        cur.execute("ALTER TABLE boundary_gauge_values ADD COLUMN vote_epoch INTEGER")
        cur.execute("UPDATE boundary_gauge_values SET vote_epoch = epoch WHERE vote_epoch IS NULL")
    if "active_only" not in existing:
        cur.execute("ALTER TABLE boundary_gauge_values ADD COLUMN active_only INTEGER")
        cur.execute("UPDATE boundary_gauge_values SET active_only = 1 WHERE active_only IS NULL")
    if "boundary_block" not in existing:
        cur.execute("ALTER TABLE boundary_gauge_values ADD COLUMN boundary_block INTEGER")
        cur.execute("UPDATE boundary_gauge_values SET boundary_block = -1 WHERE boundary_block IS NULL")
    if "votes_raw" not in existing:
        cur.execute("ALTER TABLE boundary_gauge_values ADD COLUMN votes_raw REAL")
        cur.execute("UPDATE boundary_gauge_values SET votes_raw = 0 WHERE votes_raw IS NULL")
    if "total_usd" not in existing:
        cur.execute("ALTER TABLE boundary_gauge_values ADD COLUMN total_usd REAL")
        cur.execute("UPDATE boundary_gauge_values SET total_usd = 0 WHERE total_usd IS NULL")
    if "computed_at" not in existing:
        cur.execute("ALTER TABLE boundary_gauge_values ADD COLUMN computed_at INTEGER")
        cur.execute("UPDATE boundary_gauge_values SET computed_at = 0 WHERE computed_at IS NULL")

    cur.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_boundary_gauge_values_lookup
        ON boundary_gauge_values(epoch, vote_epoch, active_only, total_usd DESC)
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


def load_cached_states(
    cur: sqlite3.Cursor,
    epoch: int,
    vote_epoch: int,
    active_only: bool,
    max_gauges: int,
) -> Tuple[List[GaugeBoundaryState], int]:
    limit_clause = ""
    params: List[object] = [epoch, vote_epoch, 1 if active_only else 0]
    if max_gauges and max_gauges > 0:
        limit_clause = "LIMIT ?"
        params.append(max_gauges)

    cur.execute(
        f"""
        SELECT gauge_address, pool_address, votes_raw, total_usd, boundary_block
        FROM boundary_gauge_values
        WHERE epoch = ?
          AND vote_epoch = ?
          AND active_only = ?
          AND total_usd > 0
        ORDER BY total_usd DESC, lower(gauge_address) ASC
        {limit_clause}
        """,
        tuple(params),
    )
    rows = cur.fetchall()
    if not rows:
        return [], -1

    states = [
        GaugeBoundaryState(
            gauge=str(g).lower(),
            pool=str(p).lower(),
            votes_raw=float(v or 0),
            total_usd=float(u or 0),
        )
        for g, p, v, u, _b in rows
    ]
    boundary_block = int(rows[0][4] or -1)
    return states, boundary_block


def save_states_to_cache(
    conn: sqlite3.Connection,
    epoch: int,
    vote_epoch: int,
    active_only: bool,
    boundary_block: int,
    states: List[GaugeBoundaryState],
) -> None:
    if not states:
        return

    cur = conn.cursor()
    now_ts = int(datetime.utcnow().timestamp())
    cur.executemany(
        """
        INSERT OR REPLACE INTO boundary_gauge_values (
            epoch, vote_epoch, active_only, boundary_block,
            gauge_address, pool_address, votes_raw, total_usd, computed_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                epoch,
                vote_epoch,
                1 if active_only else 0,
                int(boundary_block),
                s.gauge.lower(),
                str(s.pool).lower(),
                float(s.votes_raw),
                float(s.total_usd),
                now_ts,
            )
            for s in states
        ],
    )
    conn.commit()


def find_block_at_timestamp(w3: Web3, target_timestamp: int, tolerance: int = 60) -> int:
    """Binary search to find block at target timestamp."""
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


def autodetect_vote_epoch(
    voter_contract,
    pool_set: List[str],
    boundary_block: int,
    epoch_hint: int,
    scan_days: int,
    sample_pools: int,
    min_nonzero_ratio: float,
    min_nonzero_count: int,
    strict_mode: bool,
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
    
    if not ranked:
        return epoch_hint
    
    best_epoch, (best_nonzero, best_total) = ranked[0]
    
    # Log diagnostics
    console.print(f"[cyan]Vote-epoch auto-detection (sampled {len(pools)} pools over {len(candidates)} epoch candidates):[/cyan]")
    for i, (ep, (nz, tv)) in enumerate(ranked[:3]):
        ep_str = datetime.utcfromtimestamp(ep).isoformat() if ep > 0 else "N/A"
        console.print(f"  [{i+1}] epoch {ep} ({ep_str}): {nz}/{len(pools)} nonzero, {tv:,} total votes")
    
    if best_nonzero == 0:
        message = (
            f"[bold red]AUTODETECT FAILED:[/bold red] No candidate epoch had any nonzero sampled pool votes. "
            f"Set --vote-epoch explicitly for deterministic analysis."
        )
        if strict_mode:
            raise ValueError(message)
        console.print(f"[bold yellow]⚠️  {message}[/bold yellow]")
        return epoch_hint

    required_nonzero = max(1, int(math.ceil(len(pools) * max(0.0, min_nonzero_ratio))), int(max(0, min_nonzero_count)))
    if best_nonzero < required_nonzero:
        message = (
            f"[bold red]AUTODETECT WEAK:[/bold red] Best candidate {best_epoch} had {best_nonzero}/{len(pools)} nonzero sampled pools, "
            f"below required threshold {required_nonzero}/{len(pools)}. "
            f"Set --vote-epoch explicitly or relax thresholds."
        )
        if strict_mode:
            raise ValueError(message)
        console.print(f"[bold yellow]⚠️  {message}[/bold yellow]")
        return epoch_hint
    
    return best_epoch


def refresh_active_status(
    conn: sqlite3.Connection,
    voter_contract,
    gauge_addresses: List[str],
    block_identifier: int,
) -> Tuple[int, int]:
    """Refresh gauge active status from chain."""
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


def fetch_token_symbol(w3: Web3, token: str, symbol_cache: Dict[str, str]) -> str:
    """Fetch token symbol from chain."""
    token_l = token.lower()
    if token_l in symbol_cache:
        return symbol_cache[token_l]

    symbol = token[:6] + ".."
    try:
        erc20 = w3.eth.contract(address=Web3.to_checksum_address(token), abi=ERC20_ABI)
        value = erc20.functions.symbol().call()
        if isinstance(value, str) and value.strip():
            symbol = value.strip()
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
    """Resolve pool label from token0/token1 symbols."""
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


def expected_return(total_usd: float, base_votes: float, your_votes: float) -> float:
    """Calculate expected return (pool reward * user share)."""
    if your_votes <= 0:
        return 0.0
    denom = base_votes + your_votes
    if denom <= 0:
        return 0.0
    return total_usd * (your_votes / denom)


def parse_epoch_list(conn: sqlite3.Connection, args) -> List[int]:
    cur = conn.cursor()
    if getattr(args, "all_epochs", False):
        return [int(r[0]) for r in cur.execute("SELECT epoch FROM epoch_boundaries ORDER BY epoch")]
    if getattr(args, "epochs", None):
        return [int(e.strip()) for e in str(args.epochs).split(",") if e.strip()]
    return [int(args.epoch)]


def load_token_prices_asof(conn: sqlite3.Connection, cutoff_ts: int) -> Dict[str, float]:
    cur = conn.cursor()
    price_map: Dict[str, float] = {}

    try:
        rows = cur.execute(
            """
            WITH latest AS (
                SELECT lower(token_address) AS token_address, MAX(timestamp) AS ts
                FROM historical_token_prices
                WHERE timestamp <= ? AND COALESCE(usd_price, 0) > 0
                GROUP BY lower(token_address)
            )
            SELECT lower(h.token_address), h.usd_price
            FROM historical_token_prices h
            JOIN latest l
              ON lower(h.token_address) = l.token_address
             AND h.timestamp = l.ts
            WHERE COALESCE(h.usd_price, 0) > 0
            """,
            (int(cutoff_ts),),
        ).fetchall()
        for token, usd in rows:
            price_map[str(token).lower()] = float(usd)
    except sqlite3.OperationalError:
        pass

    try:
        rows = cur.execute(
            """
            SELECT lower(token_address), usd_price
            FROM token_prices
            WHERE COALESCE(usd_price, 0) > 0
            """
        ).fetchall()
        for token, usd in rows:
            price_map.setdefault(str(token).lower(), float(usd))
    except sqlite3.OperationalError:
        pass

    return price_map


def load_states_from_boundary_tables(
    conn: sqlite3.Connection,
    epoch: int,
    active_only: bool,
    max_gauges: int,
) -> Tuple[List[GaugeBoundaryState], int, int, int, int]:
    cur = conn.cursor()
    boundary_block, vote_epoch = load_epoch_boundary(conn, epoch)
    if boundary_block <= 0 or vote_epoch <= 0:
        return [], -1, -1, 0, 0

    vote_rows = cur.execute(
        """
        SELECT lower(gauge_address), lower(pool_address), CAST(votes_raw AS REAL)
        FROM boundary_gauge_values
        WHERE epoch = ? AND vote_epoch = ? AND active_only = ?
        """,
        (int(epoch), int(vote_epoch), 1 if active_only else 0),
    ).fetchall()

    votes_by_gauge: Dict[str, float] = {}
    pool_by_gauge: Dict[str, str] = {}
    for gauge, pool, votes in vote_rows:
        gauge_l = str(gauge).lower()
        votes_by_gauge[gauge_l] = float(votes or 0.0)
        pool_by_gauge[gauge_l] = str(pool).lower()

    reward_rows = cur.execute(
        """
        SELECT
            lower(brs.gauge_address),
            lower(brs.reward_token),
            CAST(brs.rewards_raw AS REAL),
            COALESCE(brs.token_decimals, tm.decimals, 18) AS decimals,
            COALESCE(brs.usd_price, 0.0) AS usd_price
        FROM boundary_reward_snapshots brs
        LEFT JOIN token_metadata tm ON lower(tm.token_address) = lower(brs.reward_token)
        WHERE brs.epoch = ?
          AND brs.vote_epoch = ?
          AND brs.active_only = ?
          AND CAST(brs.rewards_raw AS REAL) > 0
        """,
        (int(epoch), int(vote_epoch), 1 if active_only else 0),
    ).fetchall()

    price_map = load_token_prices_asof(conn, int(vote_epoch))
    gauge_total_usd: Dict[str, float] = defaultdict(float)
    priced_rows = 0
    unpriced_rows = 0

    for gauge, token, rewards_raw, decimals, usd_price in reward_rows:
        token_l = str(token).lower()
        price = float(usd_price or 0.0)
        if price <= 0:
            price = float(price_map.get(token_l, 0.0))
        if price <= 0:
            unpriced_rows += 1
            continue

        amount = float(rewards_raw or 0.0) / (10 ** int(decimals or 18))
        gauge_total_usd[str(gauge).lower()] += amount * price
        priced_rows += 1

    states: List[GaugeBoundaryState] = []
    for gauge, total_usd in gauge_total_usd.items():
        if total_usd <= 0:
            continue
        states.append(
            GaugeBoundaryState(
                gauge=gauge,
                pool=pool_by_gauge.get(gauge, gauge),
                votes_raw=float(votes_by_gauge.get(gauge, 0.0)),
                total_usd=float(total_usd),
            )
        )

    states.sort(key=lambda s: (s.total_usd, s.gauge), reverse=True)
    if max_gauges and max_gauges > 0:
        states = states[: int(max_gauges)]

    return states, boundary_block, vote_epoch, priced_rows, unpriced_rows


def solve_epoch_maximum(
    states: List[GaugeBoundaryState],
    voting_power: int,
    k: int,
    min_votes_per_pool: int,
    candidate_pools: int,
) -> Tuple[float, float, GaugeBoundaryState, List[GaugeBoundaryState], List[float], int, int]:
    best_state = max(states, key=lambda s: expected_return(s.total_usd, s.votes_raw, voting_power))
    one_pool_return = expected_return(best_state.total_usd, best_state.votes_raw, voting_power)

    ranked = sorted(states, key=lambda s: expected_return(s.total_usd, s.votes_raw, voting_power), reverse=True)
    candidates = ranked[: max(k, min(candidate_pools, len(ranked)))]
    effective_k = min(k, len(candidates))

    if effective_k <= 0:
        return one_pool_return, one_pool_return, best_state, [best_state], [float(voting_power)], 1, 1

    best_combo = [best_state]
    best_alloc = [float(voting_power)]
    best_k_return = -1.0
    combos = 0
    total_combos = math.comb(len(candidates), effective_k)

    for combo in combinations(candidates, effective_k):
        combos += 1
        alloc = solve_alloc_for_set(list(combo), voting_power, min_votes_per_pool)
        ret = sum(expected_return(s.total_usd, s.votes_raw, x) for s, x in zip(combo, alloc))
        if ret > best_k_return:
            best_k_return = ret
            best_combo = list(combo)
            best_alloc = alloc

    return one_pool_return, best_k_return, best_state, best_combo, best_alloc, combos, total_combos


def run_offline_multi_epoch_analysis(conn: sqlite3.Connection, args) -> None:
    active_only = not args.include_inactive
    epochs = parse_epoch_list(conn, args)
    if not epochs:
        console.print("[red]No epochs to analyze[/red]")
        return

    console.print(
        Panel.fit(
            "[bold cyan]Boundary Maximum Return (Offline Boundary Tables)[/bold cyan]\n"
            f"Epochs: {len(epochs)} | Voting power: {args.voting_power:,} | "
            f"K={args.k} | Min votes/pool={args.min_votes_per_pool:,}"
        )
    )

    summary = Table(show_header=True, header_style="bold cyan")
    summary.add_column("Epoch", width=12)
    summary.add_column("Vote Epoch", width=12)
    summary.add_column("Gauges", justify="right", width=8)
    summary.add_column("1-Pool Max", justify="right", width=14)
    summary.add_column(f"{args.k}-Pool Max", justify="right", width=14)
    summary.add_column("Top Gauge", width=16)
    summary.add_column("Price Rows", justify="right", width=10)

    processed = 0
    for epoch in epochs:
        states, boundary_block, vote_epoch, priced_rows, unpriced_rows = load_states_from_boundary_tables(
            conn, int(epoch), active_only, int(args.max_gauges)
        )
        if not states:
            summary.add_row(str(epoch), "-", "0", "-", "-", "-", "0")
            continue

        one_pool_return, k_pool_return, best_state, _combo, _alloc, _combos, _total = solve_epoch_maximum(
            states,
            int(args.voting_power),
            int(args.k),
            int(args.min_votes_per_pool),
            int(args.candidate_pools),
        )

        summary.add_row(
            str(epoch),
            str(vote_epoch),
            str(len(states)),
            f"${one_pool_return:,.2f}",
            f"${k_pool_return:,.2f}",
            best_state.gauge[:14] + "..",
            f"{priced_rows}/{priced_rows+unpriced_rows}",
        )
        processed += 1

    console.print()
    console.print(summary)
    console.print(f"\n[green]Processed epochs with usable states:[/green] {processed}/{len(epochs)}")


def solve_alloc_for_set(states: List[GaugeBoundaryState], total_votes: int, min_per_pool: int) -> List[float]:
    """Solve optimal allocation for K pools using Lagrange multiplier method."""
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

    # Normalize numerical drift
    if abs(s - total_votes) > 1e-8:
        scale = total_votes / s
        alloc = [max(f, a * scale) for a, f in zip(alloc, floors)]

    return alloc


def main() -> None:
    parser = argparse.ArgumentParser(description="Boundary max return analysis (modular approach)")
    parser.add_argument("--epoch", type=int, default=1771459200)
    parser.add_argument("--all-epochs", action="store_true", help="Analyze all epochs from epoch_boundaries")
    parser.add_argument("--epochs", type=str, help="Comma-separated list of epochs to analyze")
    parser.add_argument("--db", default=DATABASE_PATH)
    parser.add_argument("--voting-power", type=int, default=1_183_272)
    parser.add_argument("--voter", default=os.getenv("VOTER_ADDRESS", VOTER_ADDRESS))
    parser.add_argument("--rpc", default=os.getenv("RPC_URL") or "https://mainnet.base.org")
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
        "--vote-epoch-min-nonzero-ratio",
        type=float,
        default=0.50,
        help="Minimum sampled-pool nonzero ratio required for auto-detect acceptance (default: 0.50)",
    )
    parser.add_argument(
        "--vote-epoch-min-nonzero-count",
        type=int,
        default=8,
        help="Minimum sampled-pool nonzero count required for auto-detect acceptance (default: 8)",
    )
    parser.add_argument(
        "--allow-weak-vote-epoch-autodetect",
        action="store_true",
        help="Allow weak auto-detect candidates; otherwise weak detection fails fast and requires --vote-epoch",
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
        "--offline-only",
        action="store_true",
        help="Run analysis from DB cache only (no RPC calls). Requires --vote-epoch and cached rows.",
    )
    parser.add_argument(
        "--refresh-cache",
        action="store_true",
        help="Ignore cached boundary states and rebuild them from chain (online mode only).",
    )
    parser.add_argument(
        "--progress-every",
        type=int,
        default=100,
        help="Emit heartbeat logs every N items in long loops (default: 100)",
    )
    args = parser.parse_args()

    active_only = not args.include_inactive
    vote_epoch = int(args.vote_epoch) if args.vote_epoch is not None else -1

    # Load database (use raw sqlite3 for direct queries)
    conn = sqlite3.connect(args.db)
    cur = conn.cursor()
    ensure_boundary_cache_table(conn)

    run_offline_multi_epoch_analysis(conn, args)
    conn.close()
    return

    offline_only = bool(args.offline_only)
    w3: Optional[Web3] = None
    voter = None
    boundary_block = -1
    boundary_vote_epoch = -1

    if not offline_only:
        rpc_url = args.rpc
        if not rpc_url:
            console.print("[red]RPC_URL not configured[/red]")
            return

        w3 = Web3(Web3.HTTPProvider(rpc_url))
        if not w3.is_connected():
            console.print("[red]Failed to connect to RPC[/red]")
            return

        console.print("[green]Connected to blockchain[/green]")

        boundary_block, boundary_vote_epoch = load_epoch_boundary(conn, args.epoch)
        if boundary_block <= 0:
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

    # Get bribes from DB using DataAccess
    active_clause = "AND COALESCE(g.is_alive, 1) = 1" if active_only else ""
    
    console.print("[cyan]Phase 1/6: Loading bribe universe from DB[/cyan]")
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
    console.print(f"[cyan]Loaded {len(rows)} bribe/token rows for epoch {args.epoch}[/cyan]")

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

    # Auto-detect or use specified vote_epoch
    if vote_epoch < 0 and boundary_vote_epoch > 0:
        vote_epoch = int(boundary_vote_epoch)

    if vote_epoch < 0:
        if offline_only:
            console.print("[red]Offline mode requires --vote-epoch so cache rows are deterministic.[/red]")
            return
        if not args.disable_vote_epoch_autodetect:
            try:
                vote_epoch = autodetect_vote_epoch(
                    voter,
                    pool_set,
                    boundary_block,
                    args.epoch,
                    args.vote_epoch_scan_days,
                    args.vote_epoch_sample_pools,
                    args.vote_epoch_min_nonzero_ratio,
                    args.vote_epoch_min_nonzero_count,
                    not args.allow_weak_vote_epoch_autodetect,
                )
            except ValueError as err:
                console.print(f"[red]{err}[/red]")
                console.print(
                    "[yellow]Use --vote-epoch <timestamp> for this run, or pass "
                    "--allow-weak-vote-epoch-autodetect to override.[/yellow]"
                )
                return
        else:
            vote_epoch = int(args.epoch - (max(args.vote_epoch_offset_weeks, 0) * WEEK_SECONDS))

    console.print(
        Panel.fit(
            "[bold cyan]Boundary Theoretical Maximum Return[/bold cyan]\n"
            f"Epoch: {args.epoch} ({datetime.utcfromtimestamp(args.epoch).isoformat()} UTC)\n"
            f"Vote epoch for weightsAt: {vote_epoch} ({datetime.utcfromtimestamp(vote_epoch).isoformat()} UTC)\n"
            f"Gauge universe from DB: {len(gauge_set)} gauges | Token rows: {len(rows)}\n"
            f"Filter: {'active-only' if active_only else 'active+inactive'}\n"
            f"Mode: {'offline-cache-only' if offline_only else ('online-refresh' if args.refresh_cache else 'online-with-cache')}",
            border_style="cyan",
        )
    )

    vote_failures = 0
    reward_failures = 0
    states: List[GaugeBoundaryState] = []

    if args.refresh_cache:
        states = []
    else:
        cached_states, cached_block = load_cached_states(cur, args.epoch, vote_epoch, active_only, args.max_gauges)
        if cached_states:
            states = cached_states
            if cached_block > 0:
                boundary_block = cached_block
            console.print(
                f"[green]Loaded {len(states)} cached gauge states from DB (epoch={args.epoch}, vote_epoch={vote_epoch})[/green]"
            )

    if offline_only and not states:
        console.print(
            "[red]No cached gauge states found for this epoch/vote_epoch/filter. "
            "Run once without --offline-only to build cache.[/red]"
        )
        return

    if not states:
        # Query boundary votes per pool via weightsAt(pool, vote_epoch)
        votes_by_pool_raw: Dict[str, int] = {}
        votes_phase_start = time.time()
        console.print(f"[cyan]Phase 2/6: Querying pool votes ({len(pool_set)} pools)[/cyan]")
        for idx, pool in enumerate(track(pool_set, description="Querying pool weightsAt at boundary"), start=1):
            try:
                v = voter.functions.weightsAt(Web3.to_checksum_address(pool), vote_epoch).call(block_identifier=boundary_block)
                votes_by_pool_raw[pool] = int(v)
            except Exception:
                vote_failures += 1
                votes_by_pool_raw[pool] = 0
            if args.progress_every > 0 and idx % args.progress_every == 0:
                elapsed = max(time.time() - votes_phase_start, 1e-9)
                rate = idx / elapsed
                remaining = max(0, len(pool_set) - idx)
                eta = remaining / rate if rate > 0 else float("inf")
                console.print(
                    f"[dim]votes progress: {idx}/{len(pool_set)} | {rate:.2f} pools/s | ETA {_format_eta(eta)}[/dim]"
                )

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
        rewards_phase_start = time.time()

        console.print(f"[cyan]Phase 3/6: Querying reward snapshots ({len(rows)} bribe/token rows)[/cyan]")
        for idx, (gauge, pool, bribe_contract, reward_token, token_decimals, usd_price) in enumerate(
            track(rows, description="Querying rewardData at boundary"), start=1
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

            except Exception:
                reward_failures += 1
            if args.progress_every > 0 and idx % args.progress_every == 0:
                elapsed = max(time.time() - rewards_phase_start, 1e-9)
                rate = idx / elapsed
                remaining = max(0, len(rows) - idx)
                eta = remaining / rate if rate > 0 else float("inf")
                console.print(
                    f"[dim]reward progress: {idx}/{len(rows)} | {rate:.2f} rows/s | ETA {_format_eta(eta)}[/dim]"
                )

        console.print("[cyan]Phase 4/6: Building gauge state table[/cyan]")
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

        save_states_to_cache(conn, args.epoch, vote_epoch, active_only, boundary_block, states)
        console.print(
            f"[green]Saved {len(states)} gauge states to cache (epoch={args.epoch}, vote_epoch={vote_epoch})[/green]"
        )

    if not states:
        console.print("[red]No positive-USD gauge states computed at boundary[/red]")
        return

    console.print("[cyan]Phase 5/6: Solving 1-pool and K-pool optimization[/cyan]")
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
    total_combos = math.comb(len(candidates), effective_k)
    combo_phase_start = time.time()
    console.print(
        f"[cyan]Combination search: C({len(candidates)}, {effective_k}) = {total_combos:,}[/cyan]"
    )

    for combo in combinations(candidates, effective_k):
        combos += 1
        alloc = solve_alloc_for_set(list(combo), args.voting_power, args.min_votes_per_pool)
        ret = sum(expected_return(s.total_usd, s.votes_raw, x) for s, x in zip(combo, alloc))
        if ret > best_k_return:
            best_k_return = ret
            best_combo = list(combo)
            best_alloc = alloc
        if args.progress_every > 0 and combos % (args.progress_every * 10) == 0:
            pct = (combos / total_combos) * 100 if total_combos > 0 else 0
            elapsed = max(time.time() - combo_phase_start, 1e-9)
            rate = combos / elapsed
            remaining = max(0, total_combos - combos)
            eta = remaining / rate if rate > 0 else float("inf")
            console.print(
                f"[dim]combo progress: {combos:,}/{total_combos:,} ({pct:.1f}%) | {rate:.1f} combos/s | ETA {_format_eta(eta)}[/dim]"
            )

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

    console.print("[cyan]Phase 6/6: Rendering output tables[/cyan]")
    console.print()
    console.print(summary)

    symbol_cache: Dict[str, str] = {}
    pool_label_cache: Dict[str, str] = {}
    if offline_only:
        best_pool_label = str(best_state.pool)
    else:
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
        if offline_only:
            pool_label = str(s.pool)
        else:
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
