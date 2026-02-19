#!/usr/bin/env python3
"""
Reconcile actual rewards received against predicted bribes from our data collection.
"""

import sqlite3
from rich.console import Console
from rich.table import Table
from rich.panel import Panel

console = Console()

DATABASE_PATH = "data.db"

# Your actual votes and rewards
YOUR_VOTES = {
    "0x51f0b932855986b0e621c9d4db6eee1f4644d3d2": 4200,    # HYDX/USDC
    "0xef96ec76eeb36584fc4922e9fa268e0780170f33": 2400,    # kVCM/USDC
    "0x82dbe18346a8656dbb5e76f74bf3ae279cc16b29": 3400,    # WETH/USDC
}

ACTUAL_REWARDS = {
    "HYDX": {"fees": 670.8831, "usd": 45.28},
    "USDC_fees": {"amount": 155.5443, "usd": 155.54},
    "USDC_bribes": {"amount": 171.9885, "usd": 171.98},
    "oHYDX": {"amount": 0.0072748, "usd": 0.01},
    "WETH": {"fees": 0.046357, "usd": 91.08},
    "kVCM_fees": {"amount": 5.5658, "usd": 0.50},
    "kVCM_bribes": {"amount": 1615.61, "usd": 144.04},
}

TOTAL_ACTUAL = 45.28 + 155.54 + 171.98 + 0.01 + 91.08 + 0.50 + 144.04
WETH_PER_1K = (TOTAL_ACTUAL / 10000) * 1000  # Your voting power is 10k approx

console.print(Panel.fit(
    "[bold cyan]Vote Reward Reconciliation[/bold cyan]\n"
    "Comparing predicted vs actual rewards",
    border_style="cyan"
))

# Connect to database
conn = sqlite3.connect(DATABASE_PATH)
cursor = conn.cursor()

cursor.execute("SELECT MAX(epoch) FROM bribes")
current_epoch = cursor.fetchone()[0]

console.print(f"\n[cyan]Current epoch: {current_epoch}[/cyan]\n")

# Get predicted bribes for your 3 pools
console.print("[bold cyan]Predicted vs Actual Analysis[/bold cyan]")
console.print("=" * 120)

table = Table(show_header=True, header_style="bold cyan")
table.add_column("Pool", width=30)
table.add_column("Your Votes", width=15, justify="right")
table.add_column("Predicted Total", width=18, justify="right", style="cyan")
table.add_column("Internal (Fees)", width=18, justify="right", style="yellow")
table.add_column("External (Bribes)", width=18, justify="right", style="green")
table.add_column("Your Share %", width=12, justify="right")

total_predicted = 0
total_internal = 0
total_external = 0

for pool_addr, your_votes in YOUR_VOTES.items():
    pool_lower = pool_addr.lower()
    
    # Get gauge for this pool
    cursor.execute("""
        SELECT address FROM gauges WHERE pool = ?
    """, (pool_lower,))
    
    gauge_result = cursor.fetchone()
    if not gauge_result:
        console.print(f"[red]Could not find gauge for pool {pool_addr[:10]}...[/red]")
        continue
    
    gauge_addr = gauge_result[0]
    
    # Get bribes for this gauge
    cursor.execute("""
        SELECT 
            bribe_type,
            COALESCE(SUM(usd_value), 0) as total_usd,
            COUNT(*) as token_count
        FROM bribes 
        WHERE gauge_address = ? AND epoch = ?
        GROUP BY bribe_type
    """, (gauge_addr, current_epoch))
    
    bribe_results = cursor.fetchall()
    
    internal_usd = 0
    external_usd = 0
    
    for bribe_type, usd_value, count in bribe_results:
        if bribe_type == 'internal':
            internal_usd = usd_value
        elif bribe_type == 'external':
            external_usd = usd_value
    
    total_predicted_for_pool = internal_usd + external_usd
    
    # Get current votes on this pool to calculate our share
    cursor.execute("""
        SELECT current_votes FROM gauges WHERE pool = ?
    """, (pool_lower,))
    
    votes_result = cursor.fetchone()
    if votes_result:
        try:
            current_votes = int(votes_result[0]) if votes_result[0] else 0
        except:
            current_votes = 0
    else:
        current_votes = 0
    
    new_total = current_votes + your_votes
    our_share_pct = (your_votes / new_total * 100) if new_total > 0 else 0
    
    # Pair name lookup
    if pool_addr.lower() == "0x51f0b932855986b0e621c9d4db6eee1f4644d3d2":
        pair_name = "HYDX/USDC"
    elif pool_addr.lower() == "0xef96ec76eeb36584fc4922e9fa268e0780170f33":
        pair_name = "kVCM/USDC"
    else:
        pair_name = "WETH/USDC"
    
    table.add_row(
        pair_name,
        str(your_votes),
        f"${total_predicted_for_pool:,.2f}",
        f"${internal_usd:,.2f}",
        f"${external_usd:,.2f}",
        f"{our_share_pct:.2f}%"
    )
    
    total_predicted += total_predicted_for_pool
    total_internal += internal_usd
    total_external += external_usd

