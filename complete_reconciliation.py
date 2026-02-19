#!/usr/bin/env python3
"""
Complete reconciliation with vote information and expected vs actual comparison.
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
        "type": "internal",
    },
    "0xCFD60AF43d3ebeFd319037C23558c60438A37d18": {
        "tokens": {"USDC": 92.596109, "WETH": 0.046356840784551859},
        "pool": "WETH/USDC",
        "type": "internal",
    },
    "0x7c02E7A38774317DFC72c2506FD642De2C55A7de": {
        "tokens": {"USDC": 0.439046, "kVCM": 5.565842354677375443},
        "pool": "kVCM/USDC",
        "type": "internal",
    },
    "0x3FDE14f30732C18476CD9265144eBb1d89f6e8F9": {
        "tokens": {"USDC": 171.988534, "oHYDX": 0.000000027480406184},
        "pool": "HYDX/USDC",
        "type": "external",
    },
    "0x6b4e7d1752257cdc266b380b0F980cF75D3a2465": {
        "tokens": {"kVCM": 1615.606847270916113787},
        "pool": "kVCM/USDC",
        "type": "external",
    },
}

# Your vote information
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
    pool: (YOUR_VOTES[pool] / TOTAL_VOTES[pool] * 100)
    for pool in YOUR_VOTES.keys()
}

console.print(Panel.fit(
    "[bold cyan]Complete Reconciliation[/bold cyan]\n"
    "Votes → Expected → Actual by Pool & Contract",
    border_style="cyan"
))

# Load database
DATABASE_PATH = "data.db"
conn = sqlite3.connect(DATABASE_PATH)
cursor = conn.cursor()

CLOSED_EPOCH = 1771372800

# Get prices
cursor.execute(f"""
    SELECT DISTINCT token_symbol, usd_price FROM bribes
    WHERE epoch = {CLOSED_EPOCH}
    AND token_symbol IN ('HYDX', 'USDC', 'WETH', 'kVCM', 'oHYDX')
""")
prices = {symbol: price for symbol, price in cursor.fetchall()}

# Get predicted bribes by pool and type
console.print(f"\n[bold cyan]Step 1: Vote information[/bold cyan]\n")

vote_table = Table(show_header=True, header_style="bold cyan")
vote_table.add_column("Pool", width=15)
vote_table.add_column("Your Votes", width=15, justify="right")
vote_table.add_column("Total Votes", width=15, justify="right")
vote_table.add_column("Your Share %", width=15, justify="right", style="yellow")

for pool in ["HYDX/USDC", "kVCM/USDC", "WETH/USDC"]:
    vote_table.add_row(
        pool,
        f"{YOUR_VOTES[pool]:,}",
        f"{TOTAL_VOTES[pool]:,}",
        f"{YOUR_SHARES[pool]:.4f}%"
    )

console.print(vote_table)

# Get all bribes from database for these pools
pool_addr_map = {
    "0x51f0b932855986b0e621c9d4db6eee1f4644d3d2": "HYDX/USDC",
    "0xef96ec76eeb36584fc4922e9fa268e0780170f33": "kVCM/USDC",
    "0x82dbe18346a8656dbb5e76f74bf3ae279cc16b29": "WETH/USDC",
}

console.print(f"\n[bold cyan]Step 2: Expected rewards by pool and type[/bold cyan]\n")

# Query database for all bribes by pool, type, and token
cursor.execute(f"""
    SELECT 
        g.pool,
        b.bribe_type,
        b.token_symbol,
        COALESCE(SUM(b.amount), 0) as total_amount
    FROM bribes b
    JOIN gauges g ON b.gauge_address = g.address
    WHERE b.epoch = {CLOSED_EPOCH}
    AND g.pool IN (?, ?, ?)
    GROUP BY g.pool, b.bribe_type, b.token_symbol
