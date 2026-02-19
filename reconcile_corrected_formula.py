#!/usr/bin/env python3
"""
Correct reconciliation: Predicted = Total Bribes * Your Vote Share %
"""

import sqlite3
from collections import defaultdict
import json
from rich.console import Console
from rich.table import Table
from rich.panel import Panel

console = Console()

# Your actual tokens received
ACTUAL_TOKENS = {
    "HYDX": 670.883084480057348445,
    "USDC": 62.509172 + 92.596109 + 0.439046 + 171.988534,  # 327.532861
    "WETH": 0.046356840784551859,
    "kVCM": 5.565842354677375443 + 1615.606847270916113787,  # 1621.172689625593489230
    "oHYDX": 0.000000027480406184,
}

# Your actual votes from on-chain query
YOUR_VOTES = {
    "HYDX/USDC": 496_974,
    "kVCM/USDC": 283_985,
    "WETH/USDC": 402_313,
}

TOTAL_VOTES = {
    "HYDX/USDC": 5_779_156,
    "kVCM/USDC": 17_577_626,
    "WETH/USDC": 11_169_235,
}

YOUR_SHARES = {
    "HYDX/USDC": YOUR_VOTES["HYDX/USDC"] / TOTAL_VOTES["HYDX/USDC"] * 100,
    "kVCM/USDC": YOUR_VOTES["kVCM/USDC"] / TOTAL_VOTES["kVCM/USDC"] * 100,
    "WETH/USDC": YOUR_VOTES["WETH/USDC"] / TOTAL_VOTES["WETH/USDC"] * 100,
}

console.print(Panel.fit(
    "[bold cyan]Corrected Reconciliation[/bold cyan]\n"
    "Predicted = Total Bribes × Your Vote Share %",
    border_style="cyan"
))

# Load database
DATABASE_PATH = "data.db"
conn = sqlite3.connect(DATABASE_PATH)
cursor = conn.cursor()

CLOSED_EPOCH = 1771372800

console.print(f"\n[bold cyan]Step 1: Your vote shares in each pool[/bold cyan]\n")

shares_table = Table(show_header=True, header_style="bold cyan")
shares_table.add_column("Pool", width=15)
shares_table.add_column("Your Votes", width=15, justify="right")
shares_table.add_column("Total Votes", width=15, justify="right")
shares_table.add_column("Your Share %", width=15, justify="right", style="yellow")

for pool, share_pct in YOUR_SHARES.items():
    shares_table.add_row(
        pool,
        f"{YOUR_VOTES[pool]:,}",
        f"{TOTAL_VOTES[pool]:,}",
        f"{share_pct:.4f}%"
    )

console.print(shares_table)

# Get total bribes by pool and token
console.print(f"\n[bold cyan]Step 2: Total bribes available in each pool[/bold cyan]\n")

cursor.execute(f"""
    SELECT 
        g.pool,
        b.token_symbol,
        COALESCE(SUM(b.amount), 0) as total_amount,
        COALESCE(MAX(b.usd_price), 0) as price
    FROM bribes b
    JOIN gauges g ON b.gauge_address = g.address
    WHERE b.epoch = {CLOSED_EPOCH}
    AND g.pool IN (?, ?, ?)
    GROUP BY g.pool, b.token_symbol
    ORDER BY g.pool, b.token_symbol
""", (
    "0x51f0b932855986b0e621c9d4db6eee1f4644d3d2",
    "0xef96ec76eeb36584fc4922e9fa268e0780170f33",
    "0x82dbe18346a8656dbb5e76f74bf3ae279cc16b29"
))

pool_bribes = defaultdict(lambda: defaultdict(dict))
pool_names = {
    "0x51f0b932855986b0e621c9d4db6eee1f4644d3d2": "HYDX/USDC",
    "0xef96ec76eeb36584fc4922e9fa268e0780170f33": "kVCM/USDC",
    "0x82dbe18346a8656dbb5e76f74bf3ae279cc16b29": "WETH/USDC",
}

for pool_addr, token, amount, price in cursor.fetchall():
    pool_name = pool_names[pool_addr.lower()]
    pool_bribes[pool_name][token] = {
        "amount": amount,
        "price": price
    }

for pool_name in ["HYDX/USDC", "kVCM/USDC", "WETH/USDC"]:
    console.print(f"[bold]{pool_name}[/bold] ({YOUR_SHARES[pool_name]:.4f}% of {pool_name})")
    for token, data in sorted(pool_bribes[pool_name].items()):
        amount = data["amount"]
        price = data["price"]
        if amount < 1:
            amt_display = f"{amount:.15f}".rstrip("0").rstrip(".")
        else:
            amt_display = f"{amount:,.2f}" if amount > 100 else f"{amount:,.6f}"
        console.print(f"  {token}: {amt_display} @ ${price:.4f}/token")
    console.print()

# Calculate expected per-token amounts
console.print(f"[bold cyan]Step 3: Expected token amounts (your share)[/bold cyan]\n")

