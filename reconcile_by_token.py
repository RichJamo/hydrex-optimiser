#!/usr/bin/env python3
"""
Reconciliation using actual bribe contract payouts (token amounts first, USD second).
"""

import sqlite3
import json
from collections import defaultdict
from rich.console import Console
from rich.table import Table
from rich.panel import Panel

console = Console()

# Your actual payouts by bribe contract
ACTUAL_PAYOUTS = {
    "0xB69B1C48...6eFA39DEe": {
        "HYDX": 670.883084480057348445,
        "USDC": 62.509172,
    },
    "0xCFD60AF4...438A37d18": {
        "USDC": 92.596109,
        "WETH": 0.046356840784551859,
    },
    "0x7c02E7A3...e2C55A7de": {
        "USDC": 0.439046,
        "kVCM": 5.565842354677375443,
    },
    "0x3FDE14f3...d89f6e8F9": {
        "USDC": 171.988534,
        "oHYDX": 0.000000027480406184,
    },
    "0x6b4e7d17...75D3a2465": {
        "kVCM": 1615.606847270916113787,
    },
}

# Combine by token
TOKENS_RECEIVED = defaultdict(float)
for contract, tokens in ACTUAL_PAYOUTS.items():
    for token, amount in tokens.items():
        TOKENS_RECEIVED[token] += amount

# Load the closed epoch bribe data
DATABASE_PATH = "data.db"
conn = sqlite3.connect(DATABASE_PATH)
cursor = conn.cursor()

CLOSED_EPOCH = 1771372800

console.print(Panel.fit(
    "[bold cyan]Bribe Contract Reconciliation[/bold cyan]\n"
    "Token amounts first, then USD conversion",
    border_style="cyan"
))

console.print(f"\n[bold cyan]Step 1: Actual tokens received by contract[/bold cyan]\n")

# Display actual payouts
contract_table = Table(show_header=True, header_style="bold cyan")
contract_table.add_column("Bribe Contract", width=25)
contract_table.add_column("Token", width=10)
contract_table.add_column("Amount", width=25, justify="right")

for contract, tokens in sorted(ACTUAL_PAYOUTS.items()):
    for token, amount in sorted(tokens.items()):
        contract_table.add_row(
            contract[:10] + "..." + contract[-8:],
            token,
            f"{amount:,.18f}".rstrip("0").rstrip(".") if amount < 1 else f"{amount:,.2f}"
        )

console.print(contract_table)

console.print(f"\n[bold cyan]Step 2: Total tokens received[/bold cyan]\n")

token_summary = Table(show_header=True, header_style="bold cyan")
token_summary.add_column("Token", width=10)
token_summary.add_column("Total Received", width=25, justify="right")

for token in sorted(TOKENS_RECEIVED.keys()):
    amount = TOKENS_RECEIVED[token]
    display = f"{amount:,.18f}".rstrip("0").rstrip(".") if amount < 1 else f"{amount:,.2f}"
    token_summary.add_row(token, display)

console.print(token_summary)

console.print(f"\n[bold cyan]Step 3: Bribe contracts in database[/bold cyan]\n")

# Query the database for bribe contract details
cursor.execute(f"""
    SELECT DISTINCT 
        bribe_contract,
        token_symbol,
        bribe_type,
        gauge_address
    FROM bribes
    WHERE epoch = {CLOSED_EPOCH}
    ORDER BY bribe_contract
""")

contracts_in_db = cursor.fetchall()

console.print(f"Found {len(set(c[0] for c in contracts_in_db))} unique bribe contracts\n")

# Group by contract
contracts_by_id = defaultdict(list)
for contract, symbol, bribe_type, gauge in contracts_in_db:
    contracts_by_id[contract].append({
        "token": symbol,
        "type": bribe_type,
        "gauge": gauge
    })

# Try to match your contracts
console.print("[yellow]Note: Matching your bribe contracts to database contracts needs manual mapping.[/yellow]")
console.print("[yellow]Please provide the full addresses for these contracts:[/yellow]\n")

for contract in ACTUAL_PAYOUTS.keys():
    console.print(f"  {contract}")

console.print(f"\n[cyan]In the meantime, let's analyze what we predicted vs received by token:[/cyan]\n")

# Get all bribes we recorded
cursor.execute(f"""
    SELECT 
        token_symbol,
        COALESCE(SUM(amount), 0) as total_amount,
        COALESCE(SUM(usd_value), 0) as total_usd
    FROM bribes
    WHERE epoch = {CLOSED_EPOCH}
    GROUP BY token_symbol
    ORDER BY total_usd DESC
""")

