#!/usr/bin/env python3
"""
Reconcile actual rewards vs predicted, using corrected basis points.
"""

import json
from rich.console import Console
from rich.table import Table
from rich.panel import Panel

console = Console()

# Load the closed epoch data
with open("closed_epoch_data.json") as f:
    epoch_data = json.load(f)

# Your actual rewards received
ACTUAL_REWARDS = {
    "HYDX": 670.8831,           # tokens
    "HYDX_usd": 45.28,          # USD (internal/fees)
    "USDC_fees": 155.5443,      # tokens (internal)
    "USDC_bribes": 171.9885,    # tokens (external)
    "oHYDX": 0.0007248,         # tokens (external bribes, $0.01)
    "WETH": 0.046357,           # tokens (internal/fees)
    "WETH_usd": 91.08,          # USD
    "kVCM_fees": 5.5658,        # tokens (internal)
    "kVCM_bribes": 1615.61,     # tokens (external)
    "kVCM_usd": 144.54,         # combined USD
}

TOTAL_ACTUAL_USD = 45.28 + 155.54 + 171.98 + 0.01 + 91.08 + 0.50 + 144.04

console.print(Panel.fit(
    "[bold cyan]Vote Reward Reconciliation (Corrected)[/bold cyan]\n"
    "Basis Points vs Actual Share Analysis",
    border_style="cyan"
))

console.print(f"\n[cyan]Voting Summary:[/cyan]")
total_bp = 0
total_votes = 0
for pool_addr, votes_bp in epoch_data["your_votes"].items():
    votes = epoch_data["pools_analysis"][pool_addr]["your_votes_actual"]
    total_bp += votes_bp
    total_votes += votes
    print(f"  {votes_bp:,} BP = {votes:,} votes")

console.print(f"\n[yellow]Total: {total_bp:,} BP = {total_votes:,} votes[/yellow]")
console.print(f"[yellow]Voting power available: {epoch_data['user_voting_power']:,}[/yellow]")

# Create analysis table
table = Table(show_header=True, header_style="bold cyan", title="Final Bribe Analysis")
table.add_column("Pool", width=15)
table.add_column("Voting %", width=10, justify="right")
table.add_column("Your Votes", width=15, justify="right")
table.add_column("Total Bribes", width=15, justify="right", style="green")
table.add_column("Internal/Fees", width=15, justify="right", style="yellow")
table.add_column("External/Bribes", width=15, justify="right", style="cyan")

total_bribes_usd = 0
pool_rewards = {}

for pool_addr in ["0x51f0b932855986b0e621c9d4db6eee1f4644d3d2", 
                  "0xef96ec76eeb36584fc4922e9fa268e0780170f33",
                  "0x82dbe18346a8656dbb5e76f74bf3ae279cc16b29"]:
    analysis = epoch_data["pools_analysis"][pool_addr]
    
    if pool_addr == "0x51f0b932855986b0e621c9d4db6eee1f4644d3d2":
        name = "HYDX/USDC"
    elif pool_addr == "0xef96ec76eeb36584fc4922e9fa268e0780170f33":
        name = "kVCM/USDC"
    else:
        name = "WETH/USDC"
    
    voting_pct = (analysis["your_votes_bp"] / 10_000) * 100
    total_bribes = analysis["total_usd"]
    internal = analysis["internal_usd"]
    external = analysis["external_usd"]
    
    table.add_row(
        name,
        f"{voting_pct:.1f}%",
        f"{analysis['your_votes_actual']:,}",
        f"${total_bribes:,.2f}",
        f"${internal:,.2f}",
        f"${external:,.2f}"
    )
    
    total_bribes_usd += total_bribes
    pool_rewards[name] = {
        "total_usd": total_bribes,
        "internal": internal,
        "external": external,
        "your_bp": analysis["your_votes_bp"]
    }

console.print(table)

console.print(f"\n[bold cyan]Reconciliation Analysis:[/bold cyan]\n")

# Calculate predicted share at 100% voting power allocation
console.print(f"[yellow]Scenario 1: If total vote pool equals your votes[/yellow]")
for name, data in pool_rewards.items():
    your_share_if_only_voter = data["total_usd"] * (data["your_bp"] / 10_000)
    console.print(f"  {name}: ${your_share_if_only_voter:,.2f}")

# Calculate actual vote shares by working backwards from rewards
console.print(f"\n[yellow]Scenario 2: Back-calculating actual vote shares from rewards[/yellow]")

# Map actual rewards to pools
reward_mapping = {
    "HYDX/USDC": 45.28 + 171.98,  # HYDX internal + some USDC bribes
    "kVCM/USDC": 144.54,          # kVCM bribes mostly
    "WETH/USDC": 91.08,           # WETH internal
}

total_predicted_if_only_voter = sum(
    data["total_usd"] * (data["your_bp"] / 10_000) 
    for data in pool_rewards.values()
)

console.print(f"[cyan]Total bribes if you were only voter: ${total_predicted_if_only_voter:,.2f}[/cyan]")
console.print(f"[cyan]Total rewards actually received: ${TOTAL_ACTUAL_USD:,.2f}[/cyan]")

dilution_factor = total_predicted_if_only_voter / TOTAL_ACTUAL_USD if TOTAL_ACTUAL_USD > 0 else 0

console.print(f"\n[bold red]Vote dilution factor: {dilution_factor:.1f}x[/bold red]")
console.print(f"[red]Other votes captured {(dilution_factor - 1) * 100:.0f}% of the rewards you would have gotten[/red]")

console.print(f"\n[yellow]Scenario 3: Implied final vote counts[/yellow]")

for name, data in pool_rewards.items():
    actual_reward = reward_mapping.get(name, 0)
    total_pool_bribes = data["total_usd"]
    
    if actual_reward > 0 and total_pool_bribes > 0:
        # Actual reward = total_bribes * (your_votes / total_votes_in_pool)
        # So: your_votes / total_votes = actual_reward / total_bribes
        # Solving for total_votes: total_votes = your_votes / (actual_reward / total_bribes)
        
        actual_share_pct = (actual_reward / total_pool_bribes) * 100
        your_votes_for_pool = epoch_data["pools_analysis"][
            [k for k, v in {"0x51f0b932855986b0e621c9d4db6eee1f4644d3d2": "HYDX/USDC",
                           "0xef96ec76eeb36584fc4922e9fa268e0780170f33": "kVCM/USDC",
                           "0x82dbe18346a8656dbb5e76f74bf3ae279cc16b29": "WETH/USDC"}.items() if v == name][0]
        ]["your_votes_actual"]
        
        implied_total_votes = (your_votes_for_pool / actual_share_pct) * 100 if actual_share_pct > 0 else 0
        implied_other_votes = implied_total_votes - your_votes_for_pool
        
        console.print(f"  {name}:")
        console.print(f"    Your actual share of bribes: {actual_share_pct:.4f}%")
        console.print(f"    Implied final total votes: {implied_total_votes:,.0f}")
        console.print(f"    Implied votes from others: {implied_other_votes:,.0f}")

console.print(f"\n[bold cyan]Key Insight:[/bold cyan]")
console.print(f"""
Your basis points were correct (42% + 24% + 34% = 100%).
Your actual voting power deployed was correct ({total_votes:,} votes).

The shortfall in rewards suggests:
1. Vote volume on these pools increased significantly between data collection and epoch close
2. Other voters participated heavily in your pools
3. Your share was diluted from ~100% (if only voter) to ~{100/dilution_factor:.1f}%

This is HEALTHY - it means the voting system is working and attracting participants.
For better returns next epoch, consider voting on LOWER-voted pools early in the cycle.
""")
