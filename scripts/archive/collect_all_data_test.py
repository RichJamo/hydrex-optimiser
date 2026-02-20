#!/usr/bin/env python3
"""
TEST VERSION: Collect data for only 5 gauges to test the flow.
"""

import json
import time
import sqlite3
from web3 import Web3
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn
from rich.table import Table
from rich.panel import Panel
from datetime import datetime

from config import Config
from src.database import Database
from src.price_feed import PriceFeed

console = Console()

# Configuration
RPC_URL = "https://base-mainnet.g.alchemy.com/v2/oFfvEpXYjGo8Nj4QQIkU3kXd6Z0JvfJZ"
VOTER_ADDRESS = "0xc69E3eF39E3fFBcE2A1c570f8d3ADF76909ef17b"
DATABASE_PATH = "data.db"

# TEST: Only process 5 gauges
TEST_MODE = True
TEST_GAUGE_LIMIT = 5

# Setup Web3
w3 = Web3(Web3.HTTPProvider(RPC_URL))
console.print(f"[cyan]Connected to Base: {w3.is_connected()}[/cyan]")
console.print(f"[yellow]TEST MODE: Processing only {TEST_GAUGE_LIMIT} gauges[/yellow]\n")

# Load ABIs
with open("voterv5_abi.json", "r") as f:
    voter_abi = json.load(f)

with open("bribev2_abi.json", "r") as f:
    bribe_abi = json.load(f)

# Initialize voter contract
voter = w3.eth.contract(
    address=Web3.to_checksum_address(VOTER_ADDRESS),
    abi=voter_abi
)

# Initialize database and price feed
database = Database(DATABASE_PATH)
price_feed = PriceFeed(api_key=Config.COINGECKO_API_KEY, database=database)

# Rest of the code from collect_all_data.py but limited to 5 gauges
def get_all_gauges():
    """Query VoterV5 for gauge addresses (TEST: only first 5)."""
    console.print("\n[bold cyan]Step 1: Querying Gauges from VoterV5 (TEST)[/bold cyan]")
    console.print("=" * 100)
    
    try:
        # Get gauge count
        gauge_count = voter.functions.length().call()
        console.print(f"[green]Total gauges in contract: {gauge_count}[/green]")
        console.print(f"[yellow]TEST: Fetching only first {TEST_GAUGE_LIMIT} gauges[/yellow]")
        
        gauges = []
        
        for i in range(min(TEST_GAUGE_LIMIT, gauge_count)):
            try:
                pool_addr = voter.functions.pools(i).call()
                gauge_addr = voter.functions.gauges(pool_addr).call()
                
                # Skip if no gauge exists
                if gauge_addr == "0x0000000000000000000000000000000000000000":
                    continue
                    
                gauges.append(gauge_addr)
                console.print(f"  {i+1}. Pool: {pool_addr[:10]}... → Gauge: {gauge_addr}")
                        
            except Exception as e:
                console.print(f"[yellow]Warning: Error fetching pool/gauge {i}: {e}[/yellow]")
        
        console.print(f"[green]✓ Retrieved {len(gauges)} gauge addresses[/green]")
        return gauges
        
    except Exception as e:
        console.print(f"[red]Error querying gauges: {e}[/red]")
        return []

def get_gauge_details(gauge_addr):
    """Get pool, bribe contracts, and votes for a gauge."""
    try:
        gauge_checksum = Web3.to_checksum_address(gauge_addr)
        
        # Get pool address
        pool = voter.functions.poolForGauge(gauge_checksum).call()
        
        # Get bribe contracts
        internal_bribe = voter.functions.internal_bribes(gauge_checksum).call()
        external_bribe = voter.functions.external_bribes(gauge_checksum).call()
        
        # Get current votes (weight)
        try:
            votes = voter.functions.weights(Web3.to_checksum_address(pool)).call()
        except:
            votes = 0
        
        # Check if alive
        is_alive = voter.functions.isAlive(gauge_checksum).call()
        
        return {
            'address': gauge_addr.lower(),
            'pool': pool.lower(),
            'internal_bribe': internal_bribe.lower(),
            'external_bribe': external_bribe.lower(),
            'current_votes': votes,
            'is_alive': 1 if is_alive else 0
        }
        
    except Exception as e:
        console.print(f"[red]Error getting details for {gauge_addr[:10]}...: {e}[/red]")
        return None

