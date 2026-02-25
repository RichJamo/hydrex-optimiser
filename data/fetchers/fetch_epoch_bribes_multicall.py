#!/usr/bin/env python3
"""
Fetch boundary reward data using Multicall3 for efficiency.

Uses whitelist from existing data and batches RPC calls.
Expected speedup: 20-50x faster than sequential calls.

Usage:
  python -m data.fetchers.fetch_epoch_bribes_multicall
"""

import argparse
import json
import os
import sqlite3
import time
from collections import defaultdict
from typing import Dict, List, Set, Tuple

from dotenv import load_dotenv
from multicall import Call, Multicall
from rich.console import Console
from web3 import Web3

from config.settings import DATABASE_PATH, ONE_E18, WEEK

load_dotenv()
console = Console()

RPC_URL = os.getenv("RPC_URL", "https://mainnet.base.org")
MULTICALL3_ADDRESS = "0xcA11bde05977b3631167028862bE2a173976CA11"  # Universal address
DEFAULT_PAIRS_CACHE_PATH = "data/preboundary_cache/bribe_token_pairs.json"


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
]


def enumerate_bribe_tokens(w3: Web3, bribe_addr: str, block: int) -> List[str]:
    """Enumerate all approved reward tokens from a bribe contract."""
    try:
        bribe = w3.eth.contract(address=Web3.to_checksum_address(bribe_addr), abi=BRIBE_ABI)
        length = bribe.functions.rewardsListLength().call(block_identifier=block)
        
        tokens = []
        for i in range(min(length, 500)):  # safety limit
            try:
                token = bribe.functions.rewardTokens(i).call(block_identifier=block)
                if token and token != "0x" + "0" * 40:
                    is_approved = bribe.functions.isRewardToken(token).call(block_identifier=block)
                    if is_approved:
                        tokens.append(Web3.to_checksum_address(token).lower())
            except:
                pass
        
        return tokens
    except:
        return []


def extract_whitelist(conn: sqlite3.Connection) -> Set[Tuple[str, str]]:
    """Extract (bribe, token) pairs with non-zero rewards from existing data."""
    cur = conn.cursor()
    whitelist = set()
    for row in cur.execute(
        """
        SELECT DISTINCT LOWER(bribe_contract), LOWER(reward_token) 
        FROM boundary_reward_snapshots 
        WHERE active_only=1 
          AND bribe_contract IS NOT NULL 
          AND reward_token IS NOT NULL 
          AND CAST(rewards_raw AS REAL) > 0
        """
    ).fetchall():
        whitelist.add(row)
    
    if not whitelist:
        console.print("[yellow]⚠️  No whitelist found, will enumerate tokens from contracts[/yellow]")
    else:
        console.print(f"[green]✓ Whitelist extracted: {len(whitelist)} (bribe, token) pairs[/green]")
    
    return whitelist


def load_gauge_bribe_mapping(conn: sqlite3.Connection) -> Dict[str, Tuple[str, str]]:
    """Load gauge→(internal_bribe, external_bribe) mapping."""
    cur = conn.cursor()
    try:
        cur.execute("SELECT gauge_address, internal_bribe, external_bribe FROM gauge_bribe_mapping")
        return {g: (ib, eb) for g, ib, eb in cur.fetchall()}
    except sqlite3.OperationalError:
        console.print("[red]ERROR: gauge_bribe_mapping table not found[/red]")
        raise


def load_epoch_boundary(conn: sqlite3.Connection, epoch: int) -> Tuple[int, int]:
    """Load (boundary_block, vote_epoch) from epoch_boundaries."""
    cur = conn.cursor()
    row = cur.execute(
        "SELECT boundary_block, vote_epoch FROM epoch_boundaries WHERE epoch = ?",
        (int(epoch),)
    ).fetchone()
    
    if not row:
        raise ValueError(f"No boundary found for epoch {epoch}")
    
    return int(row[0]), int(row[1])