expected = defaultdict(float)
comp_table = Table(show_header=True, header_style="bold cyan")
comp_table.add_column("Token", width=10)
comp_table.add_column("Pool", width=15)
comp_table.add_column("Total Bribe", width=20, justify="right")
comp_table.add_column("Your Share %", width=12, justify="right")
comp_table.add_column("Expected Amt", width=20, justify="right")
comp_table.add_column("Actual Amt", width=20, justify="right")
comp_table.add_column("Match %", width=12, justify="right")

for pool_name in ["HYDX/USDC", "kVCM/USDC", "WETH/USDC"]:
    share_pct = YOUR_SHARES[pool_name]
    for token, data in sorted(pool_bribes[pool_name].items()):
        total_amt = data["amount"]
        expected_amt = total_amt * (share_pct / 100)
        actual_amt = ACTUAL_TOKENS.get(token, 0)
        
        # We need to sum across pools for tokens that appear in multiple pools
        # This is approximate since USDC appears in multiple pools
        
        match_pct = (actual_amt / expected_amt * 100) if expected_amt > 0 else 0
        
        if total_amt < 1:
            total_display = f"{total_amt:.15f}".rstrip("0").rstrip(".")
        else:
            total_display = f"{total_amt:,.2f}" if total_amt > 100 else f"{total_amt:,.6f}"
        
        if expected_amt < 1:
            exp_display = f"{expected_amt:.15f}".rstrip("0").rstrip(".")
        else:
            exp_display = f"{expected_amt:,.2f}" if expected_amt > 100 else f"{expected_amt:,.6f}"
        
        if actual_amt < 1:
            act_display = f"{actual_amt:.15f}".rstrip("0").rstrip(".")
        else:
            act_display = f"{actual_amt:,.2f}" if actual_amt > 100 else f"{actual_amt:,.6f}"
        
        comp_table.add_row(
            token,
            pool_name,
            total_display,
            f"{share_pct:.4f}%",
            exp_display,
            act_display,
            f"{match_pct:.1f}%" if expected_amt > 0 else "N/A"
        )
        
        expected[token] += expected_amt

console.print(comp_table)

# Final USD summary
console.print(f"\n[bold cyan]Step 4: Total USD by token[/bold cyan]\n")

final_table = Table(show_header=True, header_style="bold cyan")
final_table.add_column("Token", width=10)
final_table.add_column("Total Expected", width=25, justify="right")
final_table.add_column("Actual Received", width=25, justify="right")
final_table.add_column("Match %", width=12, justify="right")
final_table.add_column("Expected USD", width=15, justify="right", style="cyan")
final_table.add_column("Actual USD", width=15, justify="right", style="yellow")

total_exp_usd = 0
total_act_usd = 0

for token in sorted(set(expected.keys()) | set(ACTUAL_TOKENS.keys())):
    exp_amt = expected[token]
    act_amt = ACTUAL_TOKENS.get(token, 0)
    
    # Get price
    cursor.execute(f"""
        SELECT MAX(usd_price) FROM bribes 
        WHERE epoch = {CLOSED_EPOCH} AND token_symbol = ?
    """, (token,))
    price_result = cursor.fetchone()
    price = price_result[0] if price_result[0] else 0
    
    exp_usd = exp_amt * price
    act_usd = act_amt * price
    match_pct = (act_amt / exp_amt * 100) if exp_amt > 0 else 0
    
    total_exp_usd += exp_usd
    total_act_usd += act_usd
    
    if exp_amt < 1:
        exp_display = f"{exp_amt:.15f}".rstrip("0").rstrip(".")
    else:
        exp_display = f"{exp_amt:,.2f}" if exp_amt > 100 else f"{exp_amt:,.6f}"
    
    if act_amt < 1:
        act_display = f"{act_amt:.15f}".rstrip("0").rstrip(".")
    else:
        act_display = f"{act_amt:,.2f}" if act_amt > 100 else f"{act_amt:,.6f}"
    
    final_table.add_row(
        token,
        exp_display,
        act_display,
        f"{match_pct:.1f}%",
        f"${exp_usd:,.2f}",
        f"${act_usd:,.2f}"
    )

console.print(final_table)

console.print(f"\n[bold cyan]Summary[/bold cyan]\n")
console.print(f"Expected USD (corrected): ${total_exp_usd:,.2f}")
console.print(f"Actual USD Received:      ${total_act_usd:,.2f}")
print_diff = total_act_usd - total_exp_usd
console.print(f"Difference:               ${print_diff:+,.2f}")
match = (total_act_usd / total_exp_usd * 100) if total_exp_usd > 0 else 0
console.print(f"Match Rate:               {match:.1f}%\n")

if 90 <= match <= 110:
    console.print("[green]✓ Excellent match! Predictions were accurate[/green]")
elif 75 <= match <= 125:
    console.print("[yellow]◆ Good match within acceptable variance[/yellow]")
else:
    console.print("[red]✗ Significant difference - check token distributions[/red]")

conn.close()
