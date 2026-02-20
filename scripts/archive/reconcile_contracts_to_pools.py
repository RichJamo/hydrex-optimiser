#!/usr/bin/env python3
"""
Detailed reconciliation mapping exact bribe contract payouts to pools.
Shows internal vs external distinction.
"""

import sqlite3
from collections import defaultdict
from rich.console import Console
from rich.table import Table
from rich.panel import Panel

console = Console()

# Your actual bribe contract payouts
BRIBE_PAYOUTS = {
    "0xB69B1C48917cc055c76a93A748b5dAa6eFA39DEe": {
        "tokens": {"HYDX": 670.883084480057348445, "USDC": 62.509172},
        "pool": "HYDX/USDC",
        "type": "internal",  # fees
    },
    "0xCFD60AF43d3ebeFd319037C23558c60438A37d18": {
        "tokens": {"USDC": 92.596109, "WETH": 0.046356840784551859},
        "pool": "WETH/USDC",
        "type": "internal",  # fees
    },
    "0x7c02E7A38774317DFC72c2506FD642De2C55A7de": {
        "tokens": {"USDC": 0.439046, "kVCM": 5.565842354677375443},
        "pool": "kVCM/USDC",
        "type": "internal",  # fees
    },
    "0x3FDE14f30732C18476CD9265144eBb1d89f6e8F9": {
        "tokens": {"USDC": 171.988534, "oHYDX": 0.000000027480406184},
        "pool": None,  # TO BE DETERMINED
        "type": "external",  # bribes
    },
    "0x6b4e7d1752257cdc266b380b0F980cF75D3a2465": {
        "tokens": {"kVCM": 1615.606847270916113787},
        "pool": "kVCM/USDC",
        "type": "external",  # bribes
    },
}

console.print(Panel.fit(
    "[bold cyan]Bribe Contract → Pool Mapping[/bold cyan]\n"
    "Internal fees vs external bribes",
    border_style="cyan"
))

# Load database to query contract details
DATABASE_PATH = "data.db"
conn = sqlite3.connect(DATABASE_PATH)
cursor = conn.cursor()

CLOSED_EPOCH = 1771372800

console.print(f"\n[bold cyan]Step 1: Match contracts to pools by querying database[/bold cyan]\n")

unknown_contract = None
for contract_addr, payout_info in BRIBE_PAYOUTS.items():
    if payout_info["pool"] is None:
        unknown_contract = contract_addr
        
        # Query to find which pools this contract serves
        cursor.execute(f"""
            SELECT DISTINCT g.pool
            FROM bribes b
            JOIN gauges g ON b.gauge_address = g.address
            WHERE b.bribe_contract = ? AND b.epoch = ?
        """, (contract_addr.lower(), CLOSED_EPOCH))
        
        pools = cursor.fetchall()
        if pools:
            pool_addr = pools[0][0]
            pool_map = {
                "0x51f0b932855986b0e621c9d4db6eee1f4644d3d2": "HYDX/USDC",
                "0xef96ec76eeb36584fc4922e9fa268e0780170f33": "kVCM/USDC",
                "0x82dbe18346a8656dbb5e76f74bf3ae279cc16b29": "WETH/USDC",
            }
            pool_name = pool_map.get(pool_addr.lower(), pool_addr)
            BRIBE_PAYOUTS[contract_addr]["pool"] = pool_name
            console.print(f"[cyan]{contract_addr[:15]}...{contract_addr[-8:]}[/cyan]")
            console.print(f"  → Maps to [bold]{pool_name}[/bold]")
            console.print(f"  → Type: [bold yellow]{payout_info['type'].upper()}[/bold yellow]\n")

# Display all payouts organized by pool
console.print(f"[bold cyan]Step 2: All token payouts by pool and type[/bold cyan]\n")

payouts_by_pool = defaultdict(lambda: {"internal": {}, "external": {}})

for contract_addr, payout_info in BRIBE_PAYOUTS.items():
    pool_name = payout_info["pool"]
    payout_type = payout_info["type"]
    
    if pool_name:
        for token, amount in payout_info["tokens"].items():
            if token not in payouts_by_pool[pool_name][payout_type]:
                payouts_by_pool[pool_name][payout_type][token] = 0
            payouts_by_pool[pool_name][payout_type][token] += amount

for pool_name in ["HYDX/USDC", "kVCM/USDC", "WETH/USDC"]:
    console.print(f"[bold]{pool_name}[/bold]")
    
    # Internal
    if payouts_by_pool[pool_name]["internal"]:
        console.print(f"  [yellow]Internal (Fees):[/yellow]")
        for token, amount in sorted(payouts_by_pool[pool_name]["internal"].items()):
            if amount < 1:
                display = f"{amount:.15f}".rstrip("0").rstrip(".")
            else:
                display = f"{amount:,.2f}" if amount > 100 else f"{amount:,.6f}"
            console.print(f"    {token}: {display}")
    
    # External
    if payouts_by_pool[pool_name]["external"]:
        console.print(f"  [cyan]External (Bribes):[/cyan]")
        for token, amount in sorted(payouts_by_pool[pool_name]["external"].items()):
            if amount < 1:
                display = f"{amount:.15f}".rstrip("0").rstrip(".")
            else:
                display = f"{amount:,.2f}" if amount > 100 else f"{amount:,.6f}"
            console.print(f"    {token}: {display}")
    
    console.print()