console.print(table)

console.print(f"\n[bold]Summary:[/bold]")
console.print(f"  Total predicted bribes: ${total_predicted:,.2f}")
console.print(f"    - Internal (fees): ${total_internal:,.2f}")
console.print(f"    - External (bribes): ${total_external:,.2f}")

console.print(f"\n[bold]Actual rewards received: ${TOTAL_ACTUAL:,.2f}[/bold]")

diff = TOTAL_ACTUAL - total_predicted
diff_pct = (diff / total_predicted * 100) if total_predicted > 0 else 0

console.print(f"\n[bold cyan]Reconciliation:[/bold cyan]")
if diff > 0:
    console.print(f"  Actual exceeded prediction by ${diff:,.2f} ({diff_pct:+.1f}%)")
    console.print(f"  [yellow]This suggests:[/yellow]")
    console.print(f"    • Internal bribes grew between data collection and epoch flip")
    console.print(f"    • Trading volume increased, generating more fees")
    console.print(f"    • Or external bribes were higher than reflected")
elif diff < 0:
    console.print(f"  Actual came in ${abs(diff):,.2f} below prediction ({diff_pct:.1f}%)")
    console.print(f"  [yellow]This could mean:[/yellow]")
    console.print(f"    • Vote dilution (other voters got in)")
    console.print(f"    • Predicted bribes included unclaimed amounts")
else:
    console.print(f"  [green]Perfect match![/green]")

console.print(f"\n[bold cyan]Per-1K vote calculation:[/bold cyan]")
console.print(f"  Your total votes: {sum(YOUR_VOTES.values()):,}")
console.print(f"  Total actual reward: ${TOTAL_ACTUAL:,.2f}")
console.print(f"  [bold]Per-1K reward: ${(TOTAL_ACTUAL / sum(YOUR_VOTES.values())) * 1000:,.2f}[/bold]")
console.print(f"  Comparison to last vote baseline ($0.624 per 1K): {((TOTAL_ACTUAL / sum(YOUR_VOTES.values())) * 1000 / 624):+.1f}x")

# Breakdown by reward type
console.print(f"\n[bold cyan]Reward breakdown:[/bold cyan]")
internal_fees = 45.28 + 155.54 + 91.08 + 0.50  # HYDX, USDC, WETH, kVCM fees
external_bribes = 171.98 + 0.01 + 144.04  # USDC, oHYDX, kVCM bribes

console.print(f"  Internal (fees): ${internal_fees:,.2f} ({internal_fees/TOTAL_ACTUAL*100:.1f}%)")
console.print(f"  External (bribes): ${external_bribes:,.2f} ({external_bribes/TOTAL_ACTUAL*100:.1f}%)")

conn.close()
