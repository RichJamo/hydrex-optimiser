#!/usr/bin/env python3
"""
Complete reconciliation with actual bribe contract addresses.
Token amounts first, then USD conversion.
"""

import sqlite3
from collections import defaultdict
from rich.console import Console
from rich.table import Table
from rich.panel import Panel

console = Console()

# Your actual payouts by full bribe contract address
ACTUAL_PAYOUTS = {
    "0xB69B1C48917cc055c76a93A748b5dAa6eFA39DEe": {
        "HYDX": 670.883084480057348445,
        "USDC": 62.509172,
    },
    "0xCFD60AF43d3ebeFd319037C23558c60438A37d18": {
        "USDC": 92.596109,
        "WETH": 0.046356840784551859,
    },
    "0x7c02E7A38774317DFC72c2506FD642De2C55A7de": {
        "USDC": 0.439046,
        "kVCM": 5.565842354677375443,
    },
    "0x3FDE14f30732C18476CD9265144eBb1d89f6e8F9": {
        "USDC": 171.988534,
        "oHYDX": 0.000000027480406184,
    },
    "0x6b4e7d1752257cdc266b380b0F980cF75D3a2465": {
        "kVCM": 1615.606847270916113787,
    },
}

# Aggregate by token
TOTAL_TOKENS = defaultdict(float)
for contract, tokens in ACTUAL_PAYOUTS.items():
    for token, amount in tokens.items():
        TOTAL_TOKENS[token] += amount

console.print(Panel.fit(
    "[bold cyan]Bribe Contract Reconciliation[/bold cyan]\n"
    "Full addresses with token amounts",
    border_style="cyan"
))

console.print(f"\n[bold cyan]Step 1: Identify which pools these contracts serve[/bold cyan]\n")

DATABASE_PATH = "data.db"
conn = sqlite3.connect(DATABASE_PATH)
cursor = conn.cursor()

CLOSED_EPOCH = 1771372800

# Query database for these contracts
contract_info = {}
pools_by_contract = defaultdict(list)

for contract_addr in ACTUAL_PAYOUTS.keys():
    cursor.execute(f"""
        SELECT DISTINCT 
            g.pool,
            COUNT(DISTINCT b.id) as bribe_count
        FROM bribes b
        JOIN gauges g ON b.gauge_address = g.address
        WHERE b.bribe_contract = ? AND b.epoch = ?
        GROUP BY g.pool
    """, (contract_addr.lower(), CLOSED_EPOCH))
    
    pools = cursor.fetchall()
    if pools:
        for pool_addr, count in pools:
            pools_by_contract[contract_addr].append(pool_addr)
            contract_info[contract_addr] = {
                "pools": [p for p, _ in pools],
                "bribe_count": sum(c for _, c in pools)
            }

contract_table = Table(show_header=True, header_style="bold cyan")
contract_table.add_column("Bribe Contract", width=25)
contract_table.add_column("Associated Pools", width=60)

pool_names = {
    "0x51f0b932855986b0e621c9d4db6eee1f4644d3d2": "HYDX/USDC",
    "0xef96ec76eeb36584fc4922e9fa268e0780170f33": "kVCM/USDC",
    "0x82dbe18346a8656dbb5e76f74bf3ae279cc16b29": "WETH/USDC",
}

for contract_addr in ACTUAL_PAYOUTS.keys():
    pools = contract_info.get(contract_addr, {}).get("pools", [])
    pool_display = ", ".join([pool_names.get(p.lower(), p[:10]+"...") for p in pools]) or "Unknown"
    contract_table.add_row(
        contract_addr[:15] + "..." + contract_addr[-10:],
        pool_display
    )

console.print(contract_table)

console.print(f"\n[bold cyan]Step 2: Actual tokens received[/bold cyan]\n")

tokens_table = Table(show_header=True, header_style="bold cyan")
tokens_table.add_column("Token", width=10)
tokens_table.add_column("By Bribe Contract", width=50)

# Reorganize by token showing all contracts
for token in sorted(TOTAL_TOKENS.keys()):
    contracts_for_token = []
    for contract, tokens in ACTUAL_PAYOUTS.items():
        if token in tokens:
            amount = tokens[token]
            if amount < 1:
                amt_str = f"{amount:.15f}".rstrip("0").rstrip(".")
            else:
                amt_str = f"{amount:,.2f}" if amount > 100 else f"{amount:,.6f}"
            contracts_for_token.append(f"{amt_str}")
    
    contracts_display = " + ".join(contracts_for_token)
    tokens_table.add_row(token, contracts_display)

console.print(tokens_table)

console.print(f"\n[bold cyan]Step 3: Total tokens received[/bold cyan]\n")

total_table = Table(show_header=True, header_style="bold cyan")
total_table.add_column("Token", width=10)
total_table.add_column("Total Amount", width=30, justify="right")

for token in sorted(TOTAL_TOKENS.keys()):
    amount = TOTAL_TOKENS[token]
    if amount < 1:
        display = f"{amount:.18f}".rstrip("0").rstrip(".")
    else:
        display = f"{amount:,.2f}" if amount > 100 else f"{amount:,.6f}"
    total_table.add_row(token, display)

console.print(total_table)

# Get predicted amounts and prices
console.print(f"\n[bold cyan]Step 4: Predicted amounts (from database)[/bold cyan]\n")