def map_gauges(gauges):
    """Map gauges to their pools and bribe contracts."""
    console.print("\n[bold cyan]Step 2: Mapping Gauges → Pools → Bribe Contracts[/bold cyan]")
    console.print("=" * 100)
    
    gauge_details = []
    
    for i, gauge_addr in enumerate(gauges, 1):
        console.print(f"  [{i}/{len(gauges)}] Mapping {gauge_addr[:10]}...")
        details = get_gauge_details(gauge_addr)
        if details:
            gauge_details.append(details)
            console.print(f"       Pool: {details['pool'][:20]}...")
            console.print(f"       Internal Bribe: {details['internal_bribe'][:20]}...")
            console.print(f"       External Bribe: {details['external_bribe'][:20]}...")
            console.print(f"       Current Votes: {details['current_votes']:,}")
        time.sleep(0.2)
    
    console.print(f"[green]✓ Mapped {len(gauge_details)} gauges[/green]")
    return gauge_details

def get_current_epoch():
    """Calculate current epoch timestamp (Wednesday 00:00 UTC)."""
    from datetime import datetime, timezone, timedelta
    
    now = datetime.now(timezone.utc)
    # Wednesday is day 2 (0=Monday)
    days_since_wednesday = (now.weekday() - 2) % 7
    epoch_start = now.replace(
        hour=0, minute=0, second=0, microsecond=0
    ) - timedelta(days=days_since_wednesday)
    
    return int(epoch_start.timestamp())

def get_bribe_rewards(bribe_addr, bribe_type, gauge_addr):
    """Query a bribe contract for available reward tokens and balances."""
    try:
        bribe_checksum = Web3.to_checksum_address(bribe_addr)
        bribe_contract = w3.eth.contract(address=bribe_checksum, abi=bribe_abi)
        
        # Get number of reward tokens
        try:
            reward_count = bribe_contract.functions.rewardsListLength().call()
        except:
            return []
        
        if reward_count == 0:
            return []
        
        rewards = []
        
        for i in range(reward_count):
            try:
                # Get reward token address
                token_addr = bribe_contract.functions.rewardTokens(i).call()
                
                # Get token balance in bribe contract
                token_contract = w3.eth.contract(
                    address=Web3.to_checksum_address(token_addr),
                    abi=[
                        {"constant": True, "inputs": [{"name": "_owner", "type": "address"}], 
                         "name": "balanceOf", "outputs": [{"name": "balance", "type": "uint256"}], 
                         "type": "function"},
                        {"constant": True, "inputs": [], "name": "decimals", 
                         "outputs": [{"name": "", "type": "uint8"}], "type": "function"},
                        {"constant": True, "inputs": [], "name": "symbol", 
                         "outputs": [{"name": "", "type": "string"}], "type": "function"}
                    ]
                )
                
                balance = token_contract.functions.balanceOf(bribe_checksum).call()
                
                if balance == 0:
                    continue
                
                # Try to get token info
                try:
                    decimals = token_contract.functions.decimals().call()
                    symbol = token_contract.functions.symbol().call()
                except:
                    decimals = 18
                    symbol = "UNKNOWN"
                
                balance_decimal = balance / (10 ** decimals)
                
                # Get USD price for the token
                usd_price = price_feed.get_token_price(token_addr.lower())
                usd_value = balance_decimal * usd_price if usd_price else None
                
                rewards.append({
                    'bribe_contract': bribe_addr.lower(),
                    'bribe_type': bribe_type,
                    'gauge_address': gauge_addr.lower(),
                    'reward_token': token_addr.lower(),
                    'token_symbol': symbol,
                    'token_decimals': decimals,
                    'amount_wei': str(balance),
                    'amount': balance_decimal,
                    'usd_price': usd_price,
                    'usd_value': usd_value
                })
                
            except Exception as e:
                console.print(f"[yellow]Warning: Error reading token {i} from {bribe_addr[:10]}...: {e}[/yellow]")
                continue
        
        return rewards
        
    except Exception as e:
        return []

