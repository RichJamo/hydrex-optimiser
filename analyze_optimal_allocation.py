#!/usr/bin/env python3
"""
Analyze collected data and calculate optimal vote allocation.
Target: 5-10 pools with 1,530,896 voting power.
"""

import sqlite3
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
import math

console = Console()

DATABASE_PATH = "data.db"
YOUR_VOTING_POWER = 1_530_896
MIN_POOLS = 5
MAX_POOLS = 10

console.print(Panel.fit(
    "[bold cyan]Optimal Vote Allocation Analysis[/bold cyan]\n"
    f"Voting Power: {YOUR_VOTING_POWER:,}  |  Target: {MIN_POOLS}-{MAX_POOLS} pools",
    border_style="cyan"
))

# Connect to database
conn = sqlite3.connect(DATABASE_PATH)
cursor = conn.cursor()

# Get current epoch
cursor.execute("SELECT MAX(epoch) FROM bribes")
current_epoch = cursor.fetchone()[0]
console.print(f"\n[cyan]Analyzing epoch: {current_epoch}[/cyan]\n")

# Query all gauges with their bribes
query = """
    SELECT 
        g.address as gauge_address,
        g.pool as pool_address,
        g.current_votes,
        g.is_alive,
        COALESCE(SUM(CASE WHEN b.bribe_type = 'internal' THEN b.usd_value ELSE 0 END), 0) as internal_usd,
        COALESCE(SUM(CASE WHEN b.bribe_type = 'external' THEN b.usd_value ELSE 0 END), 0) as external_usd,
        COALESCE(SUM(b.usd_value), 0) as total_usd
    FROM gauges g
    LEFT JOIN bribes b ON b.gauge_address = g.address AND b.epoch = ?
    WHERE g.is_alive = 1 OR g.is_alive IS NULL
    GROUP BY g.address, g.pool, g.current_votes, g.is_alive
    HAVING total_usd > 0
    ORDER BY total_usd DESC
"""

cursor.execute(query, (current_epoch,))
results = cursor.fetchall()

console.print(f"[green]Found {len(results)} gauges with bribes[/green]\n")

# Calculate metrics for each gauge
gauges_data = []
for row in results:
    gauge_addr, pool_addr, votes_str, is_alive, internal_usd, external_usd, total_usd = row
    
    if total_usd <= 0:
        continue
    
    # Parse votes (stored as TEXT)
    try:
        current_votes = int(votes_str) if votes_str else 0
    except:
        current_votes = 0
    
    # Calculate expected return if we add our votes
    # Formula: our_share = our_votes / (current_votes + our_votes)
    # expected_return = total_bribes * our_share
    
    new_total_votes = current_votes + YOUR_VOTING_POWER
    our_share = YOUR_VOTING_POWER / new_total_votes if new_total_votes > 0 else 0
    expected_return = total_usd * our_share
    
    # Calculate ROI per 1000 votes
    roi_per_1k = (expected_return / YOUR_VOTING_POWER) * 1000
    
    gauges_data.append({
        'gauge': gauge_addr,
        'pool': pool_addr,
        'current_votes': current_votes,
        'internal_usd': internal_usd,
        'external_usd': external_usd,
        'total_usd': total_usd,
        'expected_return': expected_return,
        'roi_per_1k': roi_per_1k,
        'our_share': our_share * 100
    })

# Sort by expected return
gauges_data.sort(key=lambda x: x['expected_return'], reverse=True)

# Show top 20 opportunities
console.print("[bold cyan]Top 20 Pools by Expected Return[/bold cyan]")
console.print("=" * 120)

table = Table(show_header=True, header_style="bold cyan")
table.add_column("#", width=3, style="dim")
table.add_column("Pool Address", width=22)
table.add_column("Current Votes", width=20, justify="right")
table.add_column("Total Bribes", width=15, justify="right")
table.add_column("Expected Return", width=15, justify="right", style="bold green")
table.add_column("ROI per 1K", width=12, justify="right")
table.add_column("Your Share", width=10, justify="right")

