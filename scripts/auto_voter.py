#!/usr/bin/env python3
"""
Auto-Voter: Executes optimized votes automatically at the optimal time (N blocks before boundary).

This script:
1. Fetches fresh live snapshot data
2. Calculates optimal allocation
3. Builds and signs vote transaction
4. Executes vote (or dry-run for testing)

Safety Features:
- Dry-run mode (no actual transaction)
- Transaction simulation before sending
- Gas price limits
- Vote amount validation
- Comprehensive logging

Note: Vote proportions are relative weights derived from optimized vote allocations.
The contract normalizes these weights internally.
VOTE_DELAY is currently 0, so you can re-vote multiple times per epoch (not twice in same block).
"""

import argparse
import json
import os
import re
import sqlite3
import sys
import time
from typing import Dict, List, Optional, Tuple, Union

from dotenv import load_dotenv
from eth_utils import keccak
from eth_account import Account
from rich.console import Console
from rich.table import Table
from web3 import Web3
from web3.exceptions import ContractLogicError

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config.settings import (
    DATABASE_PATH,
    HYDREX_PRICE_REFRESH_MAX_FAILURES,
    ONE_E18,
    VOTER_ADDRESS,
    WEEK,
)
from src.allocation_tracking import save_executed_allocation
from src.database import Database
from src.price_feed import PriceFeed

load_dotenv()

# Load MY_ESCROW_ADDRESS from environment (escrow account)
MY_ESCROW_ADDRESS = os.getenv("MY_ESCROW_ADDRESS", "").lower()

console = Console()

# Require a small balance headroom over estimated tx fee so minor gas movement
# between preflight and send does not cause avoidable failures.
GAS_BALANCE_HEADROOM_MULTIPLIER = 1.15

# Load Voter ABI
VOTERV5_ABI_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "voterv5_abi.json")
with open(VOTERV5_ABI_PATH, "r") as f:
    VOTER_ABI = json.load(f)

# Minimal Pool ABI for token0/token1 calls
POOL_ABI = [
    {"constant": True, "inputs": [], "name": "token0", "outputs": [{"name": "", "type": "address"}], "type": "function"},
    {"constant": True, "inputs": [], "name": "token1", "outputs": [{"name": "", "type": "address"}], "type": "function"},
]

# Minimal ERC20 ABI for symbol calls
ERC20_ABI = [
    {"constant": True, "inputs": [], "name": "symbol", "outputs": [{"name": "", "type": "string"}], "type": "function"},
]

# Minimal PartnerEscrow ABI (for forwarding vote calls)
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


def _build_error_selector_map(abi: List[Dict]) -> Dict[str, str]:
    """Build selector -> error signature map from ABI custom errors."""
    selector_map: Dict[str, str] = {}
    for item in abi:
        if item.get("type") != "error":
            continue
        types = ",".join(inp.get("type", "") for inp in item.get("inputs", []))
        sig = f"{item['name']}({types})"
        selector = "0x" + keccak(text=sig)[:4].hex()
        selector_map[selector.lower()] = sig
    return selector_map


ERROR_SELECTOR_MAP = _build_error_selector_map(VOTER_ABI)


def _utc_iso(ts: int) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(int(ts)))


def _read_onchain_epoch_timestamp(voter_contract, block_identifier: Union[str, int]) -> int:
    """Read current on-chain epoch timestamp from Voter contract."""
    candidates = ("_epochTimestamp", "epochTimestamp")
    last_error: Optional[Exception] = None

    for fn_name in candidates:
        fn = getattr(voter_contract.functions, fn_name, None)
        if fn is None:
            continue
        try:
            return int(fn().call(block_identifier=block_identifier))
        except Exception as exc:
            last_error = exc

    if last_error:
        raise RuntimeError(f"Failed to read on-chain epoch timestamp: {last_error}")
    raise RuntimeError("Voter ABI missing _epochTimestamp/epochTimestamp view")


def _fetch_chain_boundary_context(w3: Web3) -> Dict[str, int]:
    """Return latest block context + on-chain epoch timestamp."""
    latest_block = w3.eth.get_block("latest")
    latest_block_number = int(latest_block["number"])
    latest_block_ts = int(latest_block["timestamp"])

    voter_contract = w3.eth.contract(
        address=Web3.to_checksum_address(VOTER_ADDRESS),
        abi=VOTER_ABI,
    )
    onchain_epoch_ts = _read_onchain_epoch_timestamp(voter_contract, block_identifier=latest_block_number)

    return {
        "latest_block_number": latest_block_number,
        "latest_block_ts": latest_block_ts,
        "onchain_epoch_ts": int(onchain_epoch_ts),
    }


def evaluate_pre_boundary_guard(
    w3: Web3,
    vote_epoch: int,
    min_seconds_before_boundary: int,
    phase_label: str,
    enforce_guard: bool,
    checkpoint_label: str,
) -> Tuple[bool, str, Dict[str, int]]:
    """Evaluate hard pre-boundary guard from chain-truth epoch/time."""
    context = _fetch_chain_boundary_context(w3)
    next_epoch_start = int(vote_epoch) + int(WEEK)
    seconds_until_boundary = int(next_epoch_start) - int(context["latest_block_ts"])

    context["vote_epoch"] = int(vote_epoch)
    context["next_epoch_start"] = int(next_epoch_start)
    context["seconds_until_boundary"] = int(seconds_until_boundary)

    console.print(
        f"[cyan]Boundary guard ({phase_label or 'unlabeled'}:{checkpoint_label}) | "
        f"block={context['latest_block_number']} ({_utc_iso(context['latest_block_ts'])}) | "
        f"onchain_epoch={context['onchain_epoch_ts']} ({_utc_iso(context['onchain_epoch_ts'])}) | "
        f"vote_epoch={vote_epoch} ({_utc_iso(vote_epoch)}) | "
        f"next_epoch_start={next_epoch_start} ({_utc_iso(next_epoch_start)}) | "
        f"seconds_until_boundary={seconds_until_boundary}[/cyan]"
    )

    if not enforce_guard:
        return True, "guard_disabled", context

    if int(context["onchain_epoch_ts"]) > int(vote_epoch):
        return (
            False,
            "Boundary guard abort: on-chain epoch already advanced (mint/flip detected)",
            context,
        )

    # Negative min_seconds_before_boundary means allow this many seconds PAST the boundary
    # (e.g. -20 lets Phase 3 send until T+20s, before the contract's epoch-flip block arrives).
    post_boundary_tolerance = max(0, -int(min_seconds_before_boundary))
    if int(context["latest_block_ts"]) >= int(next_epoch_start) + post_boundary_tolerance:
        return (
            False,
            f"Boundary guard abort: chain time is at/after boundary + {post_boundary_tolerance}s tolerance",
            context,
        )

    if int(min_seconds_before_boundary) > 0 and int(seconds_until_boundary) < int(min_seconds_before_boundary):
        return (
            False,
            f"Boundary guard abort: only {seconds_until_boundary}s until boundary (< {min_seconds_before_boundary}s minimum)",
            context,
        )

    return True, "ok", context


def ensure_auto_vote_runs_table(conn: sqlite3.Connection) -> None:
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS auto_vote_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            initiated_at INTEGER NOT NULL,
            execution_started_at INTEGER,
            vote_sent_at INTEGER,
            completed_at INTEGER,
            status TEXT NOT NULL,
            dry_run INTEGER NOT NULL,
            snapshot_ts INTEGER,
            vote_epoch INTEGER,
            query_block INTEGER,
            selected_k INTEGER,
            pool_count INTEGER,
            expected_return_usd REAL,
            tx_hash TEXT,
            error_text TEXT,
            created_at INTEGER NOT NULL DEFAULT (strftime('%s','now'))
        )
        """
    )
    conn.commit()


def create_auto_vote_run(conn: sqlite3.Connection, initiated_at: int, dry_run: bool) -> int:
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO auto_vote_runs (initiated_at, status, dry_run)
        VALUES (?, ?, ?)
        """,
        (int(initiated_at), "started", 1 if bool(dry_run) else 0),
    )
    conn.commit()
    return int(cur.lastrowid)


def update_auto_vote_run(conn: sqlite3.Connection, run_id: int, **fields) -> None:
    if not fields:
        return
    cols = []
    values = []
    for key, val in fields.items():
        cols.append(f"{key} = ?")
        values.append(val)
    values.append(int(run_id))
    sql = f"UPDATE auto_vote_runs SET {', '.join(cols)} WHERE id = ?"
    conn.execute(sql, tuple(values))
    conn.commit()


