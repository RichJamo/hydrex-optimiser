#!/usr/bin/env python3
"""
Trace the user's last vote to understand the structure:
- Which pools were voted for
- Which gauges they correspond to  
- Internal and external bribe contracts
- Which contracts paid out and how much
"""

import sqlite3
from rich.console import Console
from rich.table import Table

console = Console()

# Your voted pools
pools_voted = [
    "0x3f9b863EF4B295d6Ba370215bcCa3785FCC44f44",  # WETH/cbBTC
    "0x0BA69825c4C033e72309F6AC0Bde0023b15Cc97c",  # USDC/cbBTC
    "0x680581725840958141Bb328666D8Fc185aC4FA49",  # BNKR/WETH
    "0xEf96Ec76eEB36584FC4922e9fA268e0780170f33",  # kVCM/USDC
]

# Bribe contracts that paid you
payments = [
    {
        "contract": "0xdbd3DA2c3183a4db0d6a1E648a06B14b593dB7B5",
        "tokens": [
            {"token": "WETH", "amount": "0.054639611306253636", "usd": 123.67},
            {"token": "cbBTC", "amount": "0.00161239", "usd": 123.08},
        ],
        "total_usd": 246.75
    },
    {
        "contract": "0x71aaE818Cd357f62C3aD25B5012cC27587442AaE",
        "tokens": [
            {"token": "cbBTC", "amount": "0.00163285", "usd": 124.64},
            {"token": "USDC", "amount": "111.46623", "usd": 111.46},
        ],
        "total_usd": 236.10
    },
    {
        "contract": "0x7c02E7A38774317DFC72c2506FD642De2C55A7de",
        "tokens": [
            {"token": "USDC", "amount": "1.999759", "usd": 2.00},
            {"token": "kVCM", "amount": "96.918577040758863272", "usd": 8.71},
        ],
        "total_usd": 10.71
    },
    {
        "contract": "0xC96802e581c7B7ecC4ccFF37e0eE2b60bBe6741f",
        "tokens": [
            {"token": "WETH", "amount": "0.058558857516738677", "usd": 132.54},
            {"token": "BNKR", "amount": "171235.812464587854919736", "usd": 93.43},
        ],
        "total_usd": 225.97
    },
    {
        "contract": "0x6b4e7d1752257cdc266b380b0F980cF75D3a2465",
        "tokens": [
            {"token": "kVCM", "amount": "2614.531578534736797237", "usd": 235.00},
        ],
        "total_usd": 235.00
    },
]

# Connect to database
db = sqlite3.connect("data.db")
cursor = db.cursor()

console.print("\n[bold cyan]Your Last Vote Analysis[/bold cyan]")
console.print("=" * 100)

# Create main summary table
summary_table = Table(title="Vote → Gauge → Bribe Contracts → Payments", show_header=True, show_lines=True)
summary_table.add_column("Pool Address", style="cyan", width=20)
summary_table.add_column("Gauge Address", style="yellow", width=20)
summary_table.add_column("Internal Bribe", style="green", width=20)
summary_table.add_column("External Bribe", style="green", width=20)
summary_table.add_column("Payments Received", style="magenta", width=25)

total_received = 0