for i, g in enumerate(gauges_data[:20], 1):
    table.add_row(
        str(i),
        g['pool'][:20] + "...",
        f"{g['current_votes']:,}",
        f"${g['total_usd']:,.2f}",
        f"${g['expected_return']:,.2f}",
        f"${g['roi_per_1k']:.3f}",
        f"{g['our_share']:.2f}%"
    )

console.print(table)

# Calculate optimal allocation strategies
console.print("\n[bold cyan]Allocation Strategies[/bold cyan]")
console.print("=" * 120)

# Strategy 1: Single best pool
single_best = gauges_data[0]
console.print(f"\n[bold yellow]Strategy 1: All votes to single best pool[/bold yellow]")
console.print(f"  Pool: {single_best['pool']}")
console.print(f"  Expected return: ${single_best['expected_return']:,.2f}")
console.print(f"  Baseline comparison: ${954.53:.2f} (last vote)")
console.print(f"  Improvement: {((single_best['expected_return'] / 954.53) - 1) * 100:+.1f}%")

# Strategy 2: Top N pools (equal split)
for n in [MIN_POOLS, 7, MAX_POOLS]:
    console.print(f"\n[bold yellow]Strategy {n - 2}: Split equally across top {n} pools[/bold yellow]")
    
    votes_per_pool = YOUR_VOTING_POWER // n
    total_expected = 0
    
    for i, g in enumerate(gauges_data[:n], 1):
        new_total = g['current_votes'] + votes_per_pool
        our_share = votes_per_pool / new_total
        pool_return = g['total_usd'] * our_share
        total_expected += pool_return
        
        if i <= 3:  # Show details for top 3
            console.print(f"  {i}. {g['pool'][:30]}... → ${pool_return:.2f}")
    
    if n > 3:
        console.print(f"  ... and {n-3} more pools")
    
    console.print(f"  [bold]Total expected return: ${total_expected:,.2f}[/bold]")
    console.print(f"  Baseline comparison: ${954.53:.2f}")
    console.print(f"  Improvement: {((total_expected / 954.53) - 1) * 100:+.1f}%")

# Strategy 3: Weighted by ROI (proportional to expected return)
console.print(f"\n[bold yellow]Strategy: Proportional allocation by expected return (top {MAX_POOLS})[/bold yellow]")
top_n = gauges_data[:MAX_POOLS]
total_weight = sum(g['expected_return'] for g in top_n)

total_expected = 0
for i, g in enumerate(top_n, 1):
    weight = g['expected_return'] / total_weight
    our_votes = int(YOUR_VOTING_POWER * weight)
    
    new_total = g['current_votes'] + our_votes
    our_share = our_votes / new_total
    pool_return = g['total_usd'] * our_share
    total_expected += pool_return
    
    if i <= 5:
        console.print(f"  {i}. {g['pool'][:30]}... → {our_votes:,} votes → ${pool_return:.2f}")

console.print(f"  [bold]Total expected return: ${total_expected:,.2f}[/bold]")
console.print(f"  Baseline comparison: ${954.53:.2f}")
console.print(f"  Improvement: {((total_expected / 954.53) - 1) * 100:+.1f}%")

# Summary recommendation
console.print("\n" + "=" * 120)
console.print("[bold green]RECOMMENDATION[/bold green]")
console.print("=" * 120)

best_strategy_return = max(
    single_best['expected_return'],
    total_expected
)

console.print(f"\nBased on current bribe levels and vote distribution:")
console.print(f"  • Your last vote baseline: [bold]${954.53:.2f}[/bold]")
console.print(f"  • Best estimated return: [bold green]${best_strategy_return:,.2f}[/bold green]")
console.print(f"  • Potential improvement: [bold]+{((best_strategy_return / 954.53) - 1) * 100:.1f}%[/bold]")

console.print(f"\n[yellow]⚠️  Important notes:[/yellow]")
console.print(f"  • These calculations assume current bribe levels remain constant")
console.print(f"  • Other voters may vote before you, changing vote shares")
console.print(f"  • Internal bribes (fees) will grow as trading continues until epoch flip")
console.print(f"  • Consider voting closer to Wednesday 00:00 UTC for more certainty")

conn.close()