def persist_executed_allocation_for_run(
    conn: sqlite3.Connection,
    run_id: int,
    vote_epoch: int,
    allocation: List[Tuple[str, str, int, float, float, float]],
    tx_hash: Optional[str],
    source: str,
) -> int:
    if int(run_id) <= 0 or int(vote_epoch) <= 0:
        return 0

    target_epoch = int(vote_epoch) + int(WEEK)
    strategy_tag = f"auto_voter_run_{int(run_id)}"
    rows = [
        (
            int(rank),
            str(gauge_addr).lower(),
            str(pool_addr).lower(),
            int(votes),
        )
        for rank, (gauge_addr, pool_addr, votes, _base, _rewards, _expected) in enumerate(allocation, start=1)
        if int(votes) > 0
    ]
    if not rows:
        return 0

    return int(
        save_executed_allocation(
            conn=conn,
            epoch=int(target_epoch),
            strategy_tag=strategy_tag,
            rows=rows,
            source=str(source),
            tx_hash=(str(tx_hash) if tx_hash else None),
        )
    )


def _decode_revert_selector(error_text: str) -> Tuple[Optional[str], Optional[str]]:
    """Return (selector, known_signature) when present in an error string."""
    match = re.search(r"0x[a-fA-F0-9]{8}", error_text or "")
    if not match:
        return None, None
    selector = match.group(0).lower()
    return selector, ERROR_SELECTOR_MAP.get(selector)


def get_token_symbol_from_db(db_conn, token_address: str) -> Optional[str]:
    """Fetch token symbol from database metadata cache."""
    try:
        cur = db_conn.cursor()
        row = cur.execute(
            "SELECT symbol FROM token_metadata WHERE LOWER(address) = LOWER(?)",
            (token_address,)
        ).fetchone()
        if row and row[0] and "..." not in row[0]:
            return row[0]
    except Exception:
        pass
    return None


def get_pool_name(w3: Web3, pool_address: str, db_conn) -> str:
    """
    Fetch pool name as 'token0/token1' using token symbols.
    Falls back to shortened address if tokens cannot be fetched.
    Prioritizes database cache, falls back to RPC calls.
    """
    if not pool_address or pool_address == "0x0000000000000000000000000000000000000000":
        return "Unknown"
    
    try:
        pool = w3.eth.contract(address=Web3.to_checksum_address(pool_address), abi=POOL_ABI)
        token0 = pool.functions.token0().call()
        token1 = pool.functions.token1().call()
        
        # Try fetching from DB first
        sym0 = get_token_symbol_from_db(db_conn, token0)
        sym1 = get_token_symbol_from_db(db_conn, token1)
        
        # Fall back to RPC if not in DB
        if not sym0:
            try:
                token0_contract = w3.eth.contract(address=Web3.to_checksum_address(token0), abi=ERC20_ABI)
                sym0 = token0_contract.functions.symbol().call()
                if isinstance(sym0, bytes):
                    sym0 = sym0.decode("utf-8").rstrip("\x00")
            except Exception:
                sym0 = None
        
        if not sym1:
            try:
                token1_contract = w3.eth.contract(address=Web3.to_checksum_address(token1), abi=ERC20_ABI)
                sym1 = token1_contract.functions.symbol().call()
                if isinstance(sym1, bytes):
                    sym1 = sym1.decode("utf-8").rstrip("\x00")
            except Exception:
                sym1 = None
        
        # If both symbols available, return formatted name
        if sym0 and sym1:
            return f"{sym0}/{sym1}"
        
        # Otherwise fall back to shortened address
        return f"{pool_address[:6]}...{pool_address[-4:]}"
    except Exception as e:
        return f"{pool_address[:6]}...{pool_address[-4:]}"


def load_wallet(private_key_source: str) -> Account:
    """Load wallet from private key source: raw key or file path."""
    if os.path.isfile(private_key_source):
        with open(private_key_source, "r") as f:
            private_key = f.read().strip()
    else:
        private_key = private_key_source
    
    # Remove 0x prefix if present
    if private_key.startswith("0x"):
        private_key = private_key[2:]
    
    return Account.from_key(private_key)


def fetch_fresh_snapshot(
    conn: sqlite3.Connection,
    w3: Web3,
    query_block: int,
    discover_missing_pairs: bool = False,
) -> Tuple[int, int, int]:
    """
    Fetch fresh live snapshot by calling the fetch_live_snapshot module.
    Returns (snapshot_ts, vote_epoch, query_block).
    """
    from data.fetchers.fetch_live_snapshot import (
        ensure_live_tables,
        fetch_live_snapshot,
        fetch_votes_only_refresh,
        resolve_vote_epoch,
    )
    
    now_ts = int(time.time())
    vote_epoch = resolve_vote_epoch(conn, now_ts=now_ts, forced_vote_epoch=0)
    
    if query_block <= 0:
        query_block = int(w3.eth.block_number)
    
    console.print(f"[cyan]Fetching fresh snapshot at block {query_block}, vote_epoch={vote_epoch}...[/cyan]")
    
    started = time.perf_counter()
    snapshot_ts, token_rows, gauge_rows = fetch_live_snapshot(
        conn=conn,
        w3=w3,
        query_block=query_block,
        vote_epoch=vote_epoch,
        max_gauges=0,  # All gauges
        progress_every=100,
        progress_every_batches=3,
        discover_missing_pairs=discover_missing_pairs,
        pairs_cache_path=os.path.join(os.path.dirname(__file__), "..", "data", "fetchers", "discovered_pairs.json"),
    )
    elapsed = time.perf_counter() - started
    console.print(f"[green]✓ Fresh snapshot saved: snapshot_ts={snapshot_ts}, gauge_rows={gauge_rows}[/green]")
    console.print(f"[dim]Fetch timing: {elapsed:.2f}s (token_rows={token_rows}, gauge_rows={gauge_rows})[/dim]")
    return snapshot_ts, vote_epoch, query_block


def load_rewards_usd_by_gauge(
    conn: sqlite3.Connection,
    snapshot_ts: int,
) -> Tuple[Dict[str, float], int, int]:
    """Load rewards USD per gauge in one SQL pass.

    Returns:
        (rewards_usd_by_gauge, priced_token_rows, total_token_rows)
    """
    cur = conn.cursor()

    # Use both historical_token_prices and token_prices; pick the record
    # with the closest timestamp <= snapshot_ts per token.  This prevents
    # newly-listed bribe tokens (present in token_prices but not yet in
    # historical_token_prices) from inflating mid-tier pool rewards with
    # unvalidated prices.
    _price_cte = """
        WITH price_candidates AS (
            SELECT lower(token_address) AS token_address,
                   usd_price,
                   timestamp AS ts
            FROM historical_token_prices
            WHERE timestamp <= ?
              AND COALESCE(usd_price, 0) > 0
            UNION ALL
            SELECT lower(token_address) AS token_address,
                   usd_price,
                   updated_at AS ts
            FROM token_prices
            WHERE updated_at <= ?
              AND COALESCE(usd_price, 0) > 0
        ),
        latest_ts AS (
            SELECT token_address, MAX(ts) AS max_ts
            FROM price_candidates
            GROUP BY token_address
        ),
        best_prices AS (
            SELECT c.token_address, c.usd_price
            FROM price_candidates c
            JOIN latest_ts l
              ON c.token_address = l.token_address AND c.ts = l.max_ts
        )
    """

    gauge_rows = cur.execute(
        _price_cte + """
        SELECT LOWER(s.gauge_address) AS gauge_address,
               SUM(CAST(s.rewards_normalized AS REAL) * COALESCE(CAST(p.usd_price AS REAL), 0.0)) AS rewards_usd
         FROM live_reward_token_samples s
        LEFT JOIN best_prices p ON LOWER(s.reward_token) = p.token_address
        WHERE s.snapshot_ts = ?
        GROUP BY LOWER(s.gauge_address)
        """,
        (snapshot_ts, snapshot_ts, snapshot_ts),
    ).fetchall()

    stats_row = cur.execute(
        _price_cte + """
        SELECT
            SUM(CASE WHEN p.usd_price IS NOT NULL THEN 1 ELSE 0 END) AS priced_rows,
            COUNT(*) AS total_rows
        FROM live_reward_token_samples s
        LEFT JOIN best_prices p ON LOWER(s.reward_token) = p.token_address
        WHERE s.snapshot_ts = ?
        """,
        (snapshot_ts, snapshot_ts, snapshot_ts),
    ).fetchone()

    priced_rows = int(stats_row[0] or 0) if stats_row else 0
    total_rows = int(stats_row[1] or 0) if stats_row else 0
    rewards_map = {str(gauge): float(rewards_usd or 0.0) for gauge, rewards_usd in gauge_rows}
    return rewards_map, priced_rows, total_rows