for pool in pools_voted:
    # Look up gauge and bribe contracts
    cursor.execute("""
        SELECT address, internal_bribe, external_bribe
        FROM gauges
        WHERE LOWER(pool) = LOWER(?)
    """, (pool,))
    
    result = cursor.fetchone()
    
    if result:
        gauge_addr, internal_bribe, external_bribe = result
        
        # Find which bribe contracts paid this pool
        payments_for_pool = []
        pool_total = 0
        
        for payment in payments:
            contract = payment["contract"].lower()
            if contract == internal_bribe.lower():
                payments_for_pool.append(f"Internal: ${payment['total_usd']:.2f}")
                pool_total += payment["total_usd"]
            elif contract == external_bribe.lower():
                payments_for_pool.append(f"External: ${payment['total_usd']:.2f}")
                pool_total += payment["total_usd"]
        
        total_received += pool_total
        
        payment_str = "\n".join(payments_for_pool) if payments_for_pool else "None"
        
        summary_table.add_row(
            f"{pool[:8]}...{pool[-6:]}",
            f"{gauge_addr[:8]}...{gauge_addr[-6:]}",
            f"{internal_bribe[:8]}...{internal_bribe[-6:]}",
            f"{external_bribe[:8]}...{external_bribe[-6:]}",
            payment_str
        )
    else:
        summary_table.add_row(
            f"{pool[:8]}...{pool[-6:]}",
            "[red]NOT FOUND[/red]",
            "",
            "",
            ""
        )

console.print(summary_table)

# Payment breakdown table
console.print("\n[bold cyan]Detailed Payment Breakdown by Bribe Contract[/bold cyan]")
console.print("=" * 100)

payment_table = Table(show_header=True)
payment_table.add_column("Bribe Contract", style="cyan", width=20)
payment_table.add_column("Type", style="yellow", width=10)
payment_table.add_column("For Pool", style="green", width=20)
payment_table.add_column("Tokens Paid", style="white", width=40)
payment_table.add_column("Total USD", style="magenta", justify="right")

for payment in payments:
    contract = payment["contract"].lower()
    
    # Find which pool this contract belongs to
    matched_pool = None
    contract_type = None
    
    for pool in pools_voted:
        cursor.execute("""
            SELECT internal_bribe, external_bribe
            FROM gauges
            WHERE LOWER(pool) = LOWER(?)
        """, (pool,))
        result = cursor.fetchone()
        
        if result:
            internal, external = result
            if contract == internal.lower():
                matched_pool = pool
                contract_type = "Internal"
                break
            elif contract == external.lower():
                matched_pool = pool
                contract_type = "External"
                break
    
    # Format token list
    token_list = []
    for token in payment["tokens"]:
        token_list.append(f"{token['token']}: ${token['usd']:.2f}")
    token_str = "\n".join(token_list)
    
    pool_display = f"{matched_pool[:8]}...{matched_pool[-6:]}" if matched_pool else "[red]Unknown[/red]"
    
    payment_table.add_row(
        f"{payment['contract'][:8]}...{payment['contract'][-6:]}",
        contract_type or "[red]?[/red]",
        pool_display,
        token_str,
        f"${payment['total_usd']:.2f}"
    )

console.print(payment_table)

# Summary statistics
console.print(f"\n[bold]Summary:[/bold]")
console.print(f"  Pools Voted: [cyan]{len(pools_voted)}[/cyan]")
console.print(f"  Bribe Contracts Paid: [cyan]{len(payments)}[/cyan]")
console.print(f"  Total Received: [green]${total_received:.2f}[/green]")
console.print(f"  Voting Power: [cyan]1,530,896[/cyan]")
console.print(f"  Return per 1K Votes: [yellow]${(total_received / 1530.896):.2f}[/yellow]")

# Check for any orphaned payments
orphaned = []
for payment in payments:
    contract = payment["contract"].lower()
    found = False
    
    for pool in pools_voted:
        cursor.execute("""
            SELECT internal_bribe, external_bribe
            FROM gauges
            WHERE LOWER(pool) = LOWER(?)
        """, (pool,))
        result = cursor.fetchone()
        
        if result:
            internal, external = result
            if contract in [internal.lower(), external.lower()]:
                found = True
                break
    
    if not found:
        orphaned.append(payment)

if orphaned:
    console.print(f"\n[yellow]⚠ Warning: {len(orphaned)} bribe contract(s) paid you but don't match your voted pools![/yellow]")
    for payment in orphaned:
        console.print(f"  • {payment['contract']} paid ${payment['total_usd']:.2f}")

db.close()

console.print("\n" + "=" * 100)