predicted_by_token = {}
for symbol, amount, usd in cursor.fetchall():
    predicted_by_token[symbol] = {
        "amount": amount,
        "usd": usd
    }

console.print(f"[bold cyan]Step 4: Predicted vs Actual by Token[/bold cyan]\n")

reconciliation = Table(show_header=True, header_style="bold cyan")
reconciliation.add_column("Token", width=10)
reconciliation.add_column("Predicted Amount", width=22, justify="right")
reconciliation.add_column("Actual Received", width=22, justify="right")
reconciliation.add_column("Difference", width=22, justify="right")
reconciliation.add_column("Match %", width=12, justify="right")

for token in sorted(set(list(predicted_by_token.keys()) + list(TOKENS_RECEIVED.keys()))):
    pred_amt = predicted_by_token.get(token, {}).get("amount", 0)
    actual_amt = TOKENS_RECEIVED.get(token, 0)
    diff = actual_amt - pred_amt
    match_pct = (actual_amt / pred_amt * 100) if pred_amt > 0 else 0
    
    # Format display
    if pred_amt < 1:
        pred_display = f"{pred_amt:.18f}".rstrip("0").rstrip(".")
    else:
        pred_display = f"{pred_amt:,.2f}"
    
    if actual_amt < 1:
        actual_display = f"{actual_amt:.18f}".rstrip("0").rstrip(".")
    else:
        actual_display = f"{actual_amt:,.2f}"
    
    if diff < 0.001 and diff > -0.001:
        diff_display = f"{diff:.18f}".rstrip("0").rstrip(".")
    else:
        diff_display = f"{diff:+,.2f}"
    
    # Color code
    if abs(match_pct - 100) < 5:
        match_style = "[green]"
    elif abs(match_pct - 100) < 15:
        match_style = "[yellow]"
    else:
        match_style = "[red]"
    
    reconciliation.add_row(
        token,
        pred_display,
        actual_display,
        diff_display,
        f"{match_style}{match_pct:.1f}%[/]"
    )

console.print(reconciliation)

console.print(f"\n[bold cyan]Step 5: Convert to USD[/bold cyan]\n")

# Get token prices from database
cursor.execute(f"""
    SELECT DISTINCT
        token_symbol,
        usd_price
    FROM bribes
    WHERE epoch = {CLOSED_EPOCH}
    AND usd_price > 0
""")

token_prices = {}
for symbol, price in cursor.fetchall():
    token_prices[symbol] = price

usd_reconciliation = Table(show_header=True, header_style="bold cyan")
usd_reconciliation.add_column("Token", width=10)
usd_reconciliation.add_column("Price", width=15, justify="right")
usd_reconciliation.add_column("Predicted USD", width=15, justify="right")
usd_reconciliation.add_column("Actual USD", width=15, justify="right")
usd_reconciliation.add_column("USD Diff", width=15, justify="right")

total_pred_usd = 0
total_actual_usd = 0

for token in sorted(set(list(predicted_by_token.keys()) + list(TOKENS_RECEIVED.keys()))):
    price = token_prices.get(token, 0)
    pred_amt = predicted_by_token.get(token, {}).get("amount", 0)
    actual_amt = TOKENS_RECEIVED.get(token, 0)
    
    pred_usd = pred_amt * price
    actual_usd = actual_amt * price
    diff_usd = actual_usd - pred_usd
    
    total_pred_usd += pred_usd
    total_actual_usd += actual_usd
    
    usd_reconciliation.add_row(
        token,
        f"${price:,.2f}" if price > 0 else "N/A",
        f"${pred_usd:,.2f}",
        f"${actual_usd:,.2f}",
        f"${diff_usd:+,.2f}"
    )

console.print(usd_reconciliation)

console.print(f"\n[bold cyan]Final Summary[/bold cyan]\n")
console.print(f"Total Predicted USD: ${total_pred_usd:,.2f}")
console.print(f"Total Actual USD:    ${total_actual_usd:,.2f}")
console.print(f"Difference:          ${total_actual_usd - total_pred_usd:+,.2f}")
console.print(f"Match rate:          {(total_actual_usd / total_pred_usd * 100):.1f}%\n")

conn.close()

# Save results
with open("token_reconciliation.json", "w") as f:
    json.dump({
        "actual_tokens": dict(TOKENS_RECEIVED),
        "predicted_tokens": {k: v["amount"] for k, v in predicted_by_token.items()},
        "token_prices": token_prices,
        "total_predicted_usd": total_pred_usd,
        "total_actual_usd": total_actual_usd,
    }, f, indent=2, default=str)

console.print("[green]Saved to token_reconciliation.json[/green]")