def refresh_snapshot_token_prices(
    conn: sqlite3.Connection,
    snapshot_ts: int,
    db_path: str,
    api_key: str,
    max_age_hours: float,
    max_failures: int,
) -> Tuple[int, int, int, int]:
    """Refresh token prices used by a snapshot before allocation math."""
    cur = conn.cursor()
    rows = cur.execute(
        """
        SELECT DISTINCT LOWER(reward_token)
        FROM live_reward_token_samples
        WHERE snapshot_ts = ?
          AND reward_token IS NOT NULL
          AND TRIM(reward_token) != ''
        """,
        (int(snapshot_ts),),
    ).fetchall()
    all_tokens = sorted({str(r[0]).lower() for r in rows if r and r[0]})
    total_tokens = len(all_tokens)

    if total_tokens == 0:
        console.print("[yellow]No snapshot reward tokens found for price refresh[/yellow]")
        return 0, 0, 0, 0

    cutoff_ts = -1
    if float(max_age_hours) > 0:
        cutoff_ts = int(time.time() - (float(max_age_hours) * 3600.0))

    # Use a fresh autocommit connection for the token_prices lookup to guarantee we
    # read the latest committed rows regardless of the calling connection's transaction
    # state (Phase 1 saves prices via SQLAlchemy; stale read transactions on `conn`
    # can cause the lookup to return 0 rows even though Phase 1 already committed).
    existing_updated_at: Dict[str, int] = {}
    chunk_size = 500
    with sqlite3.connect(db_path, isolation_level=None) as price_check_conn:
        price_check_cur = price_check_conn.cursor()
        for i in range(0, total_tokens, chunk_size):
            chunk = all_tokens[i : i + chunk_size]
            placeholders = ",".join(["?"] * len(chunk))
            chunk_rows = price_check_cur.execute(
                f"""
                SELECT LOWER(token_address), COALESCE(updated_at, 0)
                FROM token_prices
                WHERE LOWER(token_address) IN ({placeholders})
                """,
                tuple(chunk),
            ).fetchall()
            for token_addr, updated_at in chunk_rows:
                existing_updated_at[str(token_addr).lower()] = int(updated_at or 0)

    cached_count = len(existing_updated_at)
    console.print(
        f"[dim]Price cache lookup: {cached_count}/{total_tokens} tokens found in token_prices[/dim]"
    )

    if cutoff_ts <= 0:
        target_tokens = list(all_tokens)
    else:
        target_tokens = [
            tok for tok in all_tokens if int(existing_updated_at.get(tok, 0)) < int(cutoff_ts)
        ]

    if not target_tokens:
        console.print(
            f"[green]✓ Price refresh skipped: all {total_tokens} snapshot tokens are fresh (< {max_age_hours:.2f}h old)[/green]"
        )
        return total_tokens, 0, 0, total_tokens

    console.print(
        f"[cyan]Refreshing token prices used in snapshot {snapshot_ts}: "
        f"targeting {len(target_tokens)}/{total_tokens} tokens[/cyan]"
    )

    database = Database(db_path)
    price_feed = PriceFeed(
        api_key=api_key or None,
        database=database,
        allow_coingecko_fallback=False,
    )

    successful = 0
    failed = 0
    save_failures = 0
    batch_size = 50

    for i in range(0, len(target_tokens), batch_size):
        batch = target_tokens[i : i + batch_size]
        batch_prices: Dict[str, float] = {}
        try:
            batch_prices = price_feed.fetch_batch_prices_by_address(batch)
        except Exception:
            batch_prices = {}

        for token in batch:
            token_l = str(token).lower()
            price = batch_prices.get(token_l)
            if price is None:
                price = price_feed.get_token_price(token_l)
            if price is None:
                failed += 1
                continue
            try:
                database.save_token_price(token_l, float(price))
                successful += 1
            except Exception:
                save_failures += 1

    total_failures = failed + save_failures
    console.print(
        "[cyan]Price refresh summary:[/cyan] "
        f"updated={successful}, fetch_failures={failed}, save_failures={save_failures}, "
        f"untouched_fresh={max(0, total_tokens - len(target_tokens))}"
    )

    if total_failures > int(max_failures):
        raise RuntimeError(
            f"Price refresh failed for {total_failures} tokens (max allowed: {int(max_failures)})."
        )

    return total_tokens, len(target_tokens), successful, total_failures


def expected_return_usd(total_usd: float, base_votes: float, your_votes: float) -> float:
    if your_votes <= 0:
        return 0.0
    denom = float(base_votes) + float(your_votes)
    if denom <= 0:
        return 0.0
    return float(total_usd) * (float(your_votes) / denom)


def marginal_gain_usd(total_usd: float, base_votes: float, current_votes: float, delta_votes: float) -> float:
    """Expected return gain from adding delta votes at current allocation level."""
    if delta_votes <= 0:
        return 0.0
    current = expected_return_usd(total_usd, base_votes, current_votes)
    after = expected_return_usd(total_usd, base_votes, current_votes + delta_votes)
    return max(0.0, after - current)


def marginal_loss_usd(total_usd: float, base_votes: float, current_votes: float, delta_votes: float) -> float:
    """Expected return loss from removing delta votes at current allocation level."""
    if delta_votes <= 0 or current_votes <= 0:
        return 0.0
    before = expected_return_usd(total_usd, base_votes, current_votes)
    after = expected_return_usd(total_usd, base_votes, max(0.0, current_votes - delta_votes))
    return max(0.0, before - after)


def solve_marginal_allocation(
    states: List[Tuple[str, str, float, float]],
    total_votes: int,
    min_per_pool: int,
    max_selected_pools: int,
    chunk_size: int = 1000,
) -> List[int]:
    """Discrete marginal allocator using vote chunks with dynamic pool entry/swap.

    - Seeds top pools with minimum allocation floor.
    - Allocates remaining votes in chunked marginal-return steps.
    - Allows inactive candidates to replace active pools when beneficial.
    - Uses exact budget by assigning final remainder to best active candidate.
    """
    n = len(states)
    if n == 0:
        return []

    total_votes_i = int(total_votes)
    if total_votes_i <= 0:
        return [0] * n

    max_selected = max(1, min(int(max_selected_pools), n))
    step = max(1, int(chunk_size))
    min_per_pool_i = int(max(0, min_per_pool))
    if max_selected * min_per_pool_i > total_votes_i:
        raise ValueError("Infeasible allocation: k * min_per_pool exceeds voting power")

    rewards = [max(float(s[3]), 0.0) for s in states]
    base_votes = [max(float(s[2]), 0.0) for s in states]

    allocations = [0] * n
    floors = [0] * n

    # Seed by score order (states are expected pre-ranked by single-pool marginal score).
    seed_count = min(max_selected, n)
    for idx in range(seed_count):
        floors[idx] = min_per_pool_i
        allocations[idx] = min_per_pool_i

    used = sum(allocations)
    if used > total_votes_i:
        raise ValueError("Infeasible seeded allocation")

    remaining = total_votes_i - used

    def active_indices() -> List[int]:
        return [idx for idx, votes in enumerate(allocations) if votes > 0]

    def best_add_candidate(delta_votes: int) -> Tuple[int, float]:
        best_idx = -1
        best_gain = -1.0
        active = set(active_indices())
        active_count = len(active)
        for idx in range(n):
            if idx not in active and active_count >= max_selected:
                continue
            gain = marginal_gain_usd(rewards[idx], base_votes[idx], float(allocations[idx]), float(delta_votes))
            if gain > best_gain:
                best_gain = gain
                best_idx = idx
        return best_idx, max(0.0, best_gain)

    def best_active_add(delta_votes: int) -> Tuple[int, float]:
        best_idx = -1
        best_gain = -1.0
        for idx in active_indices():
            gain = marginal_gain_usd(rewards[idx], base_votes[idx], float(allocations[idx]), float(delta_votes))
            if gain > best_gain:
                best_gain = gain
                best_idx = idx
        return best_idx, max(0.0, best_gain)

    def worst_removable_active(delta_votes: int) -> Tuple[int, float]:
        worst_idx = -1
        worst_loss = float("inf")
        for idx in active_indices():
            if allocations[idx] - floors[idx] < delta_votes:
                continue
            loss = marginal_loss_usd(rewards[idx], base_votes[idx], float(allocations[idx]), float(delta_votes))
            if loss < worst_loss:
                worst_loss = loss
                worst_idx = idx
        if worst_idx < 0:
            return -1, 0.0
        return worst_idx, max(0.0, worst_loss)

    while remaining >= step:
        active = set(active_indices())
        active_count = len(active)
        candidate_idx, candidate_gain = best_add_candidate(step)
        if candidate_idx < 0:
            break

        if candidate_idx in active or active_count < max_selected:
            allocations[candidate_idx] += step
            remaining -= step
            continue

        removable_idx, removable_loss = worst_removable_active(step)
        if removable_idx >= 0 and candidate_gain > removable_loss:
            allocations[removable_idx] -= step
            allocations[candidate_idx] += step
            continue

        fallback_idx, _fallback_gain = best_active_add(step)
        if fallback_idx < 0:
            break
        allocations[fallback_idx] += step
        remaining -= step

    if remaining > 0:
        active = active_indices()
        if len(active) < max_selected:
            idx, _gain = best_add_candidate(remaining)
        else:
            idx, _gain = best_active_add(remaining)
        if idx < 0:
            idx = 0
        allocations[idx] += remaining
        remaining = 0

    total_alloc = sum(allocations)
    if total_alloc != total_votes_i:
        drift = total_votes_i - total_alloc
        target_idx = active_indices()[0] if active_indices() else 0
        allocations[target_idx] += drift

    return [int(v) for v in allocations]


