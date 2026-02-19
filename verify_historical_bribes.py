#!/usr/bin/env python3
"""
Verify on-chain bribe contract balances AT EPOCH FLIP TIME.
"""

import sqlite3
import os
from web3 import Web3
from datetime import datetime
from dotenv import load_dotenv
from rich.console import Console
from rich.table import Table
from rich.panel import Panel

load_dotenv()

console = Console()

# Connect to Base
RPC_URL = os.getenv("RPC_URL")
w3 = Web3(Web3.HTTPProvider(RPC_URL))

if not w3.is_connected():
    console.print("[red]Failed to connect to Base[/red]")
    exit(1)

console.print("[green]Connected to Base[/green]")

# Load ERC20 ABI
ERC20_ABI = [
    {
        "constant": True,
        "inputs": [{"name": "_owner", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"name": "balance", "type": "uint256"}],
        "type": "function"
    },
    {
        "constant": True,
        "inputs": [],
        "name": "decimals",
        "outputs": [{"name": "", "type": "uint8"}],
        "type": "function"
    }
]

VOTER_ABI = [
    {
        "inputs": [],
        "name": "ve",
        "outputs": [{"internalType": "address", "name": "", "type": "address"}],
        "stateMutability": "view",
        "type": "function"
    },
    {
        "inputs": [],
        "name": "_ve",
        "outputs": [{"internalType": "address", "name": "", "type": "address"}],
        "stateMutability": "view",
        "type": "function"
    }
]

BRIBE_CALC_ABI = [
    {
        "inputs": [],
        "name": "WEEK",
        "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function"
    },
    {
        "inputs": [{"internalType": "address", "name": "", "type": "address"}, {"internalType": "uint256", "name": "", "type": "uint256"}],
        "name": "rewardData",
        "outputs": [
            {"internalType": "uint256", "name": "periodFinish", "type": "uint256"},
            {"internalType": "uint256", "name": "rewardsPerEpoch", "type": "uint256"},
            {"internalType": "uint256", "name": "lastUpdateTime", "type": "uint256"}
        ],
        "stateMutability": "view",
        "type": "function"
    },
    {
        "inputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
        "name": "totalSupplyAt",
        "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function"
    },
    {
        "inputs": [{"internalType": "address", "name": "", "type": "address"}, {"internalType": "uint256", "name": "", "type": "uint256"}],
        "name": "balanceOfOwnerAt",
        "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function"
    }
]

