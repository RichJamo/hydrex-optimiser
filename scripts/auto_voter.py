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

Note: Vote proportions are relative weights (e.g., [10000, 10000, ...] for equal allocation).
The contract normalizes them - they don't need to sum to voting power.
VOTE_DELAY is currently 0, so you can re-vote multiple times per epoch (not twice in same block).
"""

import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
import shutil
from typing import Dict, List, Optional, Tuple, Union

from dotenv import load_dotenv
from eth_utils import keccak
from eth_account import Account
from rich.console import Console
from web3 import Web3
from web3.exceptions import ContractLogicError

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config.settings import DATABASE_PATH, VOTER_ADDRESS, ONE_E18, WEEK

load_dotenv()

# Load MY_ESCROW_ADDRESS from environment (escrow account)
MY_ESCROW_ADDRESS = os.getenv("MY_ESCROW_ADDRESS", "").lower()

console = Console()

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
    """Load wallet from private key source: raw key, file path, or 1Password op:// reference."""
    if private_key_source.startswith("op://"):
        if shutil.which("op") is None:
            raise RuntimeError("1Password CLI 'op' not found in PATH")
        result = subprocess.run(["op", "read", private_key_source], capture_output=True, text=True)
        if result.returncode != 0:
            stderr = (result.stderr or "").strip()
            raise RuntimeError(f"Failed to read secret from 1Password: {stderr or 'unknown error'}")
        private_key = (result.stdout or "").strip()
        if not private_key:
            raise RuntimeError("1Password secret value is empty")
    elif os.path.isfile(private_key_source):
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
        resolve_vote_epoch,
    )
    
    now_ts = int(time.time())
    vote_epoch = resolve_vote_epoch(conn, now_ts=now_ts, forced_vote_epoch=0)
    
    if query_block <= 0:
        query_block = int(w3.eth.block_number)
    
    console.print(f"[cyan]Fetching fresh snapshot at block {query_block}, vote_epoch={vote_epoch}...[/cyan]")
    
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
    
    console.print(f"[green]✓ Fresh snapshot saved: snapshot_ts={snapshot_ts}, gauge_rows={gauge_rows}[/green]")
    return snapshot_ts, vote_epoch, query_block


def calculate_gauge_rewards_usd(conn: sqlite3.Connection, snapshot_ts: int, gauge_address: str) -> float:
    """
    Calculate total USD value of rewards for a gauge by fetching all reward tokens,
    looking up their prices, and summing token_amount * price.
    """
    cur = conn.cursor()
    
    # Get all reward tokens for this gauge
    reward_tokens = cur.execute(
        """
        SELECT reward_token, rewards_normalized
        FROM live_reward_token_samples
        WHERE snapshot_ts = ? AND LOWER(gauge_address) = LOWER(?)
        """,
        (snapshot_ts, gauge_address),
    ).fetchall()
    
    if not reward_tokens:
        return 0.0
    
    total_usd = 0.0
    for token_addr, token_amount in reward_tokens:
        # Look up price
        price_row = cur.execute(
            """
            SELECT usd_price
            FROM token_prices
            WHERE LOWER(token_address) = LOWER(?)
            """,
            (token_addr,),
        ).fetchone()
        
        if price_row and price_row[0]:
            price = float(price_row[0])
            total_usd += float(token_amount) * price
        # If no price, skip this token (contributes $0)
    
    return total_usd


def calculate_optimal_allocation(
    conn: sqlite3.Connection,
    snapshot_ts: int,
    your_voting_power: int,
    top_k: int,
) -> List[Tuple[str, str, int, float, float, float]]:
    """
    Calculate optimal allocation using marginal ROI.
    Returns list of (gauge_addr, pool_addr, vote_amount, current_votes, current_rewards, expected_to_us).
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
        return []
    
    # Equal allocation
    votes_per_pool = int(your_voting_power / max(1, top_k))
    
    # Calculate marginal ROI with actual USD values
    scored = []
    for gauge_addr, pool_addr, votes_raw, _rewards_norm in rows:
        base_votes = float(votes_raw or 0.0)
        
        # Calculate actual USD value of rewards
        rewards_usd = calculate_gauge_rewards_usd(conn, snapshot_ts, gauge_addr)
        
        adjusted_roi = rewards_usd / max(1.0, (base_votes + votes_per_pool))
        scored.append((gauge_addr, pool_addr, base_votes, rewards_usd, adjusted_roi))
    
    # Sort by adjusted ROI
    scored.sort(key=lambda x: x[4], reverse=True)
    
    # Return top K with vote amounts and current metrics
    selected = []
    for gauge, pool, base_votes, rewards_usd, _roi in scored[:top_k]:
        expected_to_us = float(rewards_usd) * float(votes_per_pool) / max(1.0, (float(base_votes) + float(votes_per_pool)))
        selected.append((gauge, pool, votes_per_pool, base_votes, rewards_usd, expected_to_us))

    return selected


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
) -> Tuple[bool, str]:
    """
    Build, sign, and send vote transaction.
    Transaction is signed by wallet and sent to PartnerEscrow (MY_ESCROW_ADDRESS).
    Returns (success, tx_hash_or_error).
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
        return False, err
    
    console.print(f"[green]✓ Gas price acceptable (<= {max_gas_price_gwei} Gwei)[/green]")
    
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
            return False, "Simulation failed"
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
                        "to": Web3.to_checksum_address(partner_escrow_address),
                        "data": vote_contract.encode_abi("vote", args=[pool_addresses, vote_proportions]),
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
        
        tx_cost_eth = (tx["gas"] * current_gas_price) / 1e18
        console.print(f"[cyan]Estimated transaction cost: {tx_cost_eth:.6f} ETH[/cyan]")
        
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
            return True, "DRY_RUN_SUCCESS"
        
        if not wallet:
            return False, "No wallet provided for actual transaction"
        
        # Sign transaction
        console.print("[cyan]Signing transaction...[/cyan]")
        signed_tx = wallet.sign_transaction(tx)
        
        # Send transaction
        console.print("[cyan]Sending transaction...[/cyan]")
        tx_hash = w3.eth.send_raw_transaction(signed_tx.rawTransaction)
        tx_hash_hex = tx_hash.hex()
        
        console.print(f"[green]✓ Transaction sent: {tx_hash_hex}[/green]")
        console.print("[cyan]Waiting for transaction receipt...[/cyan]")
        
        # Wait for receipt (timeout after 5 minutes)
        receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=300)
        
        if receipt["status"] == 1:
            console.print(f"[bold green]✓ TRANSACTION SUCCESSFUL[/bold green]")
            console.print(f"  Block: {receipt['blockNumber']}")
            console.print(f"  Gas Used: {receipt['gasUsed']:,}")
            console.print(f"  Tx Hash: {tx_hash_hex}")
            return True, tx_hash_hex
        else:
            console.print(f"[bold red]✗ TRANSACTION FAILED[/bold red]")
            console.print(f"  Tx Hash: {tx_hash_hex}")
            return False, f"Transaction reverted: {tx_hash_hex}"
        
    except Exception as e:
        console.print(f"[red]✗ Transaction error: {e}[/red]")
        return False, str(e)