def calculate_optimal_allocation(
    conn: sqlite3.Connection,
    snapshot_ts: int,
    your_voting_power: int,
    top_k: int,
    candidate_pools: int,
    min_votes_per_pool: int,
) -> Tuple[List[Tuple[str, str, int, float, float, float]], int, int]:
    """
    Calculate optimal allocation using marginal ROI.
    Returns:
        (allocation_rows, priced_token_rows, total_token_rows)

    allocation_rows entries are:
        (gauge_addr, pool_addr, vote_amount, current_votes, current_rewards, expected_to_us).
    """
    cur = conn.cursor()
    rows = cur.execute(
        """
        SELECT gauge_address, pool_address, votes_raw, rewards_normalized_total
        FROM live_gauge_snapshots
        WHERE snapshot_ts = ? AND is_alive = 1 AND rewards_normalized_total > 0
        ORDER BY rewards_normalized_total DESC
        """,
        (snapshot_ts,),
    ).fetchall()
    
    if not rows:
        console.print("[red]No live gauges with positive rewards found[/red]")
        return [], 0, 0

    rewards_usd_by_gauge, priced_token_rows, total_token_rows = load_rewards_usd_by_gauge(
        conn=conn,
        snapshot_ts=snapshot_ts,
    )
    
    reference_vote_size = float(your_voting_power) / float(max(1, top_k))

    scored = []
    for gauge_addr, pool_addr, votes_raw, _rewards_norm in rows:
        base_votes = float(votes_raw or 0.0)
        
        rewards_usd = float(rewards_usd_by_gauge.get(str(gauge_addr).lower(), 0.0))
        single_pool_return = expected_return_usd(rewards_usd, base_votes, reference_vote_size)
        adjusted_roi = rewards_usd / max(1.0, (base_votes + reference_vote_size))
        scored.append((gauge_addr, pool_addr, base_votes, rewards_usd, single_pool_return, adjusted_roi))

    scored.sort(key=lambda x: (x[4], x[5]), reverse=True)

    k = min(int(top_k), len(scored))
    if k <= 0:
        return [], priced_token_rows, total_token_rows

    candidate_n = max(k, min(int(candidate_pools), len(scored)))
    candidates = [(g, p, b, r) for g, p, b, r, _sr, _roi in scored[:candidate_n]]

    effective_min_votes = int(max(0, min_votes_per_pool))
    if k * effective_min_votes > int(your_voting_power):
        effective_min_votes = int(your_voting_power // max(1, k))

    alloc_votes = solve_marginal_allocation(
        states=candidates,
        total_votes=int(your_voting_power),
        min_per_pool=effective_min_votes,
        max_selected_pools=k,
        chunk_size=1000,
    )

    selected = []
    for (gauge, pool, base_votes, rewards_usd), votes_alloc in zip(candidates, alloc_votes):
        if int(votes_alloc) <= 0:
            continue
        expected_to_us = expected_return_usd(rewards_usd, base_votes, float(votes_alloc))
        selected.append((gauge, pool, int(votes_alloc), base_votes, rewards_usd, expected_to_us))

    selected.sort(key=lambda x: (x[2], x[5]), reverse=True)
    selected = selected[:k]

    return selected, priced_token_rows, total_token_rows


def auto_select_top_k(
    conn: sqlite3.Connection,
    snapshot_ts: int,
    your_voting_power: int,
    candidate_pools: int,
    min_votes_per_pool: int,
    min_k: int,
    max_k: int,
    step: int,
    return_tolerance_pct: float,
) -> Tuple[int, List[Tuple[str, str, int, float, float, float]], int, int]:
    """Sweep k range and pick smallest k within tolerated return loss from best."""
    k_start = max(1, int(min_k))
    k_end = max(k_start, int(max_k))
    k_step = max(1, int(step))
    tolerance_pct = max(0.0, float(return_tolerance_pct))

    best_k = k_start
    best_allocation: List[Tuple[str, str, int, float, float, float]] = []
    best_priced_rows = 0
    best_total_rows = 0
    best_expected = -1.0

    sweep_table = Table(title="Auto top-k sweep")
    sweep_table.add_column("k", justify="right", style="cyan")
    sweep_table.add_column("Pools Used", justify="right")
    sweep_table.add_column("Expected To Us ($)", justify="right", style="green")
    sweep_table.add_column("Expected $/1k", justify="right", style="yellow")
    sweep_table.add_column("Runtime", justify="right")

    sweep_results: List[Tuple[int, float, List[Tuple[str, str, int, float, float, float]], int, int]] = []

    for k_value in range(k_start, k_end + 1, k_step):
        iter_started = time.perf_counter()
        allocation, priced_rows, total_rows = calculate_optimal_allocation(
            conn=conn,
            snapshot_ts=snapshot_ts,
            your_voting_power=int(your_voting_power),
            top_k=int(k_value),
            candidate_pools=int(max(candidate_pools, k_value)),
            min_votes_per_pool=int(min_votes_per_pool),
        )
        elapsed = time.perf_counter() - iter_started

        total_expected = sum(float(x[5]) for x in allocation)
        expected_per_1k = (total_expected * 1000.0) / max(1.0, float(your_voting_power))

        sweep_table.add_row(
            str(k_value),
            str(len(allocation)),
            f"${total_expected:,.2f}",
            f"${expected_per_1k:,.2f}",
            f"{elapsed:.2f}s",
        )
        sweep_results.append((int(k_value), float(total_expected), allocation, int(priced_rows), int(total_rows)))

        if (total_expected > best_expected + 1e-9) or (
            abs(total_expected - best_expected) <= 0.01 and k_value < best_k
        ):
            best_expected = float(total_expected)
            best_k = int(k_value)
            best_allocation = allocation
            best_priced_rows = int(priced_rows)
            best_total_rows = int(total_rows)

    if sweep_results and best_expected > 0.0:
        min_acceptable = float(best_expected) * (1.0 - (tolerance_pct / 100.0))
        within_tolerance = [r for r in sweep_results if float(r[1]) >= min_acceptable]
        if within_tolerance:
            chosen = min(within_tolerance, key=lambda x: int(x[0]))
            best_k = int(chosen[0])
            best_expected = float(chosen[1])
            best_allocation = chosen[2]
            best_priced_rows = int(chosen[3])
            best_total_rows = int(chosen[4])

    console.print(sweep_table)
    if tolerance_pct > 0:
        console.print(
            f"[cyan]Auto-k tolerance: {tolerance_pct:.2f}% | "
            f"selected smallest k within tolerance: k={best_k}[/cyan]"
        )
    console.print(
        f"[cyan]Auto-selected top-k={best_k} with expected return ${best_expected:,.2f}[/cyan]"
    )
    return best_k, best_allocation, best_priced_rows, best_total_rows


def validate_allocation(allocation: List[Tuple[str, str, int, float, float, float]], your_voting_power: int) -> bool:
    """Validate allocation meets requirements."""
    total_votes = sum(votes for _, _, votes, _, _, _ in allocation)
    
    if total_votes > your_voting_power:
        console.print(f"[red]✗ Total votes ({total_votes}) exceeds voting power ({your_voting_power})[/red]")
        return False
    
    if total_votes < your_voting_power * 0.95:  # Allow 5% tolerance
        console.print(f"[yellow]⚠ Total votes ({total_votes}) is less than 95% of voting power ({your_voting_power})[/yellow]")
    
    console.print(f"[green]✓ Allocation validated: {total_votes:,} / {your_voting_power:,} votes ({(total_votes/your_voting_power)*100:.1f}%)[/green]")
    return True


def simulate_vote_transaction(
    vote_contract,
    pool_addresses: List[str],
    vote_proportions: List[int],
    from_address: str,
    block_identifier: Union[str, int] = "latest",
) -> bool:
    """Simulate vote transaction using eth_call."""
    try:
        console.print(f"[cyan]Simulation signer address: {from_address}[/cyan]")
        # This will revert if the vote would fail
        vote_contract.functions.vote(pool_addresses, vote_proportions).call(
            {"from": from_address},
            block_identifier=block_identifier,
        )
        console.print("[green]✓ Transaction simulation successful[/green]")
        return True
    except ContractLogicError as e:
        err_text = str(e)
        selector, signature = _decode_revert_selector(err_text)
        if signature:
            console.print(f"[red]✗ Transaction simulation failed: {signature} ({selector})[/red]")
        elif selector:
            console.print(f"[red]✗ Transaction simulation failed with unknown selector: {selector}[/red]")
            console.print(f"[yellow]Likely reverted in a downstream contract call (not in VoterV5 ABI errors).[/yellow]")
        else:
            console.print(f"[red]✗ Transaction simulation failed: {err_text}[/red]")
        return False
    except Exception as e:
        err_text = str(e)
        selector, signature = _decode_revert_selector(err_text)
        if signature:
            console.print(f"[red]✗ Simulation error: {signature} ({selector})[/red]")
        elif selector:
            console.print(f"[red]✗ Simulation error with unknown selector: {selector}[/red]")
        else:
            console.print(f"[red]✗ Simulation error: {err_text}[/red]")
        return False


def build_and_send_vote_transaction(
    w3: Web3,
    vote_contract,
    wallet: Optional[Account],
    pool_addresses: List[str],
    vote_proportions: List[int],
    max_gas_price_gwei: float,
    partner_escrow_address: str,
    gas_limit: int,
    gas_buffer_multiplier: float,
    dry_run: bool = True,
    simulate_from_address: str = "",
    simulation_block_identifier: Union[str, int] = "latest",
    vote_epoch: int = 0,
    phase_label: str = "",
    min_seconds_before_boundary: int = 0,
    enforce_pre_boundary_guard: bool = True,
) -> Tuple[bool, str, Optional[int], Optional[int], Optional[int]]:
    """
    Build, sign, and send vote transaction.
    Transaction is signed by wallet and sent to PartnerEscrow (MY_ESCROW_ADDRESS).
    Returns (success, tx_hash_or_error, vote_sent_at, receipt_block, gas_used).
    """
    # Use zero address for dry-run if no wallet provided
    from_address = wallet.address if wallet else "0x0000000000000000000000000000000000000000"
    console.print(f"[cyan]Signer wallet address: {from_address}[/cyan]")
    console.print(f"[cyan]Transaction recipient (PartnerEscrow): {partner_escrow_address}[/cyan]")
    
    # Check current gas price
    current_gas_price = w3.eth.gas_price
    current_gas_price_gwei = float(current_gas_price) / 1e9
    
    console.print(f"[cyan]Current gas price: {current_gas_price_gwei:.2f} Gwei[/cyan]")
    
    if current_gas_price_gwei > max_gas_price_gwei:
        err = f"Gas price {current_gas_price_gwei:.2f} Gwei exceeds limit {max_gas_price_gwei} Gwei"
        console.print(f"[red]✗ {err}[/red]")
        return False, err, None, None, None
    
    console.print(f"[green]✓ Gas price acceptable (<= {max_gas_price_gwei} Gwei)[/green]")

    if int(vote_epoch) > 0:
        guard_ok, guard_reason, _guard_context = evaluate_pre_boundary_guard(
            w3=w3,
            vote_epoch=int(vote_epoch),
            min_seconds_before_boundary=int(min_seconds_before_boundary),
            phase_label=str(phase_label),
            enforce_guard=bool(enforce_pre_boundary_guard),
            checkpoint_label="pre_simulation",
        )
        if not guard_ok:
            console.print(f"[bold red]✗ {guard_reason}[/bold red]")
            return False, guard_reason, None, None, None
    
    simulation_signer = simulate_from_address or from_address
    if simulation_signer:
        console.print(
            f"[cyan]Simulating transaction at block={simulation_block_identifier} "
            f"(current latest={w3.eth.block_number})...[/cyan]"
        )
        if not simulate_vote_transaction(
            vote_contract,
            pool_addresses,
            vote_proportions,
            simulation_signer,
            block_identifier=simulation_block_identifier,
        ):
            return False, "Simulation failed", None, None, None
    else:
        console.print("[yellow]Skipping simulation (no simulation signer address provided)[/yellow]")
    
    # Build transaction
    try:
        nonce = w3.eth.get_transaction_count(from_address) if wallet else 0
        
        tx = {
            "from": from_address,
            "nonce": nonce,
            "gas": int(gas_limit),
            "gasPrice": current_gas_price,
            "chainId": w3.eth.chain_id if not dry_run else 8453,
        }
        
        # Only build full transaction if not dry-run or if we have a wallet
        if not dry_run or wallet:
            tx = vote_contract.functions.vote(pool_addresses, vote_proportions).build_transaction(tx)
            
            # Estimate gas
            if not dry_run:
                try:
                    estimate_call = {
                        "from": from_address,
                        "to": tx.get("to", Web3.to_checksum_address(partner_escrow_address)),
                        "data": tx.get("data", "0x"),
                    }
                    estimated_gas = w3.eth.estimate_gas(estimate_call)
                    estimated_with_buffer = int(estimated_gas * float(gas_buffer_multiplier))
                    tx["gas"] = max(int(gas_limit), estimated_with_buffer)
                    console.print(
                        f"[cyan]Estimated gas: {estimated_gas:,} "
                        f"(buffer x{gas_buffer_multiplier:.2f} => {estimated_with_buffer:,}, using {tx['gas']:,})[/cyan]"
                    )
                except Exception as e:
                    console.print(f"[yellow]⚠ Gas estimation failed, using default: {e}[/yellow]")
        
        tx_cost_wei = int(tx["gas"]) * int(current_gas_price)
        tx_cost_eth = tx_cost_wei / 1e18
        required_balance_wei = int(tx_cost_wei * GAS_BALANCE_HEADROOM_MULTIPLIER)
        required_balance_eth = required_balance_wei / 1e18
        console.print(f"[cyan]Estimated transaction cost: {tx_cost_eth:.6f} ETH[/cyan]")

        if wallet:
            current_balance_wei = int(w3.eth.get_balance(from_address))
            current_balance_eth = current_balance_wei / 1e18
            console.print(
                "[cyan]Gas balance check: "
                f"wallet={current_balance_eth:.6f} ETH, "
                f"required(with {GAS_BALANCE_HEADROOM_MULTIPLIER:.2f}x headroom)={required_balance_eth:.6f} ETH[/cyan]"
            )
            if current_balance_wei < required_balance_wei:
                err = (
                    "Insufficient gas balance for vote transaction: "
                    f"have {current_balance_eth:.6f} ETH, need at least {required_balance_eth:.6f} ETH "
                    f"(estimated fee {tx_cost_eth:.6f} ETH)."
                )
                console.print(f"[bold red]✗ {err}[/bold red]")
                return False, err, None, None, None
        
        if dry_run:
            dry_run_from = wallet.address if wallet else (simulation_signer or from_address)
            console.print("\n[bold yellow]═══ DRY RUN MODE - NO TRANSACTION SENT ═══[/bold yellow]")
            console.print(f"[yellow]Would send transaction:[/yellow]")
            console.print(f"  From: {dry_run_from}")
            console.print(f"  To: {partner_escrow_address}")
            console.print(f"  Nonce: {nonce if wallet else 'N/A'}")
            console.print(f"  Gas: {tx.get('gas', 'N/A'):,}" if 'gas' in tx else "  Gas: (estimate)")
            console.print(f"  Gas Price: {current_gas_price_gwei:.2f} Gwei")
            console.print(f"  Estimated Cost: {tx_cost_eth:.6f} ETH" if 'gas' in tx else "  Estimated Cost: (unknown)")
            console.print(f"  Pools: {len(pool_addresses)}")
            console.print(f"  Vote Proportions (weights): {vote_proportions}")
            return True, "DRY_RUN_SUCCESS", None, None, None
        
        if not wallet:
            return False, "No wallet provided for actual transaction", None, None, None

        if int(vote_epoch) > 0:
            guard_ok, guard_reason, _guard_context = evaluate_pre_boundary_guard(
                w3=w3,
                vote_epoch=int(vote_epoch),
                min_seconds_before_boundary=int(min_seconds_before_boundary),
                phase_label=str(phase_label),
                enforce_guard=bool(enforce_pre_boundary_guard),
                checkpoint_label="pre_send",
            )
            if not guard_ok:
                console.print(f"[bold red]✗ {guard_reason}[/bold red]")
                return False, guard_reason, None, None, None
        
        # Sign transaction
        console.print("[cyan]Signing transaction...[/cyan]")
        signed_tx = wallet.sign_transaction(tx)
        
        # Send transaction
        console.print("[cyan]Sending transaction...[/cyan]")
        tx_hash = w3.eth.send_raw_transaction(signed_tx.rawTransaction)
        tx_hash_hex = tx_hash.hex()
        vote_sent_at = int(time.time())
        console.print(f"[cyan]Vote sent at: {_utc_iso(vote_sent_at)}[/cyan]")
        
        console.print(f"[green]✓ Transaction sent: {tx_hash_hex}[/green]")
        console.print("[cyan]Waiting for transaction receipt...[/cyan]")
        
        # Wait for receipt (timeout after 5 minutes)
        receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=300)
        
        if receipt["status"] == 1:
            console.print(f"[bold green]✓ TRANSACTION SUCCESSFUL[/bold green]")
            console.print(f"  Block: {receipt['blockNumber']}")
            console.print(f"  Gas Used: {receipt['gasUsed']:,}")
            console.print(f"  Tx Hash: {tx_hash_hex}")
            return True, tx_hash_hex, vote_sent_at, int(receipt.get("blockNumber", 0)), int(receipt.get("gasUsed", 0))
        else:
            console.print(f"[bold red]✗ TRANSACTION FAILED[/bold red]")
            console.print(f"  Tx Hash: {tx_hash_hex}")
            return False, f"Transaction reverted: {tx_hash_hex}", vote_sent_at, int(receipt.get("blockNumber", 0)), int(receipt.get("gasUsed", 0))
        
    except Exception as e:
        console.print(f"[red]✗ Transaction error: {e}[/red]")
        return False, str(e), None, None, None