""", list(pool_addr_map.keys()))

predicted_bribes = defaultdict(lambda: defaultdict(lambda: defaultdict(float)))

for pool_addr, bribe_type, token, amount in cursor.fetchall():
    pool_name = pool_addr_map[pool_addr.lower()]
    predicted_bribes[pool_name][bribe_type][token] = amount

# Calculate expected and actual by pool
expected_table = Table(show_header=True, header_style="bold cyan")
expected_table.add_column("Pool", width=15)
expected_table.add_column("Type", width=12)
expected_table.add_column("Token", width=10)
expected_table.add_column("Total Bribes", width=20, justify="right")
expected_table.add_column("Your Share", width=12, justify="right")
expected_table.add_column("Expected", width=20, justify="right", style="cyan")
expected_table.add_column("Actual", width=20, justify="right", style="yellow")
expected_table.add_column("Match %", width=12, justify="right")

expected_totals = defaultdict(lambda: defaultdict(float))
actual_totals = defaultdict(lambda: defaultdict(float))

for pool in ["HYDX/USDC", "kVCM/USDC", "WETH/USDC"]:
    share_pct = YOUR_SHARES[pool]
    
    for bribe_type in ["internal", "external"]:
        tokens = predicted_bribes[pool][bribe_type]
        
        for token in sorted(tokens.keys()):
            total_bribe = tokens[token]
            expected = total_bribe * (share_pct / 100)
            
            # Get actual from BRIBE_PAYOUTS
            actual = 0
            for contract, payout_info in BRIBE_PAYOUTS.items():
                if payout_info["pool"] == pool and payout_info["type"] == bribe_type:
                    actual += payout_info["tokens"].get(token, 0)
            
            match_pct = (actual / expected * 100) if expected > 0 else 0
            
            # Store for totals
            expected_totals[pool][bribe_type] += expected * prices.get(token, 0)
            actual_totals[pool][bribe_type] += actual * prices.get(token, 0)
            
            # Format displays
            if total_bribe < 1:
                bribe_display = f"{total_bribe:.12f}".rstrip("0").rstrip(".")
            else:
                bribe_display = f"{total_bribe:,.2f}"
            
            if expected < 1:
                exp_display = f"{expected:.12f}".rstrip("0").rstrip(".")
            else:
                exp_display = f"{expected:,.2f}"
            
            if actual < 1:
                act_display = f"{actual:.12f}".rstrip("0").rstrip(".")
            else:
                act_display = f"{actual:,.2f}"
            
            # Color code match
            if 90 <= match_pct <= 110:
                match_color = "[green]"
            elif 70 <= match_pct <= 130:
                match_color = "[yellow]"
            else:
                match_color = "[red]"
            
            type_display = "[yellow]Internal[/yellow]" if bribe_type == "internal" else "[cyan]External[/cyan]"
            
            expected_table.add_row(
                pool,
                type_display,
                token,
                bribe_display,
                f"{share_pct:.4f}%",
                exp_display,
                act_display,
                f"{match_color}{match_pct:.1f}%[/]"
            )

console.print(expected_table)

# USD Summary by pool
console.print(f"\n[bold cyan]Step 3: USD Summary by pool[/bold cyan]\n")

summary_table = Table(show_header=True, header_style="bold cyan")
summary_table.add_column("Pool", width=15)
summary_table.add_column("Type", width=12)
summary_table.add_column("Expected USD", width=18, justify="right", style="cyan")
summary_table.add_column("Actual USD", width=18, justify="right", style="yellow")
summary_table.add_column("Difference", width=18, justify="right")
summary_table.add_column("Match %", width=12, justify="right")

grand_expected = 0
grand_actual = 0

for pool in ["HYDX/USDC", "kVCM/USDC", "WETH/USDC"]:
    for bribe_type in ["internal", "external"]:
        exp_usd = expected_totals[pool][bribe_type]
        act_usd = actual_totals[pool][bribe_type]
        diff = act_usd - exp_usd
        match_pct = (act_usd / exp_usd * 100) if exp_usd > 0 else 0
        
        grand_expected += exp_usd
        grand_actual += act_usd
        
        if 90 <= match_pct <= 110:
            match_color = "[green]"
        elif 70 <= match_pct <= 130:
            match_color = "[yellow]"
        else:
            match_color = "[red]"
        
        type_display = "[yellow]Internal[/yellow]" if bribe_type == "internal" else "[cyan]External[/cyan]"
        
        summary_table.add_row(
            pool,
            type_display,
            f"${exp_usd:,.2f}",
            f"${act_usd:,.2f}",
            f"${diff:+,.2f}",
            f"{match_color}{match_pct:.1f}%[/]"
        )

console.print(summary_table)

# Final totals
console.print(f"\n[bold cyan]Final Summary[/bold cyan]\n")
console.print(f"Total Expected USD: ${grand_expected:,.2f}")
console.print(f"Total Actual USD:   ${grand_actual:,.2f}")
console.print(f"Difference:         ${grand_actual - grand_expected:+,.2f}")
overall_match = (grand_actual / grand_expected * 100) if grand_expected > 0 else 0
console.print(f"Overall Match:      {overall_match:.1f}%\n")

if 90 <= overall_match <= 110:
    console.print("[green]✓ Excellent! Predictions were within 10% of actual[/green]")
elif 80 <= overall_match <= 120:
    console.print("[yellow]◆ Good match within 20% variance[/yellow]")
else:
    console.print("[red]⚠ Notable variance - check individual token distributions[/red]")

# By-contract breakdown
console.print(f"\n[bold cyan]Step 4: By-contract breakdown[/bold cyan]\n")

contract_table = Table(show_header=True, header_style="bold cyan")
contract_table.add_column("Contract", width=25)
contract_table.add_column("Pool", width=15)
contract_table.add_column("Type", width=12)
contract_table.add_column("Tokens Received", width=45)
contract_table.add_column("USD Value", width=15, justify="right")

for contract_addr in sorted(BRIBE_PAYOUTS.keys()):
    payout_info = BRIBE_PAYOUTS[contract_addr]
    pool = payout_info["pool"]
    bribe_type = payout_info["type"]
    
    # Calculate USD value
    usd_value = sum(
        amount * prices.get(token, 0)
        for token, amount in payout_info["tokens"].items()
    )
    
    # Format tokens
    token_parts = []
    for token, amount in sorted(payout_info["tokens"].items()):
        if amount < 0.001:
            amt_str = f"{amount:.15f}".rstrip("0").rstrip(".")
        else:
            amt_str = f"{amount:,.2f}" if amount > 1 else f"{amount:,.6f}"
        token_parts.append(f"{token}: {amt_str}")
    
    tokens_str = ", ".join(token_parts)
    type_display = "[yellow]Internal[/yellow]" if bribe_type == "internal" else "[cyan]External[/cyan]"
    
    contract_table.add_row(
        contract_addr[:15] + "..." + contract_addr[-8:],
        pool,
        type_display,
        tokens_str,
        f"${usd_value:,.2f}"
    )

console.print(contract_table)

conn.close()
