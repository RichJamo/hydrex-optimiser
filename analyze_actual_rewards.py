#!/usr/bin/env python3
"""
Reconciliation with actual rewards breakdown from the user.
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

# Your actual rewards received (from your claim)
ACTUAL_RECEIVED = {
    "HYDX": {"amount": 670.8831, "usd": 45.28},  # Fees
    "USDC_fees": {"amount": 155.5443, "usd": 155.54},  # Internal fees
    "USDC_bribes": {"amount": 171.9885, "usd": 171.98},  # External bribes
    "oHYDX": {"amount": 0.0007248, "usd": 0.01},  # External bribes
    "WETH": {"amount": 0.046357, "usd": 91.08},  # Fees
    "kVCM_fees": {"amount": 5.5658, "usd": 0.50},  # Fees
    "kVCM_bribes": {"amount": 1615.61, "usd": 144.04},  # External bribes
}

TOTAL_ACTUAL = 45.28 + 155.54 + 171.98 + 0.01 + 91.08 + 0.50 + 144.04

console.print(Panel.fit(
    "[bold cyan]Actual Rewards Reconciliation[/bold cyan]\n"
    "What you received vs pool predictions",
    border_style="cyan"
))

console.print(f"\n[bold cyan]Your Actual Rewards Breakdown:[/bold cyan]\n")

reward_table = Table(show_header=True, header_style="bold cyan")
reward_table.add_column("Token", width=15)
reward_table.add_column("Amount", width=18, justify="right")
reward_table.add_column("USD Value", width=15, justify="right")
reward_table.add_column("Type", width=12)

for token, data in ACTUAL_RECEIVED.items():
    if "fees" in token.lower():
        typ = "[yellow]Internal[/yellow]"
    else:
        typ = "[cyan]External[/cyan]"
    
    reward_table.add_row(
        token,
        f"{data['amount']:,}",
        f"${data['usd']:,.2f}",
        typ
    )

console.print(reward_table)
console.print(f"\n[bold]Total received: ${TOTAL_ACTUAL:,.2f}[/bold]\n")

# Now calculate what was predicted based on your actual vote shares
console.print(f"[bold cyan]Predictions vs Actual:[/bold cyan]\n")

pred_table = Table(show_header=True, header_style="bold cyan")
pred_table.add_column("Pool", width=15)
pred_table.add_column("Your Share %", width=12, justify="right")
pred_table.add_column("Total Bribes", width=15, justify="right")
pred_table.add_column("Predicted $", width=15, justify="right", style="green")

total_predicted = 0

for pool_name, vote_info in votes_data["pools"].items():
    share_pct = vote_info["your_share_pct"]
    
    # Find bribes for this pool
    bribes = 0
    for pool_addr, analysis in epoch_data["pools_analysis"].items():
        if (pool_addr == "0x51f0b932855986b0e621c9d4db6eee1f4644d3d2" and pool_name == "HYDX/USDC") or \
           (pool_addr == "0xef96ec76eeb36584fc4922e9fa268e0780170f33" and pool_name == "kVCM/USDC") or \
           (pool_addr == "0x82dbe18346a8656dbb5e76f74bf3ae279cc16b29" and pool_name == "WETH/USDC"):
            bribes = analysis["total_usd"]
            break
    
    predicted = bribes * (share_pct / 100)
    total_predicted += predicted
    
    pred_table.add_row(
        pool_name,
        f"{share_pct:.4f}%",
        f"${bribes:,.2f}",
        f"${predicted:,.2f}"
    )

console.print(pred_table)
console.print(f"\n[bold]Total predicted: ${total_predicted:,.2f}[/bold]\n")

# Summary
console.print(f"[bold cyan]Summary Comparison:[/bold cyan]\n")

diff = TOTAL_ACTUAL - total_predicted
diff_pct = (diff / total_predicted * 100) if total_predicted > 0 else 0

console.print(f"Predicted based on vote shares: ${total_predicted:,.2f}")
console.print(f"Actual rewards received:        ${TOTAL_ACTUAL:,.2f}")
console.print(f"Difference:                     ${diff:+,.2f} ({diff_pct:+.1f}%)\n")

if diff > 0:
    console.print(f"[green]✓ You received ${diff:,.2f} MORE than predicted![/green]")
    console.print(f"  This suggests: bribes paid out more than snapshot showed")
elif diff < 0:
    console.print(f"[yellow]⚠ You received ${abs(diff):,.2f} LESS than predicted[/yellow]")
    console.print(f"  This could be due to:")
    console.print(f"    • Bribes not fully distributed")
    console.print(f"    • Token price volatility between collection and distribution")
    console.print(f"    • Some bribe contracts not paying out")
else:
    console.print(f"[green]✓ Perfect match![/green]")

# Breakdown by pool
console.print(f"\n[bold cyan]Breakdown by pool source:[/bold cyan]\n")

console.print("Internal rewards (trading fees):")
internal_total = 45.28 + 155.54 + 91.08 + 0.50
console.print(f"  HYDX (from HYDX/USDC): $45.28")
console.print(f"  USDC (from HYDX?): ~$155.54")
console.print(f"  WETH (from WETH/USDC): $91.08")
console.print(f"  kVCM (from kVCM/USDC): $0.50")
console.print(f"  Subtotal: ${internal_total:,.2f}")

console.print(f"\nExternal rewards (bribes):")
external_total = 171.98 + 0.01 + 144.04
console.print(f"  USDC (from HYDX/USDC?): $171.98")
console.print(f"  oHYDX (discount HYDX): $0.01")
console.print(f"  kVCM (from kVCM/USDC): $144.04")
console.print(f"  Subtotal: ${external_total:,.2f}")

console.print(f"\nTotal: ${internal_total + external_total:,.2f} ({internal_total/(internal_total + external_total)*100:.0f}% internal, {external_total/(internal_total + external_total)*100:.0f}% external)")

console.print(f"\n[bold cyan]Key Findings:[/bold cyan]\n")

console.print(f"1. Your actual voting power deployment:")
console.print(f"   - HYDX/USDC: {votes_data['pools']['HYDX/USDC']['your_votes']:,.0f} votes ({votes_data['pools']['HYDX/USDC']['your_share_pct']:.4f}% of pool)")
console.print(f"   - kVCM/USDC: {votes_data['pools']['kVCM/USDC']['your_votes']:,.0f} votes ({votes_data['pools']['kVCM/USDC']['your_share_pct']:.4f}% of pool)")
console.print(f"   - WETH/USDC: {votes_data['pools']['WETH/USDC']['your_votes']:,.0f} votes ({votes_data['pools']['WETH/USDC']['your_share_pct']:.4f}% of pool)")
console.print(f"   - Total: {votes_data['total_your_votes']:,.0f} votes\n")

console.print(f"2. Pool popularity at epoch close:")
for pool_name, vote_info in votes_data["pools"].items():
    total = vote_info["total_votes"]
    your = vote_info["your_votes"]
    others = total - your
    pct_others = (others / total * 100) if total > 0 else 0
    console.print(f"   - {pool_name}: {others:,.0f} other votes ({pct_others:.1f}% of pool)")

console.print(f"\n3. Data accuracy:")
if abs(diff_pct) <= 15:
    console.print(f"   [green]✓ Excellent! Predictions were within 15% accuracy[/green]")
elif abs(diff_pct) <= 30:
    console.print(f"   [yellow]◆ Good prediction accuracy within 30%[/yellow]")
else:
    console.print(f"   [red]✗ Significant deviation - investigate bribe contract data[/red]")