def ensure_boundary_reward_samples(conn: sqlite3.Connection) -> None:
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS boundary_reward_samples (
            epoch INTEGER NOT NULL,
            vote_epoch INTEGER NOT NULL,
            active_only INTEGER NOT NULL,
            boundary_block INTEGER NOT NULL,
            query_block INTEGER NOT NULL,
            blocks_before_boundary INTEGER NOT NULL,
            gauge_address TEXT NOT NULL,
            bribe_contract TEXT NOT NULL,
            reward_token TEXT NOT NULL,
            rewards_raw TEXT NOT NULL,
            token_decimals INTEGER,
            usd_price REAL,
            total_usd REAL NOT NULL,
            computed_at INTEGER NOT NULL,
            PRIMARY KEY (
                epoch, vote_epoch, active_only, blocks_before_boundary,
                bribe_contract, reward_token, gauge_address
            )
        )
        """
    )
    cur.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_boundary_reward_samples_lookup
        ON boundary_reward_samples(epoch, vote_epoch, active_only, blocks_before_boundary)
        """
    )
    conn.commit()


def parse_offset_blocks(offsets_raw: str) -> List[int]:
    if not offsets_raw:
        return []
    offsets = sorted({int(x.strip()) for x in offsets_raw.split(",") if x.strip()})
    return [x for x in offsets if x > 0]


def load_pairs_from_bribe_reward_tokens(
    conn: sqlite3.Connection,
    candidate_bribes: Set[str],
) -> List[Tuple[str, str]]:
    """Load cached (bribe, token) pairs from bribe_reward_tokens table."""
    if not candidate_bribes:
        return []

    placeholders = ",".join(["?"] * len(candidate_bribes))
    query = f"""
        SELECT LOWER(bribe_contract), LOWER(reward_token)
        FROM bribe_reward_tokens
        WHERE is_reward_token = 1
          AND LOWER(bribe_contract) IN ({placeholders})
    """

    cur = conn.cursor()
    try:
        rows = cur.execute(query, tuple(candidate_bribes)).fetchall()
    except sqlite3.OperationalError:
        return []

    return list({(b, t) for b, t in rows if b and t})


def load_discovered_pairs_cache(cache_path: str) -> List[Tuple[str, str]]:
    """Load discovered (bribe, token) pairs cache from disk."""
    if not cache_path or not os.path.exists(cache_path):
        return []

    try:
        with open(cache_path, "r", encoding="utf-8") as f:
            payload = json.load(f)
        raw_pairs = payload.get("pairs", []) if isinstance(payload, dict) else []
        pairs = []
        for item in raw_pairs:
            if isinstance(item, (list, tuple)) and len(item) == 2:
                bribe, token = item
                if bribe and token:
                    pairs.append((str(bribe).lower(), str(token).lower()))
        return pairs
    except Exception as e:
        console.print(f"[yellow]⚠️  Failed to load pairs cache ({cache_path}): {e}[/yellow]")
        return []


def save_discovered_pairs_cache(cache_path: str, pairs: List[Tuple[str, str]]) -> None:
    """Persist discovered (bribe, token) pairs cache to disk."""
    if not cache_path:
        return

    try:
        os.makedirs(os.path.dirname(cache_path), exist_ok=True)
        payload = {
            "generated_at": int(time.time()),
            "count": len(pairs),
            "pairs": [[b, t] for b, t in pairs],
        }
        with open(cache_path, "w", encoding="utf-8") as f:
            json.dump(payload, f)
        console.print(f"[green]✓ Saved pairs cache: {len(pairs)} pairs -> {cache_path}[/green]")
    except Exception as e:
        console.print(f"[yellow]⚠️  Failed to save pairs cache ({cache_path}): {e}[/yellow]")


