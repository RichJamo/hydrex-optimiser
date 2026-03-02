#!/usr/bin/env python3
"""
Fetch a live snapshot of rewards + votes at the current block across live gauges,
and optionally print a vote allocation recommendation.

Writes to:
- live_reward_token_samples
- live_gauge_snapshots
"""

import argparse
import os
import sqlite3
import time
from collections import defaultdict
from typing import Dict, List, Set, Tuple

from dotenv import load_dotenv
from multicall import Call, Multicall
from rich.console import Console
from rich.table import Table
from web3 import Web3

from config.settings import DATABASE_PATH, ONE_E18, VOTER_ADDRESS, WEEK
from data.fetchers.fetch_boundary_votes import VOTER_ABI
from data.fetchers.fetch_epoch_bribes_multicall import (
    DEFAULT_PAIRS_CACHE_PATH,
    batch_fetch_reward_data,
    enumerate_bribe_tokens,
    load_discovered_pairs_cache,
    load_pairs_from_bribe_reward_tokens,
    load_gauge_bribe_mapping,
    save_discovered_pairs_cache,
)

load_dotenv()
console = Console()
DEFAULT_MAX_GAUGES_TO_VOTE = int(os.getenv("MAX_GAUGES_TO_VOTE", "10"))


def ensure_live_tables(conn: sqlite3.Connection) -> None:
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS live_reward_token_samples (
            snapshot_ts INTEGER NOT NULL,
            query_block INTEGER NOT NULL,
            vote_epoch INTEGER NOT NULL,
            gauge_address TEXT NOT NULL,
            bribe_contract TEXT NOT NULL,
            reward_token TEXT NOT NULL,
            rewards_raw TEXT NOT NULL,
            token_decimals INTEGER,
            rewards_normalized REAL NOT NULL,
            computed_at INTEGER NOT NULL,
            PRIMARY KEY (snapshot_ts, gauge_address, bribe_contract, reward_token)
        )
        """
    )
    cur.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_live_reward_token_samples_lookup
        ON live_reward_token_samples(snapshot_ts, vote_epoch, gauge_address)
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS live_gauge_snapshots (
            snapshot_ts INTEGER NOT NULL,
            query_block INTEGER NOT NULL,
            vote_epoch INTEGER NOT NULL,
            gauge_address TEXT NOT NULL,
            pool_address TEXT NOT NULL,
            is_alive INTEGER NOT NULL,
            votes_raw REAL NOT NULL,
            rewards_raw_total TEXT NOT NULL,
            rewards_normalized_total REAL NOT NULL,
            computed_at INTEGER NOT NULL,
            PRIMARY KEY (snapshot_ts, gauge_address)
        )
        """
    )
    cur.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_live_gauge_snapshots_lookup
        ON live_gauge_snapshots(snapshot_ts, vote_epoch, rewards_normalized_total DESC)
        """
    )
    conn.commit()


def resolve_vote_epoch(conn: sqlite3.Connection, now_ts: int, forced_vote_epoch: int = 0) -> int:
    if forced_vote_epoch > 0:
        return int(forced_vote_epoch)

    cur = conn.cursor()
    row = cur.execute(
        "SELECT MAX(epoch) FROM epoch_boundaries WHERE epoch <= ?",
        (int(now_ts),),
    ).fetchone()
    if row and row[0]:
        return int(row[0])

    fallback = cur.execute("SELECT MAX(epoch) FROM epoch_boundaries").fetchone()
    if fallback and fallback[0]:
        return int(fallback[0])

    raise ValueError("No epoch boundaries found; cannot infer vote_epoch")


def load_all_gauges(conn: sqlite3.Connection) -> List[Tuple[str, str]]:
    cur = conn.cursor()
    rows = cur.execute(
        """
        SELECT lower(address) AS gauge_address,
               lower(COALESCE(pool, address)) AS pool_address
        FROM gauges
        ORDER BY lower(address)
        """
    ).fetchall()
    return [(str(r[0]), str(r[1])) for r in rows if r and r[0]]


def filter_live_gauges(w3: Web3, voter_address: str, gauges: List[Tuple[str, str]], query_block: int, progress_every: int) -> List[Tuple[str, str]]:
    """
    Filter gauges by liveness using batch Multicall3.
    
    Args:
        w3: Web3 instance
        voter_address: Voter contract address
        gauges: List of (gauge_address, pool_address) tuples
        query_block: Block to query
        progress_every: Progress interval (0 to disable)
    
    Returns:
        List of live (gauge_address, pool_address) tuples
    """
    # Fetch all liveness statuses in batches
    alive_status = batch_fetch_live_gauges_status(
        w3=w3,
        voter_address=voter_address,
        gauges=gauges,
        query_block=query_block,
        batch_size=150,
        progress_every=progress_every
    )
    
    # Filter to live gauges
    live = [
        (gauge_addr, pool_addr) 
        for gauge_addr, pool_addr in gauges 
        if alive_status.get(gauge_addr.lower(), False)
    ]
    
    return live


def load_token_decimals(conn: sqlite3.Connection, tokens: Set[str]) -> Dict[str, int]:
    if not tokens:
        return {}

    cur = conn.cursor()
    placeholders = ",".join(["?"] * len(tokens))
    rows = cur.execute(
        f"""
        SELECT lower(token_address), decimals
        FROM token_metadata
        WHERE lower(token_address) IN ({placeholders})
        """,
        tuple(t.lower() for t in tokens),
    ).fetchall()

    decimals_map: Dict[str, int] = {}
    for token, dec in rows:
        if token and dec is not None:
            decimals_map[str(token).lower()] = int(dec)
    return decimals_map


def batch_fetch_live_gauges_status(
    w3: Web3,
    voter_address: str,
    gauges: List[Tuple[str, str]],
    query_block: int,
    batch_size: int = 150,
    progress_every: int = 50,
) -> Dict[str, bool]:
    """
    Batch fetch isAlive status for gauges using Multicall3.
    
    Returns:
        Dict mapping gauge_address (lower) -> bool (alive status)
    """
    results = {}
    total_gauges = len(gauges)
    
    for batch_start in range(0, len(gauges), batch_size):
        batch_end = min(batch_start + batch_size, len(gauges))
        batch = gauges[batch_start:batch_end]
        
        # Build multicall
        calls = []
        for gauge_addr, _pool_addr in batch:
            call = Call(
                Web3.to_checksum_address(voter_address),
                ['isAlive(address)(bool)', Web3.to_checksum_address(gauge_addr)],
                [(gauge_addr.lower(), lambda success, value: value if success else False)]
            )
            calls.append(call)
        
        try:
            multi = Multicall(
                calls,
                _w3=w3,
                block_id=int(query_block),
                require_success=False
            )
            
            batch_results = multi()
            
            # Parse results
            for gauge_addr, _pool_addr in batch:
                gauge_lower = gauge_addr.lower()
                if gauge_lower in batch_results:
                    results[gauge_lower] = bool(batch_results[gauge_lower])
                else:
                    results[gauge_lower] = False
        except Exception as e:
            # Fallback: mark all in batch as not alive
            console.print(f"[yellow]⚠️  Multicall failed for isAlive batch: {e}[/yellow]")
            for gauge_addr, _pool_addr in batch:
                results[gauge_addr.lower()] = False
        
        if progress_every > 0 and (batch_end % progress_every == 0 or batch_end == total_gauges):
            live_count = sum(1 for v in list(results.values())[:len(results)])
            console.print(f"[dim]isAlive progress: {batch_end}/{total_gauges}, live={live_count}[/dim]")
    
    return results


def batch_fetch_vote_weights(
    w3: Web3,
    voter_address: str,
    live_gauges: List[Tuple[str, str]],
    vote_epoch: int,
    query_block: int,
    batch_size: int = 150,
    progress_every: int = 50,
) -> Dict[str, float]:
    """
    Batch fetch weightsAt for pools using Multicall3.
    
    Returns:
        Dict mapping gauge_address (lower) -> votes_raw (normalized by ONE_E18)
    """
    results = {}
    total_gauges = len(live_gauges)
    
    # Dedupe pools while preserving gauge->pool mapping
    pools_to_gauges: Dict[str, List[str]] = defaultdict(list)
    for gauge_addr, pool_addr in live_gauges:
        pools_to_gauges[pool_addr.lower()].append(gauge_addr.lower())
    
    unique_pools = list(pools_to_gauges.keys())
    
    for batch_start in range(0, len(unique_pools), batch_size):
        batch_end = min(batch_start + batch_size, len(unique_pools))
        batch_pools = unique_pools[batch_start:batch_end]
        
        # Build multicall
        calls = []
        for pool_addr in batch_pools:
            call = Call(
                Web3.to_checksum_address(voter_address),
                ['weightsAt(address,uint256)(uint256)', 
                 Web3.to_checksum_address(pool_addr), int(vote_epoch)],
                [(pool_addr, lambda success, value: value if success else 0)]
            )
            calls.append(call)
        
        try:
            multi = Multicall(
                calls,
                _w3=w3,
                block_id=int(query_block),
                require_success=False
            )
            
            batch_results = multi()
            
            # Parse results and map back to gauges
            for pool_addr in batch_pools:
                pool_lower = pool_addr.lower()
                weight_raw = 0
                
                if pool_lower in batch_results:
                    try:
                        weight_raw = int(batch_results[pool_lower])
                    except (ValueError, TypeError):
                        weight_raw = 0
                
                votes_raw = float(weight_raw) / ONE_E18
                
                # Map result to all gauges using this pool
                for gauge_addr in pools_to_gauges[pool_lower]:
                    results[gauge_addr] = votes_raw
        except Exception as e:
            # Fallback: set all gauges to 0 votes
            console.print(f"[yellow]⚠️  Multicall failed for weightsAt batch: {e}[/yellow]")
            for pool_addr in batch_pools:
                for gauge_addr in pools_to_gauges[pool_addr.lower()]:
                    results[gauge_addr] = 0.0
        
        if progress_every > 0 and (batch_end % progress_every == 0 or batch_end == len(unique_pools)):
            console.print(f"[dim]weightsAt progress: {batch_end}/{len(unique_pools)} unique pools, covering {len(results)} gauges[/dim]")
    
    return results


def pick_pairs(
    conn: sqlite3.Connection,
    w3: Web3,
    candidate_bribes: Set[str],
    query_block: int,
    cache_path: str,
    discover_missing: bool,
) -> List[Tuple[str, str]]:
    pairs: Set[Tuple[str, str]] = set(load_pairs_from_bribe_reward_tokens(conn, candidate_bribes))

    cached = [p for p in load_discovered_pairs_cache(cache_path) if p[0] in candidate_bribes]
    pairs.update(cached)

    if discover_missing or not pairs:
        console.print("[cyan]Enumerating reward tokens on-chain for candidate bribes...[/cyan]")
        discovered = set(pairs)
        for idx, bribe in enumerate(sorted(candidate_bribes), start=1):
            tokens = enumerate_bribe_tokens(w3, bribe, int(query_block))
            for token in tokens:
                discovered.add((bribe, token.lower()))
            if idx % 25 == 0:
                console.print(f"[dim]Discovered pairs progress: {idx}/{len(candidate_bribes)}[/dim]")
        pairs = discovered
        save_discovered_pairs_cache(cache_path, sorted(pairs))

    return sorted(pairs)


def fetch_live_snapshot(
    conn: sqlite3.Connection,
    w3: Web3,
    query_block: int,
    vote_epoch: int,
    max_gauges: int,
    progress_every: int,
    progress_every_batches: int,
    discover_missing_pairs: bool,
    pairs_cache_path: str,
) -> Tuple[int, int, int]:
    ensure_live_tables(conn)
    snapshot_ts = int(time.time())
    now_ts = snapshot_ts
    
    # Timing tracking
    t_start = time.time()

    all_gauges = load_all_gauges(conn)
    if max_gauges > 0:
        all_gauges = all_gauges[:max_gauges]

    console.print(f"[cyan]Loaded gauges: {len(all_gauges)}[/cyan]")
    
    # Time: filter live gauges (isAlive multicall)
    t_before_islive = time.time()
    live_gauges = filter_live_gauges(w3, VOTER_ADDRESS, all_gauges, query_block, progress_every)
    t_islive = time.time() - t_before_islive
    console.print(f"[green]Live gauges at block {query_block}: {len(live_gauges)} (isAlive: {t_islive:.2f}s)[/green]")

    mapping = load_gauge_bribe_mapping(conn)
    bribe_to_gauges: Dict[str, Set[str]] = defaultdict(set)
    for gauge_addr, _pool in live_gauges:
        ib, eb = mapping.get(gauge_addr, (None, None))
        if ib:
            bribe_to_gauges[ib.lower()].add(gauge_addr)
        if eb:
            bribe_to_gauges[eb.lower()].add(gauge_addr)

    candidate_bribes = set(bribe_to_gauges.keys())
    pairs = pick_pairs(
        conn=conn,
        w3=w3,
        candidate_bribes=candidate_bribes,
        query_block=query_block,
        cache_path=pairs_cache_path,
        discover_missing=discover_missing_pairs,
    )

    console.print(f"[cyan]Using (bribe, token) pairs: {len(pairs)}[/cyan]")
    
    # Time: fetch reward data (multicall)
    t_before_rewards = time.time()
    reward_data = batch_fetch_reward_data(
        w3=w3,
        bribe_token_pairs=pairs,
        vote_epoch=int(vote_epoch),
        boundary_block=int(query_block),
        batch_size=200,
        progress_every_batches=progress_every_batches,
    )
    t_rewards = time.time() - t_before_rewards
    console.print(f"[green]Non-zero reward pairs fetched: {len(reward_data)} (rewards: {t_rewards:.2f}s)[/green]")

    token_set = {token for (_bribe, token) in reward_data.keys()}
    token_decimals = load_token_decimals(conn, token_set)

    cur = conn.cursor()
    gauge_rewards_raw: Dict[str, int] = defaultdict(int)
    gauge_rewards_norm: Dict[str, float] = defaultdict(float)

    # Time: insert reward token samples
    t_before_insert_tokens = time.time()
    token_rows_inserted = 0
    for (bribe_addr, token_addr), (rewards_per_epoch, _period_finish, _last_update) in reward_data.items():
        gauges_for_bribe = bribe_to_gauges.get(bribe_addr.lower(), set())
        if not gauges_for_bribe:
            continue

        raw_int = int(rewards_per_epoch * ONE_E18)
        decimals = int(token_decimals.get(token_addr.lower(), 18))
        norm = float(raw_int) / float(10 ** decimals)

        for gauge_addr in gauges_for_bribe:
            cur.execute(
                """
                INSERT OR REPLACE INTO live_reward_token_samples
                (snapshot_ts, query_block, vote_epoch, gauge_address, bribe_contract, reward_token,
                 rewards_raw, token_decimals, rewards_normalized, computed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    snapshot_ts,
                    int(query_block),
                    int(vote_epoch),
                    gauge_addr.lower(),
                    bribe_addr.lower(),
                    token_addr.lower(),
                    str(raw_int),
                    decimals,
                    norm,
                    now_ts,
                ),
            )
            token_rows_inserted += 1
            gauge_rewards_raw[gauge_addr.lower()] += raw_int
            gauge_rewards_norm[gauge_addr.lower()] += norm

    # Fetch vote weights using batch Multicall3
    t_before_weights = time.time()
    votes_weights = batch_fetch_vote_weights(
        w3=w3,
        voter_address=VOTER_ADDRESS,
        live_gauges=live_gauges,
        vote_epoch=int(vote_epoch),
        query_block=int(query_block),
        batch_size=150,
        progress_every=progress_every,
    )
    t_weights = time.time() - t_before_weights
    console.print(f"[green]Vote weights fetched (weightsAt: {t_weights:.2f}s)[/green]")

    # Time: insert gauge snapshots
    t_before_insert_gauges = time.time()
    gauge_rows_inserted = 0
    for idx, (gauge_addr, pool_addr) in enumerate(live_gauges, start=1):
        votes_raw = votes_weights.get(gauge_addr.lower(), 0.0)

        cur.execute(
            """
            INSERT OR REPLACE INTO live_gauge_snapshots
            (snapshot_ts, query_block, vote_epoch, gauge_address, pool_address, is_alive,
             votes_raw, rewards_raw_total, rewards_normalized_total, computed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                snapshot_ts,
                int(query_block),
                int(vote_epoch),
                gauge_addr.lower(),
                pool_addr.lower(),
                1,
                float(votes_raw),
                str(int(gauge_rewards_raw.get(gauge_addr.lower(), 0))),
                float(gauge_rewards_norm.get(gauge_addr.lower(), 0.0)),
                now_ts,
            ),
        )
        gauge_rows_inserted += 1

        if progress_every > 0 and (idx % progress_every == 0 or idx == len(live_gauges)):
            console.print(f"[dim]Inserted gauge snapshots {idx}/{len(live_gauges)}[/dim]")

    conn.commit()
    
    t_insert_gauges = time.time() - t_before_insert_gauges
    t_insert_tokens = t_before_weights - t_before_insert_tokens
    t_total = time.time() - t_start
    
    console.print(
        f"[cyan]Fetch timing: isAlive={t_islive:.2f}s, rewards={t_rewards:.2f}s, "
        f"weightsAt={t_weights:.2f}s, insert_tokens={t_insert_tokens:.2f}s, "
        f"insert_gauges={t_insert_gauges:.2f}s, total={t_total:.2f}s[/cyan]"
    )
    
    return snapshot_ts, token_rows_inserted, gauge_rows_inserted


def print_allocation(conn: sqlite3.Connection, snapshot_ts: int, your_voting_power: int, top_k: int) -> None:
    cur = conn.cursor()
    rows = cur.execute(
        """
        SELECT gauge_address, pool_address, votes_raw, rewards_normalized_total
        FROM live_gauge_snapshots
        WHERE snapshot_ts = ? AND is_alive = 1 AND rewards_normalized_total > 0
        """,
        (int(snapshot_ts),),
    ).fetchall()

    if not rows:
        console.print("[red]No live gauges with positive rewards found for allocation.[/red]")
        return

    votes_per_pool = float(your_voting_power) / float(max(1, top_k))

    scored = []
    for gauge_addr, pool_addr, votes_raw, rewards_norm in rows:
        base_votes = float(votes_raw or 0.0)
        rewards_total = float(rewards_norm or 0.0)
        adjusted_roi = rewards_total / max(1e-12, (base_votes + votes_per_pool))
        expected_return = votes_per_pool * adjusted_roi
        scored.append((gauge_addr, pool_addr, base_votes, rewards_total, adjusted_roi, expected_return))

    scored.sort(key=lambda x: x[4], reverse=True)
    selected = scored[:top_k]

    table = Table(title=f"Live Allocation Recommendation (snapshot_ts={snapshot_ts})")
    table.add_column("Rank", justify="right")
    table.add_column("Gauge")
    table.add_column("Base Votes", justify="right")
    table.add_column("Rewards (norm)", justify="right")
    table.add_column("Adj ROI", justify="right")
    table.add_column("Vote Allocation", justify="right")
    table.add_column("Expected Return", justify="right")

    total_expected = 0.0
    for i, row in enumerate(selected, start=1):
        gauge_addr, _pool_addr, base_votes, rewards_total, adjusted_roi, expected_return = row
        total_expected += expected_return
        table.add_row(
            str(i),
            gauge_addr,
            f"{base_votes:,.2f}",
            f"{rewards_total:,.4f}",
            f"{adjusted_roi:,.8f}",
            f"{votes_per_pool:,.0f}",
            f"{expected_return:,.4f}",
        )

    console.print(table)
    console.print(f"[green]Total expected return (normalized units): {total_expected:,.4f}[/green]")

    console.print("\n[bold]Copyable allocation (equal split):[/bold]")
    for gauge_addr, _pool_addr, *_rest in selected:
        console.print(f"{gauge_addr},{int(votes_per_pool)}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Fetch live rewards + votes snapshot and produce allocation")
    parser.add_argument("--db-path", default=DATABASE_PATH, help="Database path")
    parser.add_argument("--rpc", default=os.getenv("RPC_URL", ""), help="RPC URL")
    parser.add_argument("--query-block", type=int, default=0, help="Block to query (default: latest)")
    parser.add_argument("--vote-epoch", type=int, default=0, help="Vote epoch to query (default: infer from epoch_boundaries)")
    parser.add_argument("--max-gauges", type=int, default=0, help="Optional cap on gauges (0 = all)")
    parser.add_argument("--progress-every", type=int, default=50, help="Progress interval")
    parser.add_argument("--progress-every-batches", type=int, default=1, help="Reward multicall progress interval")
    parser.add_argument("--pairs-cache-path", type=str, default=DEFAULT_PAIRS_CACHE_PATH, help="Discovered pairs cache path")
    parser.add_argument("--discover-missing-pairs", action="store_true", help="On-chain enumerate reward tokens for bribes")
    parser.add_argument("--top-k", type=int, default=int(DEFAULT_MAX_GAUGES_TO_VOTE), help="Pools to allocate to")
    parser.add_argument("--your-voting-power", type=int, default=int(os.getenv("YOUR_VOTING_POWER", "0")), help="Your total voting power")
    args = parser.parse_args()

    if not args.rpc:
        console.print("[red]RPC_URL missing[/red]")
        return

    if args.your_voting_power <= 0:
        console.print("[red]YOUR_VOTING_POWER must be > 0[/red]")
        return

    w3 = Web3(Web3.HTTPProvider(args.rpc))
    if not w3.is_connected():
        console.print("[red]Failed to connect to RPC[/red]")
        return

    conn = sqlite3.connect(args.db_path)

    latest_block = int(w3.eth.block_number)
    query_block = int(args.query_block) if args.query_block > 0 else latest_block
    now_ts = int(time.time())
    vote_epoch = resolve_vote_epoch(conn, now_ts=now_ts, forced_vote_epoch=int(args.vote_epoch))

    next_boundary_est = int(vote_epoch + WEEK)
    seconds_to_boundary = int(next_boundary_est - now_ts)

    console.print(f"[cyan]query_block={query_block} latest_block={latest_block}[/cyan]")
    console.print(f"[cyan]vote_epoch={vote_epoch} ({time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime(vote_epoch))} UTC)[/cyan]")
    console.print(
        f"[cyan]next_boundary_est={next_boundary_est} ({time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime(next_boundary_est))} UTC), "
        f"eta={seconds_to_boundary//3600}h {(seconds_to_boundary%3600)//60}m[/cyan]"
    )

    snapshot_ts, token_rows, gauge_rows = fetch_live_snapshot(
        conn=conn,
        w3=w3,
        query_block=query_block,
        vote_epoch=vote_epoch,
        max_gauges=int(args.max_gauges),
        progress_every=int(args.progress_every),
        progress_every_batches=int(args.progress_every_batches),
        discover_missing_pairs=bool(args.discover_missing_pairs),
        pairs_cache_path=args.pairs_cache_path,
    )

    console.print(
        f"[green]Live snapshot saved: snapshot_ts={snapshot_ts}, token_rows={token_rows}, gauge_rows={gauge_rows}[/green]"
    )

    print_allocation(
        conn=conn,
        snapshot_ts=snapshot_ts,
        your_voting_power=int(args.your_voting_power),
        top_k=max(1, int(args.top_k)),
    )

    conn.close()


if __name__ == "__main__":
    main()
