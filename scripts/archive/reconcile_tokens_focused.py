#!/usr/bin/env python3
"""
Reconciliation focused on actual tokens received (token amounts first, then USD).
"""

import sqlite3
from collections import defaultdict
from rich.console import Console
from rich.table import Table
from rich.panel import Panel

console = Console()

# Your actual payouts by bribe contract
ACTUAL_TOKENS = {
    "HYDX": 670.883084480057348445,
    "USDC": 62.509172 + 92.596109 + 0.439046 + 171.988534,  # 327.532861
    "WETH": 0.046356840784551859,
    "kVCM": 5.565842354677375443 + 1615.606847270916113787,  # 1621.172689625593489230
    "oHYDX": 0.000000027480406184,
}

console.print(Panel.fit(
    "[bold cyan]Token-Level Reconciliation[/bold cyan]\n"
    "Actual tokens received vs predicted amounts",
    border_style="cyan"
))

console.print(f"\n[bold cyan]Your Actual Token Receipts[/bold cyan]\n")

actual_table = Table(show_header=True, header_style="bold cyan")
actual_table.add_column("Token", width=10)
actual_table.add_column("Amount Received", width=30, justify="right")

for token in sorted(ACTUAL_TOKENS.keys()):
    amount = ACTUAL_TOKENS[token]
    # Format nicely
    if amount < 1:
        display = f"{amount:.18f}".rstrip("0").rstrip(".")
    else:
        display = f"{amount:,.2f}" if amount > 100 else f"{amount:,.6f}"
    actual_table.add_row(token, display)

console.print(actual_table)

# Load database
DATABASE_PATH = "data.db"
conn = sqlite3.connect(DATABASE_PATH)
cursor = conn.cursor()

CLOSED_EPOCH = 1771372800

# Get predicted amounts for these specific tokens
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

predicted_data = cursor.fetchall()
predicted = {}
prices = {}
for symbol, amount, usd, price in predicted_data:
    predicted[symbol] = {"amount": amount, "usd": usd}
    prices[symbol] = price

console.print(f"\n[bold cyan]Predicted Amounts (from database)[/bold cyan]\n")

pred_table = Table(show_header=True, header_style="bold cyan")
pred_table.add_column("Token", width=10)
pred_table.add_column("Amount Predicted", width=30, justify="right")
pred_table.add_column("Price", width=15, justify="right")

for token in sorted(ACTUAL_TOKENS.keys()):
    if token in predicted:
        amount = predicted[token]["amount"]
        price = prices.get(token, 0)
        if amount < 1:
            display = f"{amount:.18f}".rstrip("0").rstrip(".")
        else:
            display = f"{amount:,.2f}" if amount > 100 else f"{amount:,.6f}"
        pred_table.add_row(token, display, f"${price:,.4f}" if price > 0 else "N/A")
    else:
        pred_table.add_row(token, "N/A (not in DB)", "N/A")

console.print(pred_table)

console.print(f"\n[bold cyan]Token Amount Comparison[/bold cyan]\n")

comparison = Table(show_header=True, header_style="bold cyan")
comparison.add_column("Token", width=10)
comparison.add_column("Predicted", width=25, justify="right", style="cyan")
comparison.add_column("Received", width=25, justify="right", style="yellow")
comparison.add_column("Difference", width=25, justify="right", style="magenta")
comparison.add_column("Match %", width=12, justify="right")

for token in sorted(ACTUAL_TOKENS.keys()):
    actual = ACTUAL_TOKENS[token]
    pred = predicted.get(token, {}).get("amount", 0)
    diff = actual - pred
    match_pct = (actual / pred * 100) if pred > 0 else (100 if actual == 0 else float('inf'))
    
    # Format displays
    if pred < 1:
        pred_display = f"{pred:.18f}".rstrip("0").rstrip(".")
    else:
        pred_display = f"{pred:,.2f}" if pred > 100 else f"{pred:,.6f}"
    
    if actual < 1:
        actual_display = f"{actual:.18f}".rstrip("0").rstrip(".")
    else:
        actual_display = f"{actual:,.2f}" if actual > 100 else f"{actual:,.6f}"
    
    if abs(diff) < 0.001:
        diff_display = f"{diff:.18f}".rstrip("0").rstrip(".")
    else:
        diff_display = f"{diff:+,.2f}" if abs(diff) > 100 else f"{diff:+,.6f}"
    
    # Color for match
    if 95 <= match_pct <= 105:
        match_color = "[green]"
    elif 85 <= match_pct <= 115:
        match_color = "[yellow]"
    else:
        match_color = "[red]"
    
    comparison.add_row(
        token,
        pred_display,
        actual_display,
        diff_display,
        f"{match_color}{match_pct:.1f}%[/]"
    )

console.print(comparison)

console.print(f"\n[bold cyan]USD Conversion (using database prices)[/bold cyan]\n")

usd_table = Table(show_header=True, header_style="bold cyan")
usd_table.add_column("Token", width=10)
usd_table.add_column("Price", width=15, justify="right")
usd_table.add_column("Predicted USD", width=18, justify="right", style="cyan")
usd_table.add_column("Received USD", width=18, justify="right", style="yellow")
usd_table.add_column("USD Diff", width=18, justify="right")

total_pred_usd = 0
total_recv_usd = 0

for token in sorted(ACTUAL_TOKENS.keys()):
    price = prices.get(token, 0)
    pred_amt = predicted.get(token, {}).get("amount", 0)
    actual_amt = ACTUAL_TOKENS[token]
    
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
print_diff = total_recv_usd - total_pred_usd
console.print(f"USD Difference:      ${print_diff:+,.2f}")

match_rate = (total_recv_usd / total_pred_usd * 100) if total_pred_usd > 0 else 0
console.print(f"Overall Match Rate:  {match_rate:.1f}%\n")

if 95 <= match_rate <= 105:
    console.print("[green]✓ Excellent match! Predictions were very accurate.[/green]")
elif 85 <= match_rate <= 115:
    console.print("[yellow]◆ Good match within 15% variance.[/yellow]")
else:
    console.print("[red]✗ Significant difference - investigate token prices.[/red]")

conn.close()
