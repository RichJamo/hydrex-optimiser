#!/usr/bin/env python3
"""
Complete data collection for vote optimization:
1. Query all gauges from VoterV5
2. Map gauges → pools → bribe contracts
3. Scan bribe contracts for available rewards
4. Get current vote weights
5. Store in database for optimization
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

# Setup Web3
w3 = Web3(Web3.HTTPProvider(RPC_URL))
console.print(f"[cyan]Connected to Base: {w3.is_connected()}[/cyan]\n")

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

# Initialize database
def init_database():
    """Create tables if they don't exist and add missing columns."""
    conn = sqlite3.connect(DATABASE_PATH)
    cursor = conn.cursor()
    
    # Gauges table - ensure it exists with all needed columns
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS gauges (
            address VARCHAR PRIMARY KEY,
            pool VARCHAR,
            internal_bribe VARCHAR,
            external_bribe VARCHAR,
            is_alive BOOLEAN,
            created_at INTEGER
        )
    """)
    
    # Add missing columns to gauges table if they don't exist
    cursor.execute("PRAGMA table_info(gauges)")
    columns = {col[1] for col in cursor.fetchall()}
    
    if 'current_votes' not in columns:
        cursor.execute("ALTER TABLE gauges ADD COLUMN current_votes TEXT DEFAULT '0'")
        console.print("[yellow]Added current_votes column to gauges table[/yellow]")
    
    if 'last_updated' not in columns:
        cursor.execute("ALTER TABLE gauges ADD COLUMN last_updated INTEGER")
        console.print("[yellow]Added last_updated column to gauges table[/yellow]")
    
    # Bribes table - use existing schema (historical approach)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS bribes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            epoch INTEGER,
            bribe_contract VARCHAR,
            reward_token VARCHAR,
            amount FLOAT,
            timestamp INTEGER,
            indexed_at INTEGER,
            amount_wei TEXT
        )
    """)
    
    # Add gauge_address column if it doesn't exist (to link bribes to gauges)
    cursor.execute("PRAGMA table_info(bribes)")
    columns = {col[1] for col in cursor.fetchall()}
    
    if 'gauge_address' not in columns:
        cursor.execute("ALTER TABLE bribes ADD COLUMN gauge_address VARCHAR")
        console.print("[yellow]Added gauge_address column to bribes table[/yellow]")
    
    if 'bribe_type' not in columns:
        cursor.execute("ALTER TABLE bribes ADD COLUMN bribe_type VARCHAR")
        console.print("[yellow]Added bribe_type column to bribes table[/yellow]")
    
    if 'token_symbol' not in columns:
        cursor.execute("ALTER TABLE bribes ADD COLUMN token_symbol VARCHAR")
        console.print("[yellow]Added token_symbol column to bribes table[/yellow]")
    
    if 'token_decimals' not in columns:
        cursor.execute("ALTER TABLE bribes ADD COLUMN token_decimals INTEGER")
        console.print("[yellow]Added token_decimals column to bribes table[/yellow]")
    
    if 'usd_price' not in columns:
        cursor.execute("ALTER TABLE bribes ADD COLUMN usd_price FLOAT")
        console.print("[yellow]Added usd_price column to bribes table[/yellow]")
    
    if 'usd_value' not in columns:
        cursor.execute("ALTER TABLE bribes ADD COLUMN usd_value FLOAT")
        console.print("[yellow]Added usd_value column to bribes table[/yellow]")
    
    conn.commit()
    conn.close()
    console.print("[green]✓ Database initialized[/green]")

def get_all_gauges():
    """Query VoterV5 for all gauge addresses."""
    console.print("\n[bold cyan]Step 1: Querying All Gauges from VoterV5[/bold cyan]")
    console.print("=" * 100)
    
    try:
        # Get gauge count
        gauge_count = voter.functions.length().call()
        console.print(f"[green]Total gauges: {gauge_count}[/green]")
        
        gauges = []
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
            console=console
        ) as progress:
            task = progress.add_task(f"Fetching {gauge_count} gauges...", total=gauge_count)
            
            for i in range(gauge_count):
                try:
                    pool_addr = voter.functions.pools(i).call()
                    gauge_addr = voter.functions.gauges(pool_addr).call()
                    
                    # Skip if no gauge exists for this pool
                    if gauge_addr == "0x0000000000000000000000000000000000000000":
                        progress.update(task, advance=1)
                        continue
                        
                    gauges.append(gauge_addr)
                    progress.update(task, advance=1)
                    
                    # Rate limiting
                    if i % 50 == 0 and i > 0:
                        time.sleep(0.5)
                        
                except Exception as e:
                    console.print(f"[yellow]Warning: Error fetching gauge {i}: {e}[/yellow]")
                    progress.update(task, advance=1)
        
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