VE_ABI = [
    {
        "inputs": [{"internalType": "address", "name": "", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function"
    },
    {
        "inputs": [{"internalType": "address", "name": "", "type": "address"}, {"internalType": "uint256", "name": "", "type": "uint256"}],
        "name": "tokenOfOwnerByIndex",
        "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function"
    },
    {
        "inputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
        "name": "ownerOf",
        "outputs": [{"internalType": "address", "name": "", "type": "address"}],
        "stateMutability": "view",
        "type": "function"
    },
    {
        "inputs": [{"internalType": "uint256", "name": "", "type": "uint256"}, {"internalType": "uint48", "name": "", "type": "uint48"}],
        "name": "delegates",
        "outputs": [{"internalType": "address", "name": "", "type": "address"}],
        "stateMutability": "view",
        "type": "function"
    },
    {
        "inputs": [{"internalType": "uint256", "name": "", "type": "uint256"}, {"internalType": "uint256", "name": "", "type": "uint256"}],
        "name": "balanceOfNFTAt",
        "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function"
    },
    {
        "inputs": [{"internalType": "address", "name": "", "type": "address"}, {"internalType": "uint256", "name": "", "type": "uint256"}],
        "name": "getPastVotes",
        "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function"
    }
]

DATABASE_PATH = "data.db"
conn = sqlite3.connect(DATABASE_PATH)
cursor = conn.cursor()

CLOSED_EPOCH = 1771372800  # Feb 19, 2026 00:00:00 UTC
ONE_E18 = 10 ** 18
SCALE_32 = 10 ** 32

console.print(Panel.fit(
    "[bold cyan]Historical Bribe Balance Verification[/bold cyan]\n"
    f"At epoch flip: {datetime.utcfromtimestamp(CLOSED_EPOCH).isoformat()}",
    border_style="cyan"
))

# Find the block number at epoch flip
console.print(f"\n[cyan]Finding block number at timestamp {CLOSED_EPOCH}...[/cyan]")

# Binary search to find the block at this timestamp
def find_block_at_timestamp(target_timestamp, tolerance=60):
    """Find block number closest to target timestamp using binary search."""
    # Get latest block as upper bound
    latest_block = w3.eth.block_number
    latest_timestamp = w3.eth.get_block(latest_block)['timestamp']
    
    if target_timestamp > latest_timestamp:
        return latest_block
    
    # Estimate initial bounds (assumes ~2 second block time on Base)
    blocks_back = int((latest_timestamp - target_timestamp) / 2)
    left = max(0, latest_block - blocks_back - 1000)
    right = latest_block
    
    best_block = left
    
    # Binary search
    while left <= right:
        mid = (left + right) // 2
        mid_block = w3.eth.get_block(mid)
        mid_timestamp = mid_block['timestamp']
        
        if abs(mid_timestamp - target_timestamp) <= tolerance:
            return mid
        
        if mid_timestamp < target_timestamp:
            left = mid + 1
            if mid_timestamp <= target_timestamp:
                best_block = mid
        else:
            right = mid - 1
    
    return best_block

epoch_block = find_block_at_timestamp(CLOSED_EPOCH)
epoch_block_info = w3.eth.get_block(epoch_block)
console.print(f"[green]Found block {epoch_block} at {datetime.utcfromtimestamp(epoch_block_info['timestamp']).isoformat()}[/green]\n")

# Map pool addresses to pool names (must match complete_reconciliation.py)
pool_addr_map = {
    "0x51f0b932855986b0e621c9d4db6eee1f4644d3d2": "HYDX/USDC",
    "0xef96ec76eeb36584fc4922e9fa268e0780170f33": "kVCM/USDC",
    "0x82dbe18346a8656dbb5e76f74bf3ae279cc16b29": "WETH/USDC",
}

console.print(f"[bold cyan]Querying bribe contract balances at block {epoch_block}[/bold cyan]\n")

# Get all bribes where we expected something but got 0
# First, let's get what we actually received
ACTUAL_RECEIVED = {
    ("HYDX/USDC", "internal"): {"HYDX": 670.88, "USDC": 62.51},
    ("HYDX/USDC", "external"): {"USDC": 171.99, "oHYDX": 0.000000027480406},
    ("kVCM/USDC", "internal"): {"USDC": 0.439046, "kVCM": 5.57},
    ("kVCM/USDC", "external"): {"kVCM": 1615.61},
    ("WETH/USDC", "internal"): {"USDC": 92.60, "WETH": 0.046357},
    ("WETH/USDC", "external"): {},
}

# Resolve voter -> ve and tokenId (or use .env override)
VOTER_ADDRESS = os.getenv("VOTER_ADDRESS")
YOUR_ADDRESS = os.getenv("YOUR_ADDRESS")
YOUR_TOKEN_ID = os.getenv("YOUR_TOKEN_ID")

if not VOTER_ADDRESS:
    console.print("[red]VOTER_ADDRESS missing from .env[/red]")
    exit(1)

if not YOUR_ADDRESS:
    console.print("[red]YOUR_ADDRESS missing from .env[/red]")
    exit(1)

voter = w3.eth.contract(address=Web3.to_checksum_address(VOTER_ADDRESS), abi=VOTER_ABI)
ve_address = None

try:
    ve_address = voter.functions.ve().call()
except Exception:
    ve_address = voter.functions._ve().call()

ve = w3.eth.contract(address=Web3.to_checksum_address(ve_address), abi=VE_ABI)

if YOUR_TOKEN_ID:
    token_id = int(YOUR_TOKEN_ID)
    owner = ve.functions.ownerOf(token_id).call()
    if owner.lower() != YOUR_ADDRESS.lower():
        console.print(f"[red]YOUR_TOKEN_ID {token_id} is owned by {owner}, not YOUR_ADDRESS[/red]")
        exit(1)
else:
    nft_count = ve.functions.balanceOf(Web3.to_checksum_address(YOUR_ADDRESS)).call()
    if nft_count == 0:
        console.print("[red]YOUR_ADDRESS owns no veNFTs[/red]")
        exit(1)
    if nft_count > 1:
        console.print("[red]YOUR_ADDRESS owns multiple veNFTs. Set YOUR_TOKEN_ID in .env[/red]")
        exit(1)
    token_id = ve.functions.tokenOfOwnerByIndex(Web3.to_checksum_address(YOUR_ADDRESS), 0).call()

console.print(f"[cyan]Using ve contract {ve_address} and tokenId {token_id}[/cyan]\n")

# Query all bribes from database and check which ones we didn't receive
cursor.execute(f"""
    SELECT 
        g.pool,
        b.bribe_type,
        b.token_symbol,
        b.bribe_contract,
        b.reward_token,
        SUM(b.amount) as total_amount,
        MAX(b.token_decimals) as decimals
    FROM bribes b
    JOIN gauges g ON b.gauge_address = g.address
    WHERE b.epoch = {CLOSED_EPOCH}
    AND g.pool IN (?, ?, ?)
    GROUP BY g.pool, b.bribe_type, b.token_symbol, b.bribe_contract, b.reward_token
    ORDER BY g.pool, b.bribe_type, b.token_symbol
""", list(pool_addr_map.keys()))  # Use pool addresses, not names

rows = cursor.fetchall()

if not rows:
    console.print("[yellow]No rows found for selected pools/epoch[/yellow]")
    conn.close()
    exit(0)

# Align ve snapshot epoch to bribe WEEK (using first row's bribe contract)
first_bribe = w3.eth.contract(
    address=Web3.to_checksum_address(rows[0][3]),
    abi=BRIBE_CALC_ABI
)
first_week = first_bribe.functions.WEEK().call()
aligned_epoch = (CLOSED_EPOCH // first_week) * first_week
console.print(f"[cyan]Contract calc epoch (WEEK-aligned): {aligned_epoch}[/cyan]\n")

results = []
bribe_calc_cache = {}
delegatee_balance_cache = {}
bribe_week_cache = {}

delegatee = ve.functions.delegates(token_id, aligned_epoch).call()
power = ve.functions.balanceOfNFTAt(token_id, aligned_epoch).call()
delegatee_past_votes = 0
if delegatee != "0x0000000000000000000000000000000000000000":
    delegatee_past_votes = ve.functions.getPastVotes(delegatee, aligned_epoch).call()

weight_raw_1e18 = 0
if delegatee_past_votes > 0:
    weight_raw_1e18 = (power * ONE_E18) // delegatee_past_votes

for pool_addr, bribe_type, token_symbol, bribe_contract, token_addr, total_amount, decimals in rows:
    pool_name = pool_addr_map.get(pool_addr.lower()) if pool_addr else None
    
    if not pool_name:
        console.print(f"[red]Warning: Could not map pool address {pool_addr}[/red]")
        continue
    
    # Check if we received this token
    actual = ACTUAL_RECEIVED.get((pool_name, bribe_type), {}).get(token_symbol, 0)
    vote_share = weight_raw_1e18 / ONE_E18
    expected_reward = 0
    
    # Query historical balance for all rows
    try:
        token_contract = w3.eth.contract(
            address=Web3.to_checksum_address(token_addr),
            abi=ERC20_ABI
        )
        bribe_contract_instance = w3.eth.contract(
            address=Web3.to_checksum_address(bribe_contract),
            abi=BRIBE_CALC_ABI
        )

        contract_key = bribe_contract.lower()
        if contract_key in bribe_week_cache:
            week = bribe_week_cache[contract_key]
        else:
            week = bribe_contract_instance.functions.WEEK().call()
            bribe_week_cache[contract_key] = week

        calc_epoch = (CLOSED_EPOCH // week) * week

        cache_key = (bribe_contract.lower(), token_addr.lower(), calc_epoch)
        if cache_key in bribe_calc_cache:
            rewards_per_epoch_raw, total_supply_at_epoch = bribe_calc_cache[cache_key]
        else:
            reward_data = bribe_contract_instance.functions.rewardData(
                Web3.to_checksum_address(token_addr), calc_epoch
            ).call()
            rewards_per_epoch_raw = reward_data[1]
            total_supply_at_epoch = bribe_contract_instance.functions.totalSupplyAt(calc_epoch).call()
            bribe_calc_cache[cache_key] = (rewards_per_epoch_raw, total_supply_at_epoch)

        delegatee_balance_key = (bribe_contract.lower(), delegatee.lower(), calc_epoch)
        if delegatee_balance_key in delegatee_balance_cache:
            delegatee_pool_balance = delegatee_balance_cache[delegatee_balance_key]
        else:
            delegatee_pool_balance = bribe_contract_instance.functions.balanceOfOwnerAt(
                Web3.to_checksum_address(delegatee), calc_epoch
            ).call()
            delegatee_balance_cache[delegatee_balance_key] = delegatee_pool_balance

        if total_supply_at_epoch == 0:
            manual_reward_per_token = rewards_per_epoch_raw * SCALE_32
        else:
            manual_reward_per_token = (rewards_per_epoch_raw * SCALE_32) // total_supply_at_epoch

        manual_epoch_reward_raw = 0
        if delegatee_past_votes > 0 and power > 0 and delegatee_pool_balance > 0:
            manual_epoch_reward_raw = (manual_reward_per_token * delegatee_pool_balance) // SCALE_32
            manual_epoch_reward_raw = (manual_epoch_reward_raw * weight_raw_1e18) // ONE_E18

        if decimals:
            expected_reward = manual_epoch_reward_raw / (10 ** decimals)
        else:
            expected_reward = float(manual_epoch_reward_raw)
        
        # Get balance at epoch block
        balance_wei = token_contract.functions.balanceOf(
            Web3.to_checksum_address(bribe_contract)
        ).call(block_identifier=epoch_block)
        
        balance_tokens = balance_wei / (10 ** decimals) if decimals else balance_wei
        
        results.append({
            "pool": pool_name,
            "type": bribe_type,
            "token": token_symbol,
            "vote_share": vote_share,
            "expected_reward": expected_reward,
            "db_amount": total_amount,
            "actual_received": actual,
            "bribe_contract": bribe_contract,
            "token_address": token_addr,
            "balance_at_epoch": balance_tokens,
        })
        
    except Exception as e:
        results.append({
            "pool": pool_name,
            "type": bribe_type,
            "token": token_symbol,
            "vote_share": vote_share,
            "expected_reward": expected_reward,
            "db_amount": total_amount,
            "actual_received": actual,
                "bribe_contract": bribe_contract,
                "token_address": token_addr,
                "balance_at_epoch": f"Error: {str(e)}",
            })

# Display results
results_table = Table(show_header=True, header_style="bold cyan")
results_table.add_column("Pool", width=15)
results_table.add_column("Type", width=10)
results_table.add_column("Token", width=10)
results_table.add_column("Expected", width=15, justify="right")
results_table.add_column("Actual %", width=12, justify="right")
results_table.add_column("DB Amount", width=15, justify="right")
results_table.add_column("You Received", width=15, justify="right")
results_table.add_column("Balance @ Epoch", width=18, justify="right")
results_table.add_column("Bribe Contract", width=10)

for r in results:
    # Format displays
    if isinstance(r["expected_reward"], (int, float)):
        if r["expected_reward"] < 0.001:
            expected_display = f"{r['expected_reward']:.15f}".rstrip("0").rstrip(".")
        elif r["expected_reward"] < 1:
            expected_display = f"{r['expected_reward']:.9f}".rstrip("0").rstrip(".")
        else:
            expected_display = f"{r['expected_reward']:,.2f}"
    else:
        expected_display = str(r["expected_reward"])

    if isinstance(r["expected_reward"], (int, float)) and isinstance(r["actual_received"], (int, float)):
        if r["expected_reward"] > 0:
            actual_pct = (r["actual_received"] / r["expected_reward"]) * 100
            if 99.5 <= actual_pct <= 100.5:
                actual_pct_display = f"[green]{actual_pct:.2f}%[/green]"
            elif actual_pct > 100.5:
                actual_pct_display = f"[cyan]{actual_pct:.2f}%[/cyan]"
            elif actual_pct >= 90:
                actual_pct_display = f"[yellow]{actual_pct:.2f}%[/yellow]"
            else:
                actual_pct_display = f"[red]{actual_pct:.2f}%[/red]"
        else:
            actual_pct_display = "-"
    else:
        actual_pct_display = "-"

    if isinstance(r["db_amount"], (int, float)):
        if r["db_amount"] < 1:
            db_display = f"{r['db_amount']:.9f}".rstrip("0").rstrip(".")
        else:
            db_display = f"{r['db_amount']:,.2f}"
    else:
        db_display = str(r["db_amount"])
    
    if isinstance(r["actual_received"], (int, float)):
        if r["actual_received"] < 0.001:
            actual_display = f"{r['actual_received']:.15f}".rstrip("0").rstrip(".")
        else:
            actual_display = f"{r['actual_received']:,.2f}"
    else:
        actual_display = str(r["actual_received"])
    
    if isinstance(r["balance_at_epoch"], (int, float)):
        if r["balance_at_epoch"] < 1:
            balance_display = f"{r['balance_at_epoch']:.9f}".rstrip("0").rstrip(".")
        else:
            balance_display = f"{r['balance_at_epoch']:,.2f}"
        
        # Color code
        if r["balance_at_epoch"] > 0:
            balance_display = f"[yellow]{balance_display}[/yellow]"
        else:
            balance_display = f"[red]{balance_display}[/red]"
    else:
        balance_display = f"[red]{r['balance_at_epoch']}[/red]"
    
    type_display = "[yellow]Internal[/yellow]" if r["type"] == "internal" else "[cyan]External[/cyan]"
    
    results_table.add_row(
        r["pool"],
        type_display,
        r["token"],
        expected_display,
        actual_pct_display,
        db_display,
        actual_display,
        balance_display,
        r["bribe_contract"][:5]
    )

console.print(results_table)

console.print(f"\n[bold cyan]Analysis:[/bold cyan]\n")
console.print("[yellow]If Balance @ Epoch > 0:[/yellow]")
console.print("  • Bribes existed at epoch flip but you weren't distributed your share")
console.print("  • Possible reasons: dust threshold, claim required, distribution bug\n")

console.print("[yellow]If Balance @ Epoch = 0:[/yellow]")
console.print("  • Database recorded phantom bribes that didn't exist at epoch")
console.print("  • Data collection timing issue (bribes added after snapshot but before epoch)\n")

conn.close()