def main() -> None:
    parser = argparse.ArgumentParser(description="Automated voting executor with safety checks")
    parser.add_argument("--db-path", default=DATABASE_PATH, help="Database path")
    parser.add_argument("--rpc", default=os.getenv("RPC_URL", ""), help="RPC URL")
    parser.add_argument("--your-voting-power", type=int, default=int(os.getenv("YOUR_VOTING_POWER", "0")), help="Your total voting power")
    parser.add_argument("--top-k", type=int, default=int(os.getenv("MAX_GAUGES_TO_VOTE", "10")), help="Number of gauges to vote for")
    parser.add_argument("--query-block", type=int, default=0, help="Block to query (default: latest)")
    parser.add_argument("--discover-missing-pairs", action="store_true", help="On-chain enumerate missing reward tokens")
    parser.add_argument(
        "--private-key-source",
        default=os.getenv("AUTO_VOTE_WALLET_KEYFILE", ""),
        help="Private key source: raw key, file path, or 1Password reference (op://Vault/Item/field)",
    )
    parser.add_argument("--max-gas-price-gwei", type=float, default=float(os.getenv("AUTO_VOTE_MAX_GAS_PRICE_GWEI", "10")), help="Max gas price in Gwei")
    parser.add_argument(
        "--gas-limit",
        type=int,
        default=int(os.getenv("AUTO_VOTE_GAS_LIMIT", "1500000")),
        help="Minimum transaction gas limit for vote tx (default: 1500000)",
    )
    parser.add_argument(
        "--gas-buffer-multiplier",
        type=float,
        default=float(os.getenv("AUTO_VOTE_GAS_BUFFER_MULTIPLIER", "1.35")),
        help="Multiplier applied to estimated gas for live tx (default: 1.35)",
    )
    parser.add_argument("--dry-run", action="store_true", help="Dry run mode (no actual transaction)")
    parser.add_argument("--skip-fresh-fetch", action="store_true", help="Skip fetching fresh snapshot (use latest in DB)")
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
    args = parser.parse_args()
    
    # Validate inputs
    if not args.rpc:
        console.print("[red]Error: RPC_URL required[/red]")
        sys.exit(1)
    
    if args.your_voting_power <= 0:
        console.print("[red]Error: YOUR_VOTING_POWER must be > 0[/red]")
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
        except Exception as e:
            console.print(f"[red]✗ Failed to load wallet: {e}[/red]")
            sys.exit(1)
    elif args.dry_run:
        console.print("[yellow]Dry-run without wallet: simulation will use --simulate-from if provided[/yellow]")
    
    # Connect to database
    conn = sqlite3.connect(args.db_path)
    
    try:
        # Fetch fresh snapshot (unless skipped)
        if args.skip_fresh_fetch:
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
        
        # Calculate optimal allocation
        console.print("[cyan]Calculating optimal allocation...[/cyan]")
        allocation = calculate_optimal_allocation(
            conn=conn,
            snapshot_ts=snapshot_ts,
            your_voting_power=args.your_voting_power,
            top_k=args.top_k,
        )
        
        if not allocation:
            console.print("[red]No allocation generated[/red]")
            sys.exit(1)
        
        console.print(f"[green]✓ Allocated to {len(allocation)} pools[/green]")
        
        # Display allocation
        from rich.table import Table
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
        # Use constant proportions (relative weights) - contract normalizes them
        PROPORTION_PER_POOL = 10000  # Equal weight for equal allocation
        for idx, (gauge_addr, pool_addr, votes, current_votes, current_rewards, expected_to_us) in enumerate(allocation, start=1):
            pool_name = get_pool_name(w3, pool_addr, conn)
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
            vote_proportions.append(PROPORTION_PER_POOL)  # Use proportion, not absolute votes
        
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
        
        success, result = build_and_send_vote_transaction(
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
        )
        
        if success:
            console.print(f"\n[bold green]✓ AUTO-VOTE COMPLETED SUCCESSFULLY[/bold green]")
            if not args.dry_run:
                console.print(f"[green]Transaction Hash: {result}[/green]")
        else:
            console.print(f"\n[bold red]✗ AUTO-VOTE FAILED: {result}[/bold red]")
            sys.exit(1)
        
    finally:
        conn.close()


if __name__ == "__main__":
    main()