def map_all_gauges(gauges):
    """Map all gauges to their pools and bribe contracts."""
    console.print("\n[bold cyan]Step 2: Mapping Gauges → Pools → Bribe Contracts[/bold cyan]")
    console.print("=" * 100)
    
    gauge_details = []
    
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
        console=console
    ) as progress:
        task = progress.add_task(f"Mapping {len(gauges)} gauges...", total=len(gauges))
        
        for gauge_addr in gauges:
            details = get_gauge_details(gauge_addr)
            if details:
                gauge_details.append(details)
            
            progress.update(task, advance=1)
            
            # Rate limiting
            if len(gauge_details) % 20 == 0 and len(gauge_details) > 0:
                time.sleep(0.3)
    
    console.print(f"[green]✓ Mapped {len(gauge_details)} gauges[/green]")
    return gauge_details

def store_gauges(gauge_details):
    """Store gauge details in database."""
    console.print("\n[bold cyan]Step 3: Storing Gauge Data[/bold cyan]")
    console.print("=" * 100)
    
    conn = sqlite3.connect(DATABASE_PATH)
    cursor = conn.cursor()
    
    timestamp = int(time.time())
    
    for gauge in gauge_details:
        # Check if gauge exists
        cursor.execute("SELECT address FROM gauges WHERE address = ?", (gauge['address'],))
        exists = cursor.fetchone() is not None
        
        # Store votes as string to handle huge numbers
        votes_str = str(gauge['current_votes'])
        
        if exists:
            # Update existing gauge
            cursor.execute("""
                UPDATE gauges 
                SET pool = ?, internal_bribe = ?, external_bribe = ?, 
                    is_alive = ?, current_votes = ?, last_updated = ?
                WHERE address = ?
            """, (
                gauge['pool'],
                gauge['internal_bribe'],
                gauge['external_bribe'],
                gauge['is_alive'],
                votes_str,
                timestamp,
                gauge['address']
            ))
        else:
            # Insert new gauge
            cursor.execute("""
                INSERT INTO gauges 
                (address, pool, internal_bribe, external_bribe, is_alive, current_votes, last_updated, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                gauge['address'],
                gauge['pool'],
                gauge['internal_bribe'],
                gauge['external_bribe'],
                gauge['is_alive'],
                votes_str,
                timestamp,
                timestamp
            ))
    
    conn.commit()
    
    # Get statistics
    cursor.execute("SELECT COUNT(*) FROM gauges WHERE is_alive = 1 OR is_alive IS NULL")
    alive_count = cursor.fetchone()[0]
    
    cursor.execute("SELECT COUNT(*) FROM gauges WHERE current_votes IS NOT NULL AND current_votes != '0'")
    voted_count = cursor.fetchone()[0]
    
    conn.close()
    
    console.print(f"[green]✓ Stored {len(gauge_details)} gauges[/green]")
    console.print(f"  • Active gauges: {alive_count}")
    console.print(f"  • Gauges with votes: {voted_count}")

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

def scan_all_bribes():
    """Scan all bribe contracts for available rewards."""
    console.print("\n[bold cyan]Step 4: Scanning Bribe Contracts for Rewards[/bold cyan]")
    console.print("=" * 100)
    
    conn = sqlite3.connect(DATABASE_PATH)
    cursor = conn.cursor()
    
    # Get current epoch
    current_epoch = get_current_epoch()
    timestamp = int(time.time())
    
    console.print(f"Current epoch: {current_epoch} ({datetime.fromtimestamp(current_epoch)})")
    
    # Get all active gauges
    cursor.execute("""
        SELECT address, pool, internal_bribe, external_bribe 
        FROM gauges 
        WHERE is_alive = 1 OR is_alive IS NULL
    """)
    gauges = cursor.fetchall()
    
    console.print(f"Scanning {len(gauges)} active gauges...")
    
    # Delete existing data for current epoch (we're refreshing it)
    cursor.execute("DELETE FROM bribes WHERE epoch = ?", (current_epoch,))
    console.print(f"[yellow]Cleared existing data for epoch {current_epoch}[/yellow]")
    
    all_rewards = []
    
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
        console=console
    ) as progress:
        task = progress.add_task(f"Scanning bribe contracts...", total=len(gauges) * 2)
        
        for gauge_addr, pool, internal_bribe, external_bribe in gauges:
            # Scan internal bribe
            if internal_bribe and internal_bribe != "0x0000000000000000000000000000000000000000":
                rewards = get_bribe_rewards(internal_bribe, "internal", gauge_addr)
                all_rewards.extend(rewards)
            progress.update(task, advance=1)
            
            # Scan external bribe
            if external_bribe and external_bribe != "0x0000000000000000000000000000000000000000":
                rewards = get_bribe_rewards(external_bribe, "external", gauge_addr)
                all_rewards.extend(rewards)
            progress.update(task, advance=1)
            
            # Rate limiting
            if len(all_rewards) % 40 == 0 and len(all_rewards) > 0:
                time.sleep(0.3)
    
    # Store rewards using historical schema
    for reward in all_rewards:
        cursor.execute("""
            INSERT INTO bribes 
            (epoch, bribe_contract, reward_token, amount, timestamp, indexed_at, 
             amount_wei, gauge_address, bribe_type, token_symbol, token_decimals, usd_price, usd_value)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            current_epoch,
            reward['bribe_contract'],
            reward['reward_token'],
            reward['amount'],
            timestamp,
            timestamp,
            reward['amount_wei'],
            reward['gauge_address'],
            reward['bribe_type'],
            reward['token_symbol'],
            reward['token_decimals'],
            reward.get('usd_price'),
            reward.get('usd_value')
        ))
    
    conn.commit()
    conn.close()
    
    console.print(f"[green]✓ Found {len(all_rewards)} reward tokens across all bribes[/green]")
    console.print(f"[green]✓ Stored data for epoch {current_epoch}[/green]")
    
    return all_rewards