def batch_fetch_reward_data(
    w3: Web3,
    bribe_token_pairs: List[Tuple[str, str]],
    vote_epoch: int,
    boundary_block: int,
    batch_size: int = 200,
    progress_every_batches: int = 1,
) -> Dict[Tuple[str, str], Tuple[float, int, int]]:
    """
    Batch fetch rewardData using Multicall3.
    
    Returns:
        Dict mapping (bribe, token) -> (rewards_per_epoch, period_finish, last_update)
    """
    results = {}
    total_batches = (len(bribe_token_pairs) + batch_size - 1) // batch_size
    successful_batches = 0
    missing_key_count = 0
    bool_false_count = 0
    decode_fail_count = 0
    zero_reward_count = 0
    
    for batch_start in range(0, len(bribe_token_pairs), batch_size):
        batch_index = (batch_start // batch_size) + 1
        batch = bribe_token_pairs[batch_start:batch_start + batch_size]
        if progress_every_batches > 0 and (
            batch_index == 1
            or batch_index == total_batches
            or batch_index % progress_every_batches == 0
        ):
            console.print(
                f"    [dim]Batch {batch_index}/{total_batches} (size={len(batch)}), "
                f"current non-zero rewards={len(results)}[/dim]"
            )
        
        # Build multicall
        calls = []
        for bribe_addr, token_addr in batch:
            call = Call(
                Web3.to_checksum_address(bribe_addr),
                ['rewardData(address,uint256)((uint256,uint256,uint256))',
                 Web3.to_checksum_address(token_addr), vote_epoch],
                [(f"{bribe_addr}_{token_addr}", lambda success, value: value if success else None)]
            )
            calls.append(call)
        
        try:
            multi = Multicall(
                calls,
                _w3=w3,
                block_id=boundary_block,
                require_success=False
            )
            
            batch_results = multi()
            successful_batches += 1
            
            # Parse results
            for bribe_addr, token_addr in batch:
                key = f"{bribe_addr}_{token_addr}"
                if key not in batch_results:
                    missing_key_count += 1
                    continue

                data = batch_results[key]
                decoded = None

                if isinstance(data, bool):
                    if not data:
                        bool_false_count += 1
                    continue

                if isinstance(data, (list, tuple)) and len(data) == 2 and isinstance(data[0], bool):
                    success, payload = data
                    if not success:
                        bool_false_count += 1
                        continue
                    decoded = payload
                elif isinstance(data, (list, tuple)):
                    decoded = data

                if isinstance(decoded, (list, tuple)) and len(decoded) == 1 and isinstance(decoded[0], (list, tuple)):
                    decoded = decoded[0]

                if isinstance(decoded, (list, tuple)) and len(decoded) == 3:
                    period_finish, rewards_per_epoch, last_update = decoded
                    if rewards_per_epoch and int(rewards_per_epoch) > 0:
                        results[(bribe_addr, token_addr)] = (
                            float(rewards_per_epoch) / ONE_E18,
                            int(period_finish),
                            int(last_update)
                        )
                    else:
                        zero_reward_count += 1
                else:
                    decode_fail_count += 1
        
        except Exception as e:
            console.print(f"[yellow]Batch {batch_index}/{total_batches} error: {e}[/yellow]")
            continue

    console.print(
        f"    [dim]Completed multicall batches: {successful_batches}/{total_batches}, "
        f"non-zero rewards found={len(results)}[/dim]"
    )
    console.print(
        f"    [dim]Decode stats: missing_keys={missing_key_count}, failed_calls={bool_false_count}, "
        f"decode_failures={decode_fail_count}, zero_rewards={zero_reward_count}[/dim]"
    )
    
    return results


def fetch_epoch_rewards_multicall(
    conn: sqlite3.Connection,
    w3: Web3,
    epoch: int,
    whitelist: Set[Tuple[str, str]],
    discovered_pairs: List[Tuple[str, str]],
    mapping: Dict[str, Tuple[str, str]],
    progress_every_batches: int,
    blocks_before_boundary: int = 0,
) -> int:
    """Fetch all rewards for an epoch using multicall."""
    
    # Load boundary
    try:
        boundary_block, vote_epoch = load_epoch_boundary(conn, epoch)
    except ValueError as e:
        console.print(f"[red]{e}[/red]")
        return 0
    
    console.print(f"[cyan]Epoch {epoch}:[/cyan]")
    query_block = int(boundary_block - blocks_before_boundary)
    console.print(
        f"  vote_epoch={vote_epoch}, boundary_block={boundary_block}, "
        f"offset={blocks_before_boundary}, query_block={query_block}"
    )
    
    # Clear old data
    cur = conn.cursor()
    if blocks_before_boundary > 0:
        cur.execute(
            "DELETE FROM boundary_reward_samples WHERE epoch = ? AND active_only = 1 AND blocks_before_boundary = ?",
            (epoch, int(blocks_before_boundary)),
        )
    else:
        cur.execute(
            "DELETE FROM boundary_reward_snapshots WHERE epoch = ? AND active_only = 1",
            (epoch,),
        )
    conn.commit()
    
    # Build (bribe, token) pairs
    if whitelist:
        bribe_token_pairs = list(whitelist)
    else:
        bribe_token_pairs = discovered_pairs
    
    if not bribe_token_pairs:
        console.print("  [yellow]⚠️  No bribe/token pairs to fetch[/yellow]")
        return 0
    
    # Batch fetch
    console.print(f"  Fetching {len(bribe_token_pairs)} (bribe, token) pairs via multicall...")
    start = time.time()
    
    reward_data = batch_fetch_reward_data(
        w3,
        bribe_token_pairs,
        vote_epoch,
        query_block,
        batch_size=200
        ,progress_every_batches=progress_every_batches
    )
    
    elapsed = time.time() - start
    console.print(f"  ✓ Fetched {len(reward_data)} non-zero rewards in {elapsed:.1f}s")
    
    # Insert into DB
    rows_inserted = 0
    now_ts = int(time.time())
    
    for (bribe_addr, token_addr), (rewards_per_epoch, period_finish, last_update) in reward_data.items():
        # Find gauges using this bribe
        gauges_for_bribe = [
            g for g, (ib, eb) in mapping.items()
            if (ib and ib.lower() == bribe_addr.lower()) or (eb and eb.lower() == bribe_addr.lower())
        ]
        
        for gauge in gauges_for_bribe:
            if blocks_before_boundary > 0:
                cur.execute(
                    """
                    INSERT OR REPLACE INTO boundary_reward_samples 
                    (epoch, vote_epoch, active_only, boundary_block, query_block, blocks_before_boundary,
                     gauge_address, bribe_contract, reward_token, rewards_raw, computed_at, total_usd)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        epoch,
                        vote_epoch,
                        1,
                        boundary_block,
                        query_block,
                        int(blocks_before_boundary),
                        gauge.lower(),
                        bribe_addr.lower(),
                        token_addr.lower(),
                        str(int(rewards_per_epoch * ONE_E18)),
                        now_ts,
                        0.0,
                    ),
                )
            else:
                cur.execute(
                    """
                    INSERT OR REPLACE INTO boundary_reward_snapshots 
                    (epoch, vote_epoch, active_only, boundary_block, gauge_address, 
                     bribe_contract, reward_token, rewards_raw, computed_at, total_usd)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        epoch,
                        vote_epoch,
                        1,
                        boundary_block,
                        gauge.lower(),
                        bribe_addr.lower(),
                        token_addr.lower(),
                        str(int(rewards_per_epoch * ONE_E18)),
                        now_ts,
                        0.0,
                    ),
                )
            rows_inserted += 1
    
    conn.commit()
    console.print(f"  ✓ Inserted {rows_inserted} rows")
    
    return rows_inserted


