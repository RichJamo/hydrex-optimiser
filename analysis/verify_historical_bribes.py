#!/usr/bin/env python3
"""
Verify on-chain bribe contract balances AT EPOCH FLIP TIME.

Uses the contract-based reward calculation formula and high-level data access layer.
Data is fetched once and cached in the database; this script reads from the cache.
"""

import os
import sys
from pathlib import Path
from datetime import datetime
from typing import List, Dict, Optional
from collections import defaultdict

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from web3 import Web3
from dotenv import load_dotenv
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.progress import track

from config.settings import (
    VOTER_ADDRESS, ONE_E18, SCALE_32, WEEK, LEGACY_POOL_SHARES, KNOWN_POOLS, DATABASE_PATH
)
from src.database import Database
from src.data_access import DataAccess
from src.contract_reward_calculator import (
    query_ve_delegation_snapshot,
    query_bribe_contract_state,
    calculate_expected_reward,
    ContractRewardCalculator,
)

load_dotenv()
console = Console()

# ═══ ABI Definitions ═══
VOTER_ABI = [
    {"inputs": [], "name": "ve", "outputs": [{"internalType": "address", "name": "", "type": "address"}], "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "_ve", "outputs": [{"internalType": "address", "name": "", "type": "address"}], "stateMutability": "view", "type": "function"}
]

BRIBE_CALC_ABI = [
    {"inputs": [], "name": "WEEK", "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}], "stateMutability": "view", "type": "function"},
    {"inputs": [{"internalType": "address", "name": "", "type": "address"}], "name": "isReward", "outputs": [{"internalType": "bool", "name": "", "type": "bool"}], "stateMutability": "view", "type": "function"},
    {"inputs": [{"internalType": "address", "name": "", "type": "address"}, {"internalType": "uint256", "name": "", "type": "uint256"}], "name": "rewardData",
     "outputs": [{"internalType": "uint256", "name": "periodFinish", "type": "uint256"}, {"internalType": "uint256", "name": "rewardsPerEpoch", "type": "uint256"}, {"internalType": "uint256", "name": "lastUpdateTime", "type": "uint256"}],
     "stateMutability": "view", "type": "function"},
    {"inputs": [{"internalType": "uint256", "name": "", "type": "uint256"}], "name": "totalSupplyAt", "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}], "stateMutability": "view", "type": "function"},
    {"inputs": [{"internalType": "address", "name": "", "type": "address"}, {"internalType": "uint256", "name": "", "type": "uint256"}], "name": "balanceOfOwnerAt", "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}], "stateMutability": "view", "type": "function"}
]

VE_ABI = [
    {"inputs": [{"internalType": "uint256", "name": "_tokenId", "type": "uint256"}], "name": "ownerOf", "outputs": [{"internalType": "address", "name": "", "type": "address"}], "stateMutability": "view", "type": "function"},
    {"inputs": [{"internalType": "address", "name": "_owner", "type": "address"}], "name": "balanceOf", "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}], "stateMutability": "view", "type": "function"},
    {"inputs": [{"internalType": "address", "name": "_owner", "type": "address"}, {"internalType": "uint256", "name": "_index", "type": "uint256"}], "name": "tokenOfOwnerByIndex", "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}], "stateMutability": "view", "type": "function"},
    {"inputs": [{"internalType": "uint256", "name": "_tokenId", "type": "uint256"}, {"internalType": "uint48", "name": "_block", "type": "uint48"}], "name": "delegates", "outputs": [{"internalType": "address", "name": "", "type": "address"}], "stateMutability": "view", "type": "function"},
    {"inputs": [{"internalType": "uint256", "name": "_tokenId", "type": "uint256"}, {"internalType": "uint256", "name": "_block", "type": "uint256"}], "name": "balanceOfNFTAt", "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}], "stateMutability": "view", "type": "function"},
    {"inputs": [{"internalType": "address", "name": "_account", "type": "address"}, {"internalType": "uint256", "name": "_block", "type": "uint256"}], "name": "getPastVotes", "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}], "stateMutability": "view", "type": "function"}
]

ERC20_ABI = [
    {"constant": True, "inputs": [{"name": "_owner", "type": "address"}], "name": "balanceOf", "outputs": [{"name": "balance", "type": "uint256"}], "type": "function"},
    {"constant": True, "inputs": [], "name": "decimals", "outputs": [{"name": "", "type": "uint8"}], "type": "function"}
]


def find_block_at_timestamp(w3: Web3, target_ts: int, tolerance: int = 60) -> int:
    """Binary search to find block at target timestamp."""
    left, right = 0, w3.eth.block_number
    
    while left < right:
        mid = (left + right) // 2
        block = w3.eth.get_block(mid)
        if block['timestamp'] < target_ts:
            left = mid + 1
        else:
            right = mid
    
    return left


def main():
    """Verify historical bribes against actual payouts."""
    
    # ═══ Setup ═══
    rpc_url = os.getenv("RPC_URL")
    if not rpc_url:
        console.print("[red]RPC_URL not set in .env[/red]")
        return
    
    w3 = Web3(Web3.HTTPProvider(rpc_url))
    if not w3.is_connected():
        console.print("[red]Failed to connect to RPC[/red]")
        return
    
    console.print("[green]Connected to blockchain[/green]")
    
    # Load database
    db = Database(DATABASE_PATH)
    da = DataAccess(db)
    
    # Configuration
    closed_epoch = int(os.getenv("CLOSED_EPOCH", "1771372800"))
    your_address = os.getenv("YOUR_ADDRESS", "").strip()
    your_token_id = os.getenv("YOUR_TOKEN_ID", "").strip()
    
    if not your_address:
        console.print("[red]YOUR_ADDRESS not set in .env[/red]")
        return
    
    # Hardcoded actual received amounts (from epoch payouts)
    actual_received = {
        ("HYDX/USDC", "internal"): {"HYDX": 670.88, "USDC": 62.51},
        ("HYDX/USDC", "external"): {"USDC": 171.99, "oHYDX": 0.000000027480406},
        ("kVCM/USDC", "internal"): {"USDC": 0.439046, "kVCM": 5.57},
        ("kVCM/USDC", "external"): {"kVCM": 1615.61},
        ("WETH/USDC", "internal"): {"USDC": 92.60, "WETH": 0.046357},
        ("WETH/USDC", "external"): {},
    }

    token_aliases = {
        # Full addresses
        "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913": "USDC",
        "0x4200000000000000000000000000000000000006": "WETH",
        "0x00000e7efa313f4e11bfff432471ed9423ac6b30": "HYDX",
        "0xa1136031150e50b015b41f1ca6b2e99e49d8cb78": "oHYDX",
        "0x00fbac94fec8d4089d3fe979f39454f48c71a65d": "kVCM",
        # Abbreviated forms currently present in token metadata cache
        "0x8335...2913": "USDC",
        "0x4200...0006": "WETH",
        "0x0000...6b30": "HYDX",
    }

    token_decimals_overrides = {
        "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913": 6,  # USDC
        "0xfde4c96c8593536e31f229ea8f37b2ada2699bb2": 6,  # USDT
    }

    symbol_decimals_overrides = {
        "USDC": 6,
        "USDT": 6,
    }

    token_decimals_cache = {}

    def canonical_token_symbol(raw_symbol: str, token_addr: str) -> str:
        if raw_symbol in token_aliases:
            return token_aliases[raw_symbol]
        if token_addr and token_addr.lower() in token_aliases:
            return token_aliases[token_addr.lower()]
        return raw_symbol

    def resolve_token_decimals(token_addr: str, canonical_symbol: str, db_decimals: Optional[int]) -> int:
        token_addr_l = (token_addr or "").lower()
        if token_addr_l in token_decimals_cache:
            return token_decimals_cache[token_addr_l]

        if token_addr_l in token_decimals_overrides:
            token_decimals_cache[token_addr_l] = token_decimals_overrides[token_addr_l]
            return token_decimals_cache[token_addr_l]

        if canonical_symbol in symbol_decimals_overrides:
            token_decimals_cache[token_addr_l] = symbol_decimals_overrides[canonical_symbol]
            return token_decimals_cache[token_addr_l]

        if db_decimals is not None and db_decimals > 0:
            token_decimals_cache[token_addr_l] = db_decimals
            return token_decimals_cache[token_addr_l]

        try:
            if Web3.is_address(token_addr):
                token_contract = w3.eth.contract(
                    address=Web3.to_checksum_address(token_addr),
                    abi=ERC20_ABI,
                )
                onchain_decimals = int(token_contract.functions.decimals().call())
                token_decimals_cache[token_addr_l] = onchain_decimals
                return onchain_decimals
        except Exception:
            pass

        token_decimals_cache[token_addr_l] = 18
        return 18
    
    # ═══ Resolve VE NFT ═══
    voter = w3.eth.contract(address=Web3.to_checksum_address(VOTER_ADDRESS), abi=VOTER_ABI)
    try:
        ve_address = voter.functions.ve().call()
    except Exception:
        ve_address = voter.functions._ve().call()
    
    ve = w3.eth.contract(address=Web3.to_checksum_address(ve_address), abi=VE_ABI)
    
    if your_token_id:
        token_id = int(your_token_id)
        owner = ve.functions.ownerOf(token_id).call()
        if owner.lower() != your_address.lower():
            console.print(f"[red]Token ID {token_id} not owned by YOUR_ADDRESS[/red]")
            return
    else:
        nft_count = ve.functions.balanceOf(Web3.to_checksum_address(your_address)).call()
        if nft_count != 1:
            console.print(f"[red]YOUR_ADDRESS owns {nft_count} NFTs; set YOUR_TOKEN_ID in .env[/red]")
            return
        token_id = ve.functions.tokenOfOwnerByIndex(Web3.to_checksum_address(your_address), 0).call()
    
    console.print(f"[cyan]Using ve {ve_address} and tokenId {token_id}[/cyan]\n")
    
    # ═══ Get Bribes from Database ═══
    epoch_summary = da.get_bribes_for_epoch_detailed(closed_epoch)
    if not epoch_summary.bribes:
        console.print("[yellow]No bribe data in database for this epoch[/yellow]")
        return

    verify_all_pools = os.getenv("VERIFY_ALL_POOLS", "0").strip().lower() in {"1", "true", "yes"}
    focus_pools = {pool for (pool, _bribe_type) in actual_received.keys()}
    focus_pool_addresses = {
        addr.lower() for addr, name in KNOWN_POOLS.items() if name in focus_pools
    }

    if not verify_all_pools and focus_pools:
        filtered_bribes = [
            b
            for b in epoch_summary.bribes
            if (b.pool_name or "") in focus_pools
            or (b.pool_name or "").lower() in focus_pool_addresses
        ]
        console.print(
            f"[cyan]Filtering bribes to configured pools ({len(focus_pools)} pools): "
            f"{len(filtered_bribes)}/{len(epoch_summary.bribes)} rows[/cyan]"
        )
        epoch_summary.bribes = filtered_bribes

        if not epoch_summary.bribes:
            console.print("[yellow]No bribes left after pool filtering; set VERIFY_ALL_POOLS=1 to scan all pools.[/yellow]")
            return
    
    # ═══ Find Blocks at Key Timestamps ═══
    epoch_block = find_block_at_timestamp(w3, closed_epoch)
    t5_block = find_block_at_timestamp(w3, closed_epoch - 300)
    t1_block = find_block_at_timestamp(w3, closed_epoch - 60)
    
    console.print(f"[green]Epoch block: {epoch_block} at {datetime.utcfromtimestamp(w3.eth.get_block(epoch_block)['timestamp']).isoformat()}[/green]")
    console.print(f"[green]T-5 block:   {t5_block} at {datetime.utcfromtimestamp(w3.eth.get_block(t5_block)['timestamp']).isoformat()}[/green]")
    console.print(f"[green]T-1 block:   {t1_block} at {datetime.utcfromtimestamp(w3.eth.get_block(t1_block)['timestamp']).isoformat()}[/green]\n")
    
    # ═══ Calculate Expected Rewards ═══
    calculator = ContractRewardCalculator(w3, ve)
    
    # Get first bribe to determine WEEK
    first_bribe = w3.eth.contract(
        address=Web3.to_checksum_address(epoch_summary.bribes[0].bribe_contract),
        abi=BRIBE_CALC_ABI
    )
    week = first_bribe.functions.WEEK().call()
    calc_epoch = (closed_epoch // week) * week
    
    console.print(f"[cyan]Calculation epoch (WEEK-aligned): {calc_epoch}[/cyan]\n")
    
    # ═══ Build Results ═══
    results = []
    suppressed_reverts = defaultdict(int)
    unexpected_errors = []
    skipped_invalid_address = 0
    skipped_non_reward = 0
    unsupported_pairs = set()
    pair_result_cache = {}
    
    console.print(f"[cyan]Processing {len(epoch_summary.bribes)} bribe/token rows...[/cyan]")

    for bribe_detail in track(epoch_summary.bribes, description="Evaluating bribe rows"):
        raw_pool_name = bribe_detail.pool_name or "Unknown"
        pool_name = KNOWN_POOLS.get(raw_pool_name.lower(), raw_pool_name)
        token_symbol = bribe_detail.token_symbol or "Unknown"
        token_address = (bribe_detail.token_address or "").strip()
        bribe_address = (bribe_detail.bribe_contract or "").strip()
        canonical_symbol = canonical_token_symbol(token_symbol, token_address)
        token_decimals = resolve_token_decimals(token_address, canonical_symbol, bribe_detail.token_decimals)
        
        actual = actual_received.get((pool_name, bribe_detail.bribe_type), {}).get(canonical_symbol, 0)

        if not Web3.is_address(bribe_address):
            skipped_invalid_address += 1
            continue
        if not Web3.is_address(token_address):
            skipped_invalid_address += 1
            continue

        pair_key = (bribe_address.lower(), token_address.lower())
        if pair_key in unsupported_pairs:
            skipped_non_reward += 1
            continue
        
        try:
            bribe_contract = w3.eth.contract(
                address=Web3.to_checksum_address(bribe_address),
                abi=BRIBE_CALC_ABI
            )

            if pair_key in pair_result_cache:
                expected_reward, t5_estimate, t1_estimate = pair_result_cache[pair_key]
            else:
                try:
                    if not bribe_contract.functions.isReward(Web3.to_checksum_address(token_address)).call():
                        unsupported_pairs.add(pair_key)
                        skipped_non_reward += 1
                        continue
                except Exception:
                    pass
            
                # Calculate expected reward (at epoch)
                expected_reward = calculator.calculate_reward(
                    token_id=token_id,
                    calc_epoch=calc_epoch,
                    bribe_contract=bribe_contract,
                    token_address=token_address,
                    token_decimals=token_decimals,
                    fallback_db_amount=bribe_detail.amount_human,
                    legacy_pool_share=LEGACY_POOL_SHARES.get(pool_name),
                    block_identifier=None,  # Latest state
                )
                
                # Calculate T-5 estimate
                t5_estimate = calculator.calculate_reward(
                    token_id=token_id,
                    calc_epoch=calc_epoch,
                    bribe_contract=bribe_contract,
                    token_address=token_address,
                    token_decimals=token_decimals,
                    fallback_db_amount=bribe_detail.amount_human,
                    legacy_pool_share=LEGACY_POOL_SHARES.get(pool_name),
                    block_identifier=t5_block,
                )
                
                # Calculate T-1 estimate
                t1_estimate = calculator.calculate_reward(
                    token_id=token_id,
                    calc_epoch=calc_epoch,
                    bribe_contract=bribe_contract,
                    token_address=token_address,
                    token_decimals=token_decimals,
                    fallback_db_amount=bribe_detail.amount_human,
                    legacy_pool_share=LEGACY_POOL_SHARES.get(pool_name),
                    block_identifier=t1_block,
                )
                pair_result_cache[pair_key] = (expected_reward, t5_estimate, t1_estimate)
            
            # Get balance at epoch
            token_contract = w3.eth.contract(
                address=Web3.to_checksum_address(token_address),
                abi=ERC20_ABI
            )
            balance_wei = token_contract.functions.balanceOf(
                Web3.to_checksum_address(bribe_detail.bribe_contract)
            ).call(block_identifier=epoch_block)
            balance_tokens = balance_wei / (10 ** token_decimals)
            
            results.append({
                "pool": pool_name,
                "type": bribe_detail.bribe_type,
                "token": canonical_symbol,
                "expected": expected_reward,
                "t5_est": t5_estimate,
                "t1_est": t1_estimate,
                "actual": actual,
                "db_amount": bribe_detail.amount_human or 0,
                "balance_at_epoch": balance_tokens,
                "bribe_contract": bribe_detail.bribe_contract,
            })
        
        except Exception as e:
            message = str(e).lower()
            error_key = f"{bribe_detail.bribe_contract}/{token_symbol}"
            if "execution reverted" in message:
                suppressed_reverts[error_key] += 1
            else:
                unexpected_errors.append((pool_name, token_symbol, str(e)))
                console.print(f"[red]Error processing {pool_name}/{token_symbol}: {e}[/red]")
    
    # ═══ Display Results ═══
    panel = Panel("[bold cyan]Historical Bribe Verification[/bold cyan]", expand=False)
    console.print(panel)
    
    table = Table(show_header=True, header_style="bold cyan")
    table.add_column("Pool", width=15)
    table.add_column("Type", width=10)
    table.add_column("Token", width=10)
    table.add_column("Expected", width=15, justify="right")
    table.add_column("T-5 Est.", width=15, justify="right")
    table.add_column("T-1 Est.", width=15, justify="right")
    table.add_column("Actual %", width=12, justify="right")
    table.add_column("DB Amount", width=15, justify="right")
    table.add_column("You Got", width=15, justify="right")
    table.add_column("Balance @ Epoch", width=18, justify="right")
    
    for r in results:
        # Format numbers
        expected_str = f"{r['expected']:,.2f}" if r['expected'] >= 1 else f"{r['expected']:.9f}".rstrip("0").rstrip(".")
        t5_str = f"{r['t5_est']:,.2f}" if r['t5_est'] >= 1 else f"{r['t5_est']:.9f}".rstrip("0").rstrip(".")
        t1_str = f"{r['t1_est']:,.2f}" if r['t1_est'] >= 1 else f"{r['t1_est']:.9f}".rstrip("0").rstrip(".")
        actual_str = f"{r['actual']:,.2f}" if r['actual'] >= 1 else f"{r['actual']:.9f}".rstrip("0").rstrip(".")
        db_str = f"{r['db_amount']:,.2f}" if r['db_amount'] >= 1 else f"{r['db_amount']:.9f}".rstrip("0").rstrip(".")
        balance_str = f"{r['balance_at_epoch']:,.2f}" if r['balance_at_epoch'] >= 1 else f"{r['balance_at_epoch']:.9f}".rstrip("0").rstrip(".")
        
        # Color code actual %
        if r['expected'] > 0:
            pct = (r['actual'] / r['expected']) * 100
            if 99.5 <= pct <= 100.5:
                actual_pct_str = f"[green]{pct:.2f}%[/green]"
            elif pct > 100.5:
                actual_pct_str = f"[cyan]{pct:.2f}%[/cyan]"
            elif pct >= 90:
                actual_pct_str = f"[yellow]{pct:.2f}%[/yellow]"
            else:
                actual_pct_str = f"[red]{pct:.2f}%[/red]"
        else:
            actual_pct_str = "-"
        
        # Color code type
        type_str = "[yellow]Internal[/yellow]" if r["type"] == "internal" else "[cyan]External[/cyan]"
        
        # Color code balance
        balance_display = f"[yellow]{balance_str}[/yellow]" if r['balance_at_epoch'] > 0 else f"[red]{balance_str}[/red]"
        
        table.add_row(
            r["pool"],
            type_str,
            r["token"],
            expected_str,
            t5_str,
            t1_str,
            actual_pct_str,
            db_str,
            actual_str,
            balance_display,
        )
    
    console.print(table)

    if suppressed_reverts:
        total_suppressed = sum(suppressed_reverts.values())
        unique_pairs = len(suppressed_reverts)
        console.print(
            f"[yellow]Suppressed {total_suppressed} expected contract reverts across {unique_pairs} bribe/token pairs.[/yellow]"
        )

    if skipped_invalid_address:
        console.print(f"[yellow]Skipped {skipped_invalid_address} rows with invalid token/bribe addresses.[/yellow]")

    if skipped_non_reward:
        console.print(f"[yellow]Skipped {skipped_non_reward} rows where token is not configured as a reward for the bribe contract.[/yellow]")

    if unexpected_errors:
        console.print(f"[red]Unexpected processing errors: {len(unexpected_errors)}[/red]")
        for pool, token, err in unexpected_errors[:5]:
            console.print(f"[red] - {pool}/{token}: {err}[/red]")


if __name__ == "__main__":
    main()