def generate_summary():
    """Generate summary statistics."""
    console.print("\n[bold cyan]Summary Statistics[/bold cyan]")
    console.print("=" * 100)
    
    conn = sqlite3.connect(DATABASE_PATH)
    cursor = conn.cursor()
    
    current_epoch = get_current_epoch()
    
    # Gauge stats
    cursor.execute("SELECT COUNT(*) FROM gauges")
    total_gauges = cursor.fetchone()[0]
    
    cursor.execute("SELECT COUNT(*) FROM gauges WHERE is_alive = 1 OR is_alive IS NULL")
    active_gauges = cursor.fetchone()[0]
    
    cursor.execute("SELECT COUNT(*) FROM gauges WHERE current_votes IS NOT NULL AND current_votes != '0'")
    voted_gauges = cursor.fetchone()[0]
    
    # Sum votes - need to cast TEXT to INTEGER for summation
    cursor.execute("SELECT SUM(CAST(current_votes AS INTEGER)) FROM gauges WHERE current_votes IS NOT NULL")
    total_votes = cursor.fetchone()[0] or 0
    
    # Bribe stats for current epoch
    cursor.execute("SELECT COUNT(*) FROM bribes WHERE epoch = ? AND bribe_type = 'internal'", (current_epoch,))
    internal_rewards = cursor.fetchone()[0]
    
    cursor.execute("SELECT COUNT(*) FROM bribes WHERE epoch = ? AND bribe_type = 'external'", (current_epoch,))
    external_rewards = cursor.fetchone()[0]
    
    cursor.execute("""
        SELECT COUNT(DISTINCT gauge_address) 
        FROM bribes 
        WHERE epoch = ? AND bribe_type = 'internal' AND amount > 0
    """, (current_epoch,))
    gauges_with_internal = cursor.fetchone()[0]
    
    cursor.execute("""
        SELECT COUNT(DISTINCT gauge_address) 
        FROM bribes 
        WHERE epoch = ? AND bribe_type = 'external' AND amount > 0
    """, (current_epoch,))
    gauges_with_external = cursor.fetchone()[0]
    
    # USD value stats
    cursor.execute("""
        SELECT 
            SUM(CASE WHEN bribe_type = 'internal' THEN usd_value ELSE 0 END) as internal_usd,
            SUM(CASE WHEN bribe_type = 'external' THEN usd_value ELSE 0 END) as external_usd,
            COUNT(CASE WHEN usd_value IS NULL THEN 1 END) as missing_prices,
            COUNT(*) as total_rewards
        FROM bribes 
        WHERE epoch = ?
    """, (current_epoch,))
    usd_stats = cursor.fetchone()
    internal_usd = usd_stats[0] or 0
    external_usd = usd_stats[1] or 0
    missing_prices = usd_stats[2] or 0
    total_rewards = usd_stats[3] or 0
    
    conn.close()
    
    # Display table
    table = Table(show_header=False, show_lines=False)
    table.add_column("Metric", style="cyan", width=40)
    table.add_column("Value", style="green", width=30)
    
    table.add_row("[bold]Gauge Statistics[/bold]", "")
    table.add_row("  Total Gauges", f"{total_gauges}")
    table.add_row("  Active Gauges", f"{active_gauges}")
    table.add_row("  Gauges with Votes", f"{voted_gauges}")
    table.add_row("  Total Votes Cast", f"{total_votes:,}")
    table.add_row("", "")
    table.add_row(f"[bold]Bribe Statistics (Epoch {current_epoch})[/bold]", "")
    table.add_row("  Internal Reward Tokens", f"{internal_rewards}")
    table.add_row("  External Reward Tokens", f"{external_rewards}")
    table.add_row("  Gauges with Internal Bribes", f"{gauges_with_internal}")
    table.add_row("  Gauges with External Bribes", f"{gauges_with_external}")
    table.add_row("", "")
    table.add_row("[bold]USD Value Statistics[/bold]", "")
    table.add_row("  Total Internal Bribes (USD)", f"${internal_usd:,.2f}")
    table.add_row("  Total External Bribes (USD)", f"${external_usd:,.2f}")
    table.add_row("  [bold]Total Available Bribes (USD)[/bold]", f"[bold]${internal_usd + external_usd:,.2f}[/bold]")
    table.add_row("  Tokens with Prices", f"{total_rewards - missing_prices} / {total_rewards}")
    if missing_prices > 0:
        table.add_row("  [yellow]Tokens Missing Prices[/yellow]", f"[yellow]{missing_prices}[/yellow]")
    
    console.print(table)