def main() -> None:
    parser = argparse.ArgumentParser(description="Fetch boundary rewards using multicall")
    parser.add_argument("--db-path", default=DATABASE_PATH, help="Database path")
    parser.add_argument("--all-epochs", action="store_true", help="Fetch all epochs from epoch_boundaries")
    parser.add_argument("--epochs", type=str, help="Comma-separated list of epochs")
    parser.add_argument(
        "--pairs-cache-path",
        type=str,
        default=DEFAULT_PAIRS_CACHE_PATH,
        help=f"Path for persistent discovered (bribe, token) cache (default: {DEFAULT_PAIRS_CACHE_PATH})",
    )
    parser.add_argument(
        "--refresh-pairs-cache",
        action="store_true",
        help="Ignore existing discovered pairs cache and rebuild from chain",
    )
    parser.add_argument(
        "--progress-every-batches",
        type=int,
        default=1,
        help="Log multicall progress every N batches (default: 1)",
    )
    parser.add_argument(
        "--progress-every-bribes",
        type=int,
        default=25,
        help="Log token discovery progress every N bribes (default: 25)",
    )
    parser.add_argument("--single-bribe", type=str, help="Run only one bribe contract (for debugging)")
    parser.add_argument("--single-token", type=str, help="Run only one reward token with --single-bribe")
    parser.add_argument("--ignore-whitelist", action="store_true", help="Ignore whitelist extracted from boundary_reward_snapshots")
    parser.add_argument(
        "--offset-blocks",
        type=str,
        default="",
        help="Comma-separated block offsets before boundary to sample (e.g. 1,20). Empty keeps boundary table behavior.",
    )
    
    args = parser.parse_args()
    
    conn = sqlite3.connect(args.db_path)
    w3 = Web3(Web3.HTTPProvider(RPC_URL))
    
    if not w3.is_connected():
        console.print("[red]❌ Failed to connect to RPC[/red]")
        return
    
    console.print(f"[green]✓ Connected to RPC: {RPC_URL[:50]}...[/green]")

    offsets = parse_offset_blocks(args.offset_blocks)
    if offsets:
        ensure_boundary_reward_samples(conn)
        console.print(f"[cyan]Offset sampling mode enabled for offsets: {offsets}[/cyan]")
    
    # Extract whitelist
    whitelist = extract_whitelist(conn)
    if args.ignore_whitelist:
        whitelist = set()
        console.print("[yellow]Whitelist ignored via --ignore-whitelist[/yellow]")
    
    # Load mapping
    console.print("[cyan]Loading gauge→bribe mapping...[/cyan]")
    mapping = load_gauge_bribe_mapping(conn)
    console.print(f"[green]✓ Loaded {len(mapping)} gauge mappings[/green]")
    
    # Determine epochs to fetch
    if args.all_epochs:
        cur = conn.cursor()
        epochs = [row[0] for row in cur.execute("SELECT epoch FROM epoch_boundaries ORDER BY epoch")]
    elif args.epochs:
        epochs = [int(e.strip()) for e in args.epochs.split(',')]
    else:
        # Default: all epochs from epoch_boundaries
        cur = conn.cursor()
        epochs = [row[0] for row in cur.execute("SELECT epoch FROM epoch_boundaries ORDER BY epoch")]
    
    console.print(f"\n[bold cyan]Fetching rewards for {len(epochs)} epochs[/bold cyan]\n")

    discovered_pairs: List[Tuple[str, str]] = []

    if args.single_bribe and args.single_token:
        discovered_pairs = [(args.single_bribe.lower(), args.single_token.lower())]
        whitelist = set()
        console.print(
            f"[yellow]Single-pair override enabled: bribe={args.single_bribe.lower()} token={args.single_token.lower()}[/yellow]"
        )

    if not whitelist and not (args.single_bribe and args.single_token):
        unique_bribes = set()
        for _, (ib, eb) in mapping.items():
            if ib:
                unique_bribes.add(ib.lower())
            if eb:
                unique_bribes.add(eb.lower())

        discovered_pairs = load_pairs_from_bribe_reward_tokens(conn, unique_bribes)
        if discovered_pairs:
            console.print(
                f"[green]✓ Loaded {len(discovered_pairs)} pairs from bribe_reward_tokens table[/green]"
            )

        if not discovered_pairs and not args.refresh_pairs_cache:
            discovered_pairs = load_discovered_pairs_cache(args.pairs_cache_path)
            if discovered_pairs:
                console.print(
                    f"[green]✓ Loaded pairs cache: {len(discovered_pairs)} pairs from {args.pairs_cache_path}[/green]"
                )

        if not discovered_pairs:
            latest_epoch = max(epochs)
            latest_boundary_block, _ = load_epoch_boundary(conn, latest_epoch)
            console.print("[yellow]No whitelist/table/cache, enumerating tokens from contracts once...[/yellow]")

            pairs_set = set()
            total_bribes = len(unique_bribes)
            for idx, bribe in enumerate(unique_bribes, 1):
                tokens = enumerate_bribe_tokens(w3, bribe, latest_boundary_block)
                for token in tokens:
                    pairs_set.add((bribe, token))
                if args.progress_every_bribes > 0 and (
                    idx == 1
                    or idx == total_bribes
                    or idx % args.progress_every_bribes == 0
                ):
                    console.print(
                        f"  [dim]Token discovery {idx}/{total_bribes} bribes, "
                        f"discovered pairs={len(pairs_set)}[/dim]"
                    )

            discovered_pairs = list(pairs_set)
            console.print(f"[green]✓ Discovered {len(discovered_pairs)} (bribe, token) pairs[/green]")
            if discovered_pairs:
                save_discovered_pairs_cache(args.pairs_cache_path, discovered_pairs)
    
    total_rows = 0
    start_time = time.time()
    
    for idx, epoch in enumerate(epochs, 1):
        console.print(f"[bold]Epoch {idx}/{len(epochs)}:[/bold]")
        offsets_to_run = offsets if offsets else [0]
        for block_offset in offsets_to_run:
            rows = fetch_epoch_rewards_multicall(
                conn,
                w3,
                epoch,
                whitelist,
                discovered_pairs,
                mapping,
                progress_every_batches=args.progress_every_batches,
                blocks_before_boundary=block_offset,
            )
            total_rows += rows
        console.print()
    
    elapsed = time.time() - start_time
    
    console.print(f"[bold green]✅ Complete![/bold green]")
    console.print(f"   Total rows: {total_rows}")
    console.print(f"   Total time: {elapsed:.1f}s ({elapsed/len(epochs):.1f}s per epoch)")
    console.print(f"   Distinct epochs: {len(epochs)}")
    
    conn.close()


if __name__ == "__main__":
    main()
