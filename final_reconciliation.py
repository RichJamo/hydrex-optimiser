#!/usr/bin/env python3
"""
Final reconciliation: Compare predicted vs actual rewards received.
"""

import json
from rich.console import Console
from rich.table import Table
from rich.panel import Panel

console = Console()

# Load on-chain vote data
with open("actual_votes_on_chain.json") as f:
    votes_data = json.load(f)

# Load closed epoch bribe data
with open("closed_epoch_data.json") as f:
    epoch_data = json.load(f)

# Your actual rewards
ACTUAL_REWARDS = {
    "HYDX/USDC": 45.28 + 171.98,  # HYDX fees + some USDC bribes
    "kVCM/USDC": 144.54,          # kVCM bribes
    "WETH/USDC": 91.08,           # WETH fees  
}

console.print(Panel.fit(
    "[bold cyan]Final Reconciliation[/bold cyan]\n"
    "Predicted vs Actual Rewards",
    border_style="cyan"
))

table = Table(show_header=True, header_style="bold cyan", title="Reward Analysis")
table.add_column("Pool", width=15)
table.add_column("Your Votes", width=15, justify="right")
table.add_column("Total Votes", width=15, justify="right")
table.add_column("Your Share %", width=12, justify="right")
table.add_column("Total Bribes", width=15, justify="right", style="green")
table.add_column("Predicted $", width=15, justify="right", style="cyan")
table.add_column("Actual $", width=15, justify="right", style="yellow")
table.add_column("Match %", width=12, justify="right", style="magenta")

total_predicted = 0
total_actual = 0

for pool_name, vote_info in votes_data["pools"].items():
    your_votes = vote_info["your_votes"]
    total_votes = vote_info["total_votes"]
    share_pct = vote_info["your_share_pct"]
    
    # Find bribes for this pool
    bribes = 0
    for pool_addr, analysis in epoch_data["pools_analysis"].items():
        if (pool_addr == "0x51f0b932855986b0e621c9d4db6eee1f4644d3d2" and pool_name == "HYDX/USDC") or \
           (pool_addr == "0xef96ec76eeb36584fc4922e9fa268e0780170f33" and pool_name == "kVCM/USDC") or \
           (pool_addr == "0x82dbe18346a8656dbb5e76f74bf3ae279cc16b29" and pool_name == "WETH/USDC"):
            bribes = analysis["total_usd"]
            break
    
    # Predicted reward based on on-chain actual share
    predicted = bribes * (share_pct / 100)
    
    # Actual reward received
    actual = ACTUAL_REWARDS.get(pool_name, 0)
    
    # Match percentage
    match_pct = (actual / predicted * 100) if predicted > 0 else 0
    
    table.add_row(
        pool_name,
        f"{your_votes:,.0f}",
        f"{total_votes:,.0f}",
        f"{share_pct:.4f}%",
        f"${bribes:,.2f}",
        f"${predicted:,.2f}",
        f"${actual:,.2f}",
        f"{match_pct:.1f}%"
    )
    
    total_predicted += predicted
    total_actual += actual

console.print(table)

console.print(f"\n[bold cyan]Summary:[/bold cyan]")
console.print(f"Total predicted: ${total_predicted:,.2f}")
console.print(f"Total actual: ${total_actual:,.2f}")
console.print(f"Difference: ${total_actual - total_predicted:+,.2f}")
console.print(f"Match rate: {(total_actual / total_predicted * 100):.1f}%")

console.print(f"\n[bold cyan]Analysis:[/bold cyan]")

if abs(total_actual - total_predicted) <= total_predicted * 0.15:
    console.print("[green]✓ Excellent match! Predictions were accurate within 15%[/green]")
elif total_actual > total_predicted:
    console.print("[green]✓ You received MORE than predicted! Extra rewards came in.[/green]")
else:
    console.print("[yellow]◆ You received less than predicted. Possible reasons:[/yellow]")
    console.print("  • Bribe contracts paid out less than recorded value")
    console.print("  • Some bribes were not distributed")
    console.print("  • Token prices changed between data collection and distribution")

# Calculate per-vote value
per_vote = total_actual / votes_data["total_your_votes"]
console.print(f"\n[cyan]Per-vote value: ${per_vote:,.6f}/vote[/cyan]")

# Save final reconciliation
with open("final_reconciliation.json", "w") as f:
    json.dump({
        "total_predicted": total_predicted,
        "total_actual": total_actual,
        "match_rate": total_actual / total_predicted if total_predicted > 0 else 0,
        "pools": {
            name: {
                "predicted": votes_data["pools"][name]["your_share_pct"] * epoch_data["pools_analysis"][
                    [k for k, v in {"0x51f0b932855986b0e621c9d4db6eee1f4644d3d2": "HYDX/USDC",
                                   "0xef96ec76eeb36584fc4922e9fa268e0780170f33": "kVCM/USDC", 
                                   "0x82dbe18346a8656dbb5e76f74bf3ae279cc16b29": "WETH/USDC"}.items() if v == name][0]
                ]["total_usd"] / 100,
                "actual": ACTUAL_REWARDS.get(name, 0)
            }
            for name in votes_data["pools"].keys()
        }
    }, f, indent=2, default=str)

console.print(f"\n[green]Saved final reconciliation to final_reconciliation.json[/green]")