cursor.execute(f"""
    SELECT 
        token_symbol,
        COALESCE(SUM(amount), 0) as total_amount,
        COALESCE(SUM(usd_value), 0) as total_usd,
        COALESCE(MAX(usd_price), 0) as price
    FROM bribes
    WHERE epoch = {CLOSED_EPOCH}
    AND token_symbol IN ('HYDX', 'USDC', 'WETH', 'kVCM', 'oHYDX')
    GROUP BY token_symbol
""")

predicted = {}
prices = {}
for symbol, amount, usd, price in cursor.fetchall():
    predicted[symbol] = {"amount": amount, "usd": usd}
    prices[symbol] = price

pred_table = Table(show_header=True, header_style="bold cyan")
pred_table.add_column("Token", width=10)
pred_table.add_column("Predicted Amount", width=30, justify="right")
pred_table.add_column("Price", width=15, justify="right")
pred_table.add_column("Predicted USD", width=15, justify="right")

for token in sorted(TOTAL_TOKENS.keys()):
    if token in predicted:
        amount = predicted[token]["amount"]
        usd = predicted[token]["usd"]
        price = prices.get(token, 0)
        if amount < 1:
            amt_display = f"{amount:.18f}".rstrip("0").rstrip(".")
        else:
            amt_display = f"{amount:,.2f}" if amount > 100 else f"{amount:,.6f}"
        pred_table.add_row(token, amt_display, f"${price:,.4f}", f"${usd:,.2f}")
    else:
        pred_table.add_row(token, "N/A (not in DB)", "N/A", "$0.00")

console.print(pred_table)

# Comparison
console.print(f"\n[bold cyan]Step 5: Token amount comparison[/bold cyan]\n")

comp_table = Table(show_header=True, header_style="bold cyan")
comp_table.add_column("Token", width=10)
comp_table.add_column("Predicted", width=25, justify="right", style="cyan")
comp_table.add_column("Received", width=25, justify="right", style="yellow")
comp_table.add_column("Match %", width=12, justify="right")

for token in sorted(TOTAL_TOKENS.keys()):
    actual = TOTAL_TOKENS[token]
    pred = predicted.get(token, {}).get("amount", 0)
    match_pct = (actual / pred * 100) if pred > 0 else (100 if actual == 0 else 0)
    
    if pred < 1:
        pred_display = f"{pred:.18f}".rstrip("0").rstrip(".")
    else:
        pred_display = f"{pred:,.2f}" if pred > 100 else f"{pred:,.6f}"
    
    if actual < 1:
        actual_display = f"{actual:.18f}".rstrip("0").rstrip(".")
    else:
        actual_display = f"{actual:,.2f}" if actual > 100 else f"{actual:,.6f}"
    
    if 95 <= match_pct <= 105:
        match_style = "[green]"
    elif 85 <= match_pct <= 115:
        match_style = "[yellow]"
    else:
        match_style = "[red]"
    
    comp_table.add_row(
        token,
        pred_display,
        actual_display,
        f"{match_style}{match_pct:.1f}%[/]"
    )

console.print(comp_table)

# USD conversion
console.print(f"\n[bold cyan]Step 6: USD conversion (using database prices)[/bold cyan]\n")

usd_table = Table(show_header=True, header_style="bold cyan")
usd_table.add_column("Token", width=10)
usd_table.add_column("Price", width=15, justify="right")
usd_table.add_column("Predicted USD", width=18, justify="right", style="cyan")
usd_table.add_column("Received USD", width=18, justify="right", style="yellow")
usd_table.add_column("USD Difference", width=18, justify="right")

total_pred_usd = 0
total_recv_usd = 0

for token in sorted(TOTAL_TOKENS.keys()):
    price = prices.get(token, 0)
    pred_amt = predicted.get(token, {}).get("amount", 0)
    actual_amt = TOTAL_TOKENS[token]
    
    pred_usd = pred_amt * price
    recv_usd = actual_amt * price
    diff_usd = recv_usd - pred_usd
    
    total_pred_usd += pred_usd
    total_recv_usd += recv_usd
    
    usd_table.add_row(
        token,
        f"${price:,.4f}" if price > 0 else "N/A",
        f"${pred_usd:,.2f}",
        f"${recv_usd:,.2f}",
        f"${diff_usd:+,.2f}"
    )

console.print(usd_table)

console.print(f"\n[bold cyan]Final Summary[/bold cyan]\n")
console.print(f"Total Predicted USD: ${total_pred_usd:,.2f}")
console.print(f"Total Received USD:  ${total_recv_usd:,.2f}")
diff_usd = total_recv_usd - total_pred_usd
console.print(f"USD Difference:      ${diff_usd:+,.2f}")

match_rate = (total_recv_usd / total_pred_usd * 100) if total_pred_usd > 0 else 0
console.print(f"Match Rate:          {match_rate:.1f}%\n")

if 95 <= match_rate <= 105:
    console.print("[green]✓ Excellent! Predictions were very accurate[/green]")
elif 85 <= match_rate <= 115:
    console.print("[yellow]◆ Good match within 15% variance[/yellow]")
else:
    console.print("[yellow]⚠ Notable difference - likely due to token price movement[/yellow]")

conn.close()