# Create detailed table
console.print(f"[bold cyan]Step 3: Detailed transaction table[/bold cyan]\n")

detail_table = Table(show_header=True, header_style="bold cyan")
detail_table.add_column("Contract", width=20)
detail_table.add_column("Pool", width=15)
detail_table.add_column("Type", width=12)
detail_table.add_column("Token", width=10)
detail_table.add_column("Amount", width=25, justify="right")

for contract_addr in sorted(BRIBE_PAYOUTS.keys()):
    payout_info = BRIBE_PAYOUTS[contract_addr]
    pool_name = payout_info["pool"]
    payout_type = payout_info["type"]
    
    type_display = "[yellow]Internal[/yellow]" if payout_type == "internal" else "[cyan]External[/cyan]"
    
    for token, amount in sorted(payout_info["tokens"].items()):
        if amount < 1:
            amt_display = f"{amount:.15f}".rstrip("0").rstrip(".")
        else:
            amt_display = f"{amount:,.2f}" if amount > 100 else f"{amount:,.6f}"
        
        detail_table.add_row(
            contract_addr[:10] + "..." + contract_addr[-8:],
            pool_name or "UNKNOWN",
            type_display,
            token,
            amt_display
        )

console.print(detail_table)

# Summary totals
console.print(f"\n[bold cyan]Step 4: Summary by pool[/bold cyan]\n")

summary_table = Table(show_header=True, header_style="bold cyan")
summary_table.add_column("Pool", width=15)
summary_table.add_column("Type", width=12)
summary_table.add_column("Breakdown", width=70)

for pool_name in ["HYDX/USDC", "kVCM/USDC", "WETH/USDC"]:
    for payout_type in ["internal", "external"]:
        tokens = payouts_by_pool[pool_name][payout_type]
        if tokens:
            breakdown_parts = []
            for token, amount in sorted(tokens.items()):
                if amount < 1:
                    display = f"{amount:.9f}".rstrip("0").rstrip(".")
                else:
                    display = f"{amount:,.2f}" if amount > 100 else f"{amount:,.6f}"
                breakdown_parts.append(f"{token}: {display}")
            
            breakdown = ", ".join(breakdown_parts)
            type_display = "[yellow]Internal[/yellow]" if payout_type == "internal" else "[cyan]External[/cyan]"
            summary_table.add_row(pool_name, type_display, breakdown)

console.print(summary_table)

# Get prices and calculate USD totals
console.print(f"\n[bold cyan]Step 5: USD conversion[/bold cyan]\n")

cursor.execute(f"""
    SELECT DISTINCT token_symbol, usd_price FROM bribes
    WHERE epoch = {CLOSED_EPOCH}
    AND token_symbol IN ('HYDX', 'USDC', 'WETH', 'kVCM', 'oHYDX')
""")

prices = {symbol: price for symbol, price in cursor.fetchall()}

usd_table = Table(show_header=True, header_style="bold cyan")
usd_table.add_column("Pool", width=15)
usd_table.add_column("Type", width=12)
usd_table.add_column("Token", width=10)
usd_table.add_column("Amount", width=20, justify="right")
usd_table.add_column("Price", width=12, justify="right")
usd_table.add_column("USD Value", width=15, justify="right")

total_internal_usd = 0
total_external_usd = 0

for pool_name in ["HYDX/USDC", "kVCM/USDC", "WETH/USDC"]:
    for payout_type in ["internal", "external"]:
        tokens = payouts_by_pool[pool_name][payout_type]
        for token in sorted(tokens.keys()):
            amount = tokens[token]
            price = prices.get(token, 0)
            usd_value = amount * price
            
            if payout_type == "internal":
                total_internal_usd += usd_value
                type_display = "[yellow]Internal[/yellow]"
            else:
                total_external_usd += usd_value
                type_display = "[cyan]External[/cyan]"
            
            if amount < 0.001:
                amt_display = f"{amount:.15f}".rstrip("0").rstrip(".")
            else:
                amt_display = f"{amount:,.6f}" if amount < 1 else f"{amount:,.2f}"
            
            usd_table.add_row(
                pool_name,
                type_display,
                token,
                amt_display,
                f"${price:,.4f}",
                f"${usd_value:,.2f}"
            )

console.print(usd_table)

console.print(f"\n[bold cyan]Final Totals[/bold cyan]\n")
total_usd = total_internal_usd + total_external_usd
console.print(f"Internal fees:  ${total_internal_usd:,.2f} ({total_internal_usd/total_usd*100:.1f}%)")
console.print(f"External bribes: ${total_external_usd:,.2f} ({total_external_usd/total_usd*100:.1f}%)")
console.print(f"[bold]Total:         ${total_usd:,.2f}[/bold]\n")

conn.close()