def main() -> None:
    parser = argparse.ArgumentParser(description="Automated voting executor with safety checks")
    auto_top_k_enabled_default = os.getenv("AUTO_TOP_K_ENABLED", "true").strip().lower() not in {"0", "false", "no", "off"}
    resolve_pool_names_default = os.getenv("AUTO_VOTE_RESOLVE_POOL_NAMES", "false").strip().lower() in {"1", "true", "yes", "on"}
    parser.add_argument("--db-path", default=DATABASE_PATH, help="Database path")
    parser.add_argument("--rpc", default=os.getenv("RPC_URL", ""), help="RPC URL")
    parser.add_argument("--your-voting-power", type=int, default=int(os.getenv("YOUR_VOTING_POWER", "0")), help="Your total voting power")
    parser.add_argument("--top-k", type=int, default=int(os.getenv("MAX_GAUGES_TO_VOTE", "10")), help="Number of gauges to vote for")
    parser.add_argument(
        "--candidate-pools",
        type=int,
        default=int(os.getenv("AUTO_TOP_K_CANDIDATE_POOLS", "60")),
        help="Candidate pool count before constrained marginal allocation",
    )
    parser.add_argument(
        "--auto-top-k",
        action=argparse.BooleanOptionalAction,
        default=auto_top_k_enabled_default,
        help="Auto-select top-k by sweeping a configured range (default: enabled)",
    )
    parser.add_argument("--auto-top-k-min", type=int, default=1, help="Minimum k for --auto-top-k sweep")
    parser.add_argument(
        "--auto-top-k-max",
        type=int,
        default=int(os.getenv("AUTO_TOP_K_MAX", "50")),
        help="Maximum k for --auto-top-k sweep",
    )
    parser.add_argument("--auto-top-k-step", type=int, default=1, help="Step size for --auto-top-k sweep")
    parser.add_argument(
        "--auto-top-k-return-tolerance-pct",
        type=float,
        default=float(os.getenv("AUTO_TOP_K_RETURN_TOLERANCE_PCT", "2.0")),
        help="Choose the smallest k within this %% of best expected return",
    )
    parser.add_argument(
        "--min-votes-per-pool",
        type=int,
        default=int(os.getenv("MIN_VOTE_ALLOCATION", "1000")),
        help="Minimum votes per selected pool for constrained optimization",
    )
    parser.add_argument("--query-block", type=int, default=0, help="Block to query (default: latest)")
    parser.add_argument("--discover-missing-pairs", action="store_true", help="On-chain enumerate missing reward tokens")
    parser.add_argument(
        "--resolve-pool-names",
        action=argparse.BooleanOptionalAction,
        default=resolve_pool_names_default,
        help="Resolve pool names via on-chain token lookups for display (default: disabled)",
    )
    parser.add_argument(
        "--private-key-source",
        default=os.getenv("TEST_WALLET_PK", "").strip(),
        help="Private key source: raw key (default from TEST_WALLET_PK) or file path override",
    )
    parser.add_argument("--max-gas-price-gwei", type=float, default=float(os.getenv("AUTO_VOTE_MAX_GAS_PRICE_GWEI", "10")), help="Max gas price in Gwei")
    parser.add_argument(
        "--gas-limit",
        type=int,
        default=int(os.getenv("AUTO_VOTE_GAS_LIMIT", "3000000")),
        help="Minimum transaction gas limit for vote tx (default: 3000000)",
    )
    parser.add_argument(
        "--gas-buffer-multiplier",
        type=float,
        default=float(os.getenv("AUTO_VOTE_GAS_BUFFER_MULTIPLIER", "1.35")),
        help="Multiplier applied to estimated gas for live tx (default: 1.35)",
    )
    parser.add_argument("--dry-run", action="store_true", help="Dry run mode (no actual transaction)")
    parser.add_argument("--skip-fresh-fetch", action="store_true", help="Skip fetching fresh snapshot (use latest in DB)")
    parser.add_argument("--votes-only-refresh", action="store_true", help="Phase-2 fast path: re-fetch only vote weights (weightsAt), skip bribe re-fetch and price refresh")
    parser.add_argument(
        "--refresh-prices-before-vote",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Deprecated compatibility flag; pre-vote price refresh is always enforced",
    )
    parser.add_argument(
        "--price-max-age-hours",
        type=float,
        default=0.0,
        help="Refresh prices older than this age in hours (default: 0=always refresh snapshot tokens)",
    )
    parser.add_argument(
        "--allow-stale-prices",
        action="store_true",
        help="Legacy override: continue even if some token prices fail to refresh",
    )
    parser.add_argument(
        "--allow-price-failures",
        type=int,
        default=int(HYDREX_PRICE_REFRESH_MAX_FAILURES),
        help="Maximum number of token price refresh failures allowed before abort (default from HYDREX_PRICE_REFRESH_MAX_FAILURES)",
    )
    parser.add_argument(
        "--simulate-from",
        default="",
        help="Address to use as msg.sender for simulation (default: wallet address, then MY_ESCROW_ADDRESS)",
    )
    parser.add_argument(
        "--simulation-block",
        default="latest",
        help="Block identifier for simulation (default: latest)",
    )
    parser.add_argument(
        "--phase-label",
        default=os.getenv("AUTO_VOTE_PHASE_LABEL", "manual"),
        help="Execution phase label for observability (e.g. phase1/phase2/manual)",
    )
    parser.add_argument(
        "--min-seconds-before-boundary",
        type=int,
        default=int(os.getenv("AUTO_VOTE_MIN_SECONDS_BEFORE_BOUNDARY", "0")),
        help="Abort if chain time is closer than this many seconds to boundary (default: 0=disabled)",
    )
    enforce_guard_default = os.getenv("AUTO_VOTE_ENFORCE_PRE_BOUNDARY_GUARD", "true").strip().lower() in {"1", "true", "yes", "on"}
    parser.add_argument(
        "--enforce-pre-boundary-guard",
        action=argparse.BooleanOptionalAction,
        default=enforce_guard_default,
        help="Enforce hard chain-time pre-boundary guard (default: enabled)",
    )
    args = parser.parse_args()
    
    # Validate inputs
    if not args.rpc:
        console.print("[red]Error: RPC_URL required[/red]")
        sys.exit(1)
    
    if args.your_voting_power <= 0:
        console.print("[red]Error: YOUR_VOTING_POWER must be > 0[/red]")
        sys.exit(1)

    if int(args.allow_price_failures) < 0:
        console.print("[red]Error: --allow-price-failures must be >= 0[/red]")
        sys.exit(1)

    if args.auto_top_k and args.auto_top_k_min > args.auto_top_k_max:
        console.print("[red]Error: --auto-top-k-min cannot be greater than --auto-top-k-max[/red]")
        sys.exit(1)

    if args.auto_top_k and args.auto_top_k_step <= 0:
        console.print("[red]Error: --auto-top-k-step must be > 0[/red]")
        sys.exit(1)

    if args.min_seconds_before_boundary < -300:
        console.print("[red]Error: --min-seconds-before-boundary must be >= -300 (post-boundary tolerance cap: 5 minutes)[/red]")
        sys.exit(1)

    if args.price_max_age_hours < 0:
        console.print("[red]Error: --price-max-age-hours must be >= 0[/red]")
        sys.exit(1)
    
    if not args.private_key_source and not args.dry_run:
        console.print("[red]Error: --private-key-source required (or use --dry-run)[/red]")
        sys.exit(1)
    
    # Connect to blockchain
    w3 = Web3(Web3.HTTPProvider(args.rpc))
    if not w3.is_connected():
        console.print("[red]Failed to connect to RPC[/red]")
        sys.exit(1)
    
    console.print(f"[green]✓ Connected to {args.rpc}[/green]")
    console.print(f"[cyan]Chain ID: {w3.eth.chain_id}, Latest Block: {w3.eth.block_number}[/cyan]")
    
    # Load wallet (required for actual tx, and preferred for dry-run signer parity)
    wallet = None
    if args.private_key_source:
        try:
            wallet = load_wallet(args.private_key_source)
            console.print(f"[green]✓ Wallet loaded: {wallet.address}[/green]")
            
            # Check balance
            balance = w3.eth.get_balance(wallet.address)
            balance_eth = float(balance) / 1e18
            console.print(f"[cyan]Wallet balance: {balance_eth:.6f} ETH[/cyan]")
            
            if balance_eth < 0.001:
                console.print("[yellow]⚠ Low wallet balance, may not have enough gas[/yellow]")

            # Early hard preflight for live mode: fail fast before expensive prep work.
            if not args.dry_run:
                current_gas_price = int(w3.eth.gas_price)
                worst_case_tx_fee_wei = int(args.gas_limit) * current_gas_price
                required_preflight_wei = int(
                    worst_case_tx_fee_wei * GAS_BALANCE_HEADROOM_MULTIPLIER
                )
                if int(balance) < required_preflight_wei:
                    required_preflight_eth = required_preflight_wei / 1e18
                    current_gas_price_gwei = current_gas_price / 1e9
                    console.print(
                        "[bold red]✗ Insufficient gas balance preflight failed:[/bold red] "
                        f"have {balance_eth:.6f} ETH, need at least {required_preflight_eth:.6f} ETH "
                        f"(gas_limit={args.gas_limit:,}, gas_price={current_gas_price_gwei:.6f} Gwei, "
                        f"headroom={GAS_BALANCE_HEADROOM_MULTIPLIER:.2f}x)."
                    )
                    sys.exit(1)
        except Exception as e:
            console.print(f"[red]✗ Failed to load wallet: {e}[/red]")
            sys.exit(1)
    elif args.dry_run:
        console.print("[yellow]Dry-run without wallet: simulation will use --simulate-from if provided[/yellow]")
    
    # Connect to database
    conn = sqlite3.connect(args.db_path)
    run_id: Optional[int] = None
    initiated_at = int(time.time())
    execution_started_at: Optional[int] = None
    vote_sent_at: Optional[int] = None
    tx_hash_or_result = ""
    final_status = "failed"
    
    try:
        ensure_auto_vote_runs_table(conn)
        run_id = create_auto_vote_run(conn=conn, initiated_at=initiated_at, dry_run=bool(args.dry_run))
        console.print(f"[cyan]Auto-vote initiated at: {_utc_iso(initiated_at)} (run_id={run_id})[/cyan]")

        # Fetch fresh snapshot (unless skipped or votes-only-refresh)
        if args.votes_only_refresh:
            from data.fetchers.fetch_live_snapshot import fetch_votes_only_refresh
            console.print("[cyan]Votes-only refresh: re-fetching vote weights only (skipping bribe data and price refresh)...[/cyan]")
            current_block = int(w3.eth.block_number) if args.query_block <= 0 else args.query_block
            snapshot_ts, vote_epoch, query_block = fetch_votes_only_refresh(
                conn=conn,
                w3=w3,
                query_block=current_block,
            )
        elif args.skip_fresh_fetch:
            console.print("[yellow]Skipping fresh snapshot fetch, using latest in DB...[/yellow]")
            cur = conn.cursor()
            row = cur.execute(
                """
                SELECT snapshot_ts, vote_epoch, query_block
                FROM live_gauge_snapshots
                WHERE snapshot_ts = (SELECT MAX(snapshot_ts) FROM live_gauge_snapshots)
                LIMIT 1
                """
            ).fetchone()
            if not row:
                console.print("[red]No snapshot found in DB[/red]")
                sys.exit(1)
            snapshot_ts, vote_epoch, query_block = int(row[0]), int(row[1]), int(row[2])
        else:
            snapshot_ts, vote_epoch, query_block = fetch_fresh_snapshot(
                conn=conn,
                w3=w3,
                query_block=args.query_block,
                discover_missing_pairs=args.discover_missing_pairs,
            )
        
        console.print(f"[cyan]Using snapshot: ts={snapshot_ts}, vote_epoch={vote_epoch}, block={query_block}[/cyan]")

        if args.votes_only_refresh:
            console.print("[cyan]Votes-only refresh: skipping price refresh (reusing Phase 1 prices from DB)[/cyan]")
        else:
            if not args.refresh_prices_before_vote:
                console.print(
                    "[yellow]Ignoring --no-refresh-prices-before-vote: pre-vote price refresh is mandatory[/yellow]"
                )

            max_price_failures = int(args.allow_price_failures)
            if bool(args.allow_stale_prices):
                # Preserve legacy behavior: effectively disable failure threshold.
                max_price_failures = 10**9

            refresh_snapshot_token_prices(
                conn=conn,
                snapshot_ts=int(snapshot_ts),
                db_path=args.db_path,
                api_key=os.getenv("COINGECKO_API_KEY", ""),
                max_age_hours=float(args.price_max_age_hours),
                max_failures=max_price_failures,
            )

            # Lock current token_prices into historical_token_prices at snap_ts so
            # the post-mortem can use the same prices that informed this allocation
            # decision, regardless of when the post-mortem is run.
            now_ts = int(time.time())
            try:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO historical_token_prices
                        (token_address, timestamp, granularity, usd_price, updated_at)
                    SELECT lower(token_address), ?, 'auto_voter_snap', usd_price, ?
                    FROM token_prices
                    WHERE COALESCE(usd_price, 0) > 0
                    """,
                    (int(snapshot_ts), now_ts),
                )
                conn.commit()
                snap_count = conn.execute(
                    "SELECT COUNT(*) FROM historical_token_prices WHERE timestamp = ? AND granularity = 'auto_voter_snap'",
                    (int(snapshot_ts),),
                ).fetchone()[0]
                console.print(f"[dim]Price snapshot locked: {snap_count} tokens at ts={snapshot_ts} (auto_voter_snap)[/dim]")
            except Exception as _price_snap_err:
                console.print(f"[yellow]Price snapshot warning: {_price_snap_err}[/yellow]")

        # Calculate optimal allocation
        console.print("[cyan]Calculating optimal allocation...[/cyan]")
        alloc_started = time.perf_counter()
        selected_top_k = int(args.top_k)
        if args.auto_top_k:
            selected_top_k, allocation, priced_token_rows, total_token_rows = auto_select_top_k(
                conn=conn,
                snapshot_ts=snapshot_ts,
                your_voting_power=int(args.your_voting_power),
                candidate_pools=int(args.candidate_pools),
                min_votes_per_pool=int(args.min_votes_per_pool),
                min_k=int(args.auto_top_k_min),
                max_k=int(args.auto_top_k_max),
                step=int(args.auto_top_k_step),
                return_tolerance_pct=float(args.auto_top_k_return_tolerance_pct),
            )
        else:
            allocation, priced_token_rows, total_token_rows = calculate_optimal_allocation(
                conn=conn,
                snapshot_ts=snapshot_ts,
                your_voting_power=args.your_voting_power,
                top_k=args.top_k,
                candidate_pools=args.candidate_pools,
                min_votes_per_pool=args.min_votes_per_pool,
            )
        alloc_elapsed = time.perf_counter() - alloc_started
        
        if not allocation:
            console.print("[red]No allocation generated[/red]")
            sys.exit(1)
        
        console.print(f"[green]✓ Allocated to {len(allocation)} pools[/green]")
        if args.auto_top_k:
            console.print(f"[cyan]Using auto-selected k={selected_top_k}[/cyan]")
        price_coverage = (float(priced_token_rows) * 100.0 / float(max(1, total_token_rows)))
        console.print(
            f"[dim]Allocation timing: {alloc_elapsed:.2f}s | "
            f"price coverage: {priced_token_rows}/{total_token_rows} token rows ({price_coverage:.1f}%)[/dim]"
        )
        if args.resolve_pool_names:
            console.print("[dim]Pool name resolution: enabled (may add RPC latency before send)[/dim]")
        else:
            console.print("[dim]Pool name resolution: disabled (using pool address labels)[/dim]")
        
        # Display allocation
        table = Table(title="Auto-Voter Allocation")
        table.add_column("#", justify="right")
        table.add_column("Pool Name")
        table.add_column("Pool Address")
        table.add_column("Current Votes", justify="right")
        table.add_column("Current Rewards ($)", justify="right")
        table.add_column("Current $/1k Votes", justify="right")
        table.add_column("Your Votes", justify="right")
        table.add_column("Expected To Us ($)", justify="right")
        table.add_column("Expected $/1k Votes", justify="right")
        
        pool_addresses = []
        vote_proportions = []
        total_expected_to_us = 0.0
        total_alloc_votes = sum(int(votes) for _, _, votes, _, _, _ in allocation)
        for idx, (gauge_addr, pool_addr, votes, current_votes, current_rewards, expected_to_us) in enumerate(allocation, start=1):
            if args.resolve_pool_names:
                pool_name = get_pool_name(w3, pool_addr, conn)
            else:
                pool_name = f"{pool_addr[:6]}...{pool_addr[-4:]}"
            current_per_1k_votes = (float(current_rewards) * 1000.0) / max(1.0, float(current_votes))
            expected_per_1k_votes = (float(expected_to_us) * 1000.0) / max(1.0, float(votes))
            table.add_row(
                str(idx), 
                pool_name, 
                pool_addr, 
                f"{int(current_votes):,}",
                f"${current_rewards:,.2f}",
                f"${current_per_1k_votes:,.2f}",
                f"{votes:,}",
                f"${expected_to_us:,.2f}",
                f"${expected_per_1k_votes:,.2f}"
            )
            total_expected_to_us += float(expected_to_us)
            pool_addresses.append(pool_addr)
            weight = max(1, int(round((float(votes) / max(1.0, float(total_alloc_votes))) * 1000000.0)))
            vote_proportions.append(weight)
        
        console.print(table)
        total_expected_per_1k_votes = (total_expected_to_us * 1000.0) / max(1.0, float(args.your_voting_power))
        console.print(
            f"[bold green]Total Expected To Us: ${total_expected_to_us:,.2f} "
            f"(${total_expected_per_1k_votes:,.2f} per 1k votes)[/bold green]"
        )
        
        # Validate allocation
        if not validate_allocation(allocation, args.your_voting_power):
            console.print("[red]Allocation validation failed[/red]")
            sys.exit(1)
        
        if not MY_ESCROW_ADDRESS:
            console.print("[red]Error: MY_ESCROW_ADDRESS is required to call PartnerEscrow.vote[/red]")
            sys.exit(1)

        # Load PartnerEscrow contract (call target)
        partner_escrow_contract = w3.eth.contract(
            address=Web3.to_checksum_address(MY_ESCROW_ADDRESS),
            abi=PARTNER_ESCROW_ABI,
        )
        
        # Build and send transaction
        console.print("\n[bold cyan]═══ EXECUTING VOTE ═══[/bold cyan]\n")

        simulation_from = args.simulate_from.strip() if args.simulate_from else ""
        if not simulation_from and wallet:
            simulation_from = wallet.address

        simulation_block: Union[str, int]
        simulation_block_raw = str(args.simulation_block).strip()
        if simulation_block_raw.isdigit():
            simulation_block = int(simulation_block_raw)
        else:
            simulation_block = simulation_block_raw or "latest"
        
        execution_started_at = int(time.time())
        if run_id is not None:
            update_auto_vote_run(conn, run_id, execution_started_at=int(execution_started_at))

        success, result, vote_sent_ts, _receipt_block, _gas_used = build_and_send_vote_transaction(
            w3=w3,
            vote_contract=partner_escrow_contract,
            wallet=wallet,
            pool_addresses=[Web3.to_checksum_address(addr) for addr in pool_addresses],
            vote_proportions=vote_proportions,
            max_gas_price_gwei=args.max_gas_price_gwei,
            partner_escrow_address=Web3.to_checksum_address(MY_ESCROW_ADDRESS),
            gas_limit=args.gas_limit,
            gas_buffer_multiplier=args.gas_buffer_multiplier,
            dry_run=args.dry_run,
            simulate_from_address=Web3.to_checksum_address(simulation_from) if simulation_from else "",
            simulation_block_identifier=simulation_block,
            vote_epoch=int(vote_epoch),
            phase_label=str(args.phase_label),
            min_seconds_before_boundary=int(args.min_seconds_before_boundary),
            enforce_pre_boundary_guard=bool(args.enforce_pre_boundary_guard),
        )
        vote_sent_at = vote_sent_ts
        tx_hash_or_result = str(result)
        
        if success:
            console.print(f"\n[bold green]✓ AUTO-VOTE COMPLETED SUCCESSFULLY[/bold green]")
            if not args.dry_run:
                console.print(f"[green]Transaction Hash: {result}[/green]")
            final_status = "dry_run_success" if args.dry_run else "tx_success"
        else:
            final_status = "failed"
            console.print(f"\n[bold red]✗ AUTO-VOTE FAILED: {result}[/bold red]")
            sys.exit(1)

        if run_id is not None:
            update_auto_vote_run(
                conn,
                run_id,
                completed_at=int(time.time()),
                status=str(final_status),
                snapshot_ts=int(snapshot_ts),
                vote_epoch=int(vote_epoch),
                query_block=int(query_block),
                selected_k=int(selected_top_k),
                pool_count=int(len(allocation)),
                expected_return_usd=float(total_expected_to_us),
                tx_hash=(str(result) if (not args.dry_run and success) else None),
                vote_sent_at=int(vote_sent_at) if vote_sent_at else None,
                error_text=(None if success else str(result)),
            )

            if success:
                source_label = "dry_run" if args.dry_run else "auto_voter"
                inserted = persist_executed_allocation_for_run(
                    conn=conn,
                    run_id=int(run_id),
                    vote_epoch=int(vote_epoch),
                    allocation=allocation,
                    tx_hash=(str(result) if (not args.dry_run and success) else None),
                    source=f"{source_label}:run_id={int(run_id)}",
                )
                console.print(
                    f"[cyan]Saved {inserted} executed allocation rows for epoch {int(vote_epoch) + int(WEEK)} "
                    f"(strategy=auto_voter_run_{int(run_id)})[/cyan]"
                )

    except BaseException as exc:
        if run_id is not None:
            update_auto_vote_run(
                conn,
                run_id,
                completed_at=int(time.time()),
                status="failed",
                error_text=str(exc),
                vote_sent_at=int(vote_sent_at) if vote_sent_at else None,
            )
        raise
        
    finally:
        conn.close()


if __name__ == "__main__":
    main()