def main():
    """Main execution."""
    console.print(Panel.fit(
        "[bold cyan]Hydrex Vote Optimizer - Data Collection[/bold cyan]\n"
        "Collecting all gauge, pool, and bribe data for optimization",
        border_style="cyan"
    ))
    
    start_time = time.time()
    
    # Initialize database
    init_database()
    
    # Step 1: Get all gauges
    gauges = get_all_gauges()
    if not gauges:
        console.print("[red]Failed to fetch gauges. Exiting.[/red]")
        return
    
    # Step 2: Map gauges to pools and bribe contracts
    gauge_details = map_all_gauges(gauges)
    
    # Step 3: Store in database
    store_gauges(gauge_details)
    
    # Step 4: Scan bribe contracts
    scan_all_bribes()
    
    # Generate summary
    generate_summary()
    
    elapsed = time.time() - start_time
    
    console.print(f"\n[bold green]✓ Data collection complete in {elapsed:.1f} seconds![/bold green]")
    console.print(f"\n[cyan]Database updated:[/cyan]")
    console.print(f"  • All 291 gauges mapped with current votes")
    console.print(f"  • Bribe balances scanned for current epoch")
    console.print(f"  • USD values calculated using CoinGecko prices")
    console.print(f"\n[cyan]Next steps:[/cyan]")
    console.print(f"  1. Run optimization to find best vote allocation")
    console.print(f"  2. Compare with your last vote ($954.53 baseline)")
    console.print(f"  3. Generate voting recommendation for tonight")

if __name__ == "__main__":
    main()