def scan_bribes(gauge_details):
    """Scan bribe contracts for test gauges."""
    console.print("\n[bold cyan]Step 3: Scanning Bribe Contracts for Rewards[/bold cyan]")
    console.print("=" * 100)
    
    all_rewards = []
    
    for i, gauge in enumerate(gauge_details, 1):
        console.print(f"\n[{i}/{len(gauge_details)}] Gauge: {gauge['address'][:20]}...")
        
        # Scan internal bribe
        if gauge['internal_bribe'] and gauge['internal_bribe'] != "0x0000000000000000000000000000000000000000":
            console.print(f"  Scanning internal bribe: {gauge['internal_bribe'][:20]}...")
            rewards = get_bribe_rewards(gauge['internal_bribe'], "internal", gauge['address'])
            if rewards:
                for r in rewards:
                    usd_str = f"${r['usd_value']:.2f}" if r['usd_value'] else "No price"
                    console.print(f"    • {r['amount']:.4f} {r['token_symbol']} ({usd_str})")
            else:
                console.print(f"    [dim](no rewards)[/dim]")
            all_rewards.extend(rewards)
        
        # Scan external bribe
        if gauge['external_bribe'] and gauge['external_bribe'] != "0x0000000000000000000000000000000000000000":
            console.print(f"  Scanning external bribe: {gauge['external_bribe'][:20]}...")
            rewards = get_bribe_rewards(gauge['external_bribe'], "external", gauge['address'])
            if rewards:
                for r in rewards:
                    usd_str = f"${r['usd_value']:.2f}" if r['usd_value'] else "No price"
                    console.print(f"    • {r['amount']:.4f} {r['token_symbol']} ({usd_str})")
            else:
                console.print(f"    [dim](no rewards)[/dim]")
            all_rewards.extend(rewards)
        
        time.sleep(0.3)
    
    console.print(f"\n[green]✓ Found {len(all_rewards)} reward tokens across {len(gauge_details)} gauges[/green]")
    return all_rewards

def display_summary(gauge_details, all_rewards):
    """Display test summary."""
    console.print("\n[bold cyan]Test Summary[/bold cyan]")
    console.print("=" * 100)
    
    # Calculate totals
    total_internal_usd = sum(r['usd_value'] for r in all_rewards if r['usd_value'] and r['bribe_type'] == 'internal')
    total_external_usd = sum(r['usd_value'] for r in all_rewards if r['usd_value'] and r['bribe_type'] == 'external')
    total_votes = sum(g['current_votes'] for g in gauge_details)
    
    internal_count = sum(1 for r in all_rewards if r['bribe_type'] == 'internal')
    external_count = sum(1 for r in all_rewards if r['bribe_type'] == 'external')
    
    table = Table(show_header=False, show_lines=False)
    table.add_column("Metric", style="cyan", width=40)
    table.add_column("Value", style="green", width=30)
    
    table.add_row("[bold]Test Scope[/bold]", "")
    table.add_row("  Gauges Tested", f"{len(gauge_details)}")
    table.add_row("  Total Current Votes", f"{total_votes:,}")
    table.add_row("", "")
    table.add_row("[bold]Rewards Found[/bold]", "")
    table.add_row("  Internal Reward Tokens", f"{internal_count}")
    table.add_row("  External Reward Tokens", f"{external_count}")
    table.add_row("  Internal Bribes (USD)", f"${total_internal_usd:,.2f}")
    table.add_row("  External Bribes (USD)", f"${total_external_usd:,.2f}")
    table.add_row("  [bold]Total (USD)[/bold]", f"[bold]${total_internal_usd + total_external_usd:,.2f}[/bold]")
    
    console.print(table)

def main():
    """Main execution."""
    console.print(Panel.fit(
        "[bold cyan]Hydrex Vote Optimizer - Data Collection TEST[/bold cyan]\n"
        f"Testing complete flow with {TEST_GAUGE_LIMIT} gauges",
        border_style="yellow"
    ))
    
    start_time = time.time()
    
    # Step 1: Get test gauges
    gauges = get_all_gauges()
    if not gauges:
        console.print("[red]Failed to fetch gauges. Exiting.[/red]")
        return
    
    # Step 2: Map gauges to pools and bribes
    gauge_details = map_gauges(gauges)
    if not gauge_details:
        console.print("[red]Failed to map gauges. Exiting.[/red]")
        return
    
    # Step 3: Scan bribe contracts
    all_rewards = scan_bribes(gauge_details)
    
    # Display summary
    display_summary(gauge_details, all_rewards)
    
    elapsed = time.time() - start_time
    
    console.print(f"\n[bold green]✓ Test run complete in {elapsed:.1f} seconds![/bold green]")

if __name__ == "__main__":
    main()
