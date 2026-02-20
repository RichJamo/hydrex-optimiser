#!/usr/bin/env python3
"""
Verify on-chain bribe contract balances for tokens where we expected rewards but got 0.
"""

import sqlite3
import json
import os
from web3 import Web3
from dotenv import load_dotenv
from rich.console import Console
from rich.table import Table
from rich.panel import Panel

load_dotenv()

console = Console()

# Connect to Base
RPC_URL = os.getenv("RPC_URL")
w3 = Web3(Web3.HTTPProvider(RPC_URL))

if not w3.is_connected():
    console.print("[red]Failed to connect to Base[/red]")
    exit(1)

console.print("[green]Connected to Base[/green]")

# Load ERC20 ABI for balanceOf
ERC20_ABI = [
    {
        "constant": True,
        "inputs": [{"name": "_owner", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"name": "balance", "type": "uint256"}],
        "type": "function"
    },
    {
        "constant": True,
        "inputs": [],
        "name": "decimals",
        "outputs": [{"name": "", "type": "uint8"}],
        "type": "function"
    },
    {
        "constant": True,
        "inputs": [],
        "name": "symbol",
        "outputs": [{"name": "", "type": "string"}],
        "type": "function"
    }
]

DATABASE_PATH = "data.db"
conn = sqlite3.connect(DATABASE_PATH)
cursor = conn.cursor()

CLOSED_EPOCH = 1771372800

console.print(Panel.fit(
    "[bold cyan]Bribe Contract Balance Verification[/bold cyan]\n"
    "Checking on-chain balances for unfulfilled bribes",
    border_style="cyan"
))

# Cases where expected > 0 but actual = 0
ZERO_CASES = [
    {"pool": "HYDX/USDC", "type": "external", "token": "HYDX"},
    {"pool": "HYDX/USDC", "type": "external", "token": "WETH"},
    {"pool": "HYDX/USDC", "type": "external", "token": "oHYDX"},
    {"pool": "kVCM/USDC", "type": "external", "token": "oHYDX"},
    {"pool": "WETH/USDC", "type": "external", "token": "USDC"},
    {"pool": "WETH/USDC", "type": "external", "token": "oHYDX"},
]

pool_addr_map = {
    "HYDX/USDC": "0x51f0b932855986b0e621c9d4db6eee1f4644d3d2",
    "kVCM/USDC": "0xef96ec76eeb36584fc4922e9fa268e0780170f33",
    "WETH/USDC": "0x82dbe18346a8656dbb5e76f74bf3ae279cc16b29",
}

console.print(f"\n[bold cyan]Step 1: Query database for bribe contracts and token addresses[/bold cyan]\n")

results_table = Table(show_header=True, header_style="bold cyan")
results_table.add_column("Pool", width=15)
results_table.add_column("Type", width=10)
results_table.add_column("Token", width=10)
results_table.add_column("DB Amount", width=18, justify="right")
results_table.add_column("Bribe Contract", width=25)
results_table.add_column("Token Address", width=25)
results_table.add_column("On-Chain Balance", width=20, justify="right")

for case in ZERO_CASES:
    pool_name = case["pool"]
    bribe_type = case["type"]
    token_symbol = case["token"]
    
    pool_addr = pool_addr_map[pool_name]
    
    # Query database for this specific bribe
    cursor.execute(f"""
        SELECT 
            b.bribe_contract,
            b.reward_token,
            b.amount,
            b.token_decimals,
            g.address as gauge_address
        FROM bribes b
        JOIN gauges g ON b.gauge_address = g.address
        WHERE g.pool = ?
        AND b.bribe_type = ?
        AND b.token_symbol = ?
        AND b.epoch = ?
        LIMIT 5
    """, (pool_addr.lower(), bribe_type, token_symbol, CLOSED_EPOCH))
    
    bribes = cursor.fetchall()
    
    if not bribes:
        results_table.add_row(
            pool_name,
            bribe_type,
            token_symbol,
            "[red]NOT IN DB[/red]",
            "N/A",
            "N/A",
            "N/A"
        )
        continue
    
    # Check each bribe contract
    for bribe_contract, token_addr, db_amount, decimals, gauge_addr in bribes:
        # Query on-chain balance
        try:
            token_contract = w3.eth.contract(
                address=Web3.to_checksum_address(token_addr),
                abi=ERC20_ABI
            )
            
            # Get current balance in the bribe contract
            balance_wei = token_contract.functions.balanceOf(
                Web3.to_checksum_address(bribe_contract)
            ).call()
            
            balance_tokens = balance_wei / (10 ** decimals) if decimals else balance_wei
            
            # Format displays
            if db_amount < 1:
                db_display = f"{db_amount:.12f}".rstrip("0").rstrip(".")
            else:
                db_display = f"{db_amount:,.2f}"
            
            if balance_tokens < 1:
                balance_display = f"{balance_tokens:.12f}".rstrip("0").rstrip(".")
            else:
                balance_display = f"{balance_tokens:,.2f}"
            
            # Color code based on balance
            if balance_tokens > 0:
                balance_display = f"[yellow]{balance_display}[/yellow]"
            else:
                balance_display = f"[red]{balance_display}[/red]"
            
            results_table.add_row(
                pool_name,
                bribe_type,
                token_symbol,
                db_display,
                bribe_contract[:15] + "..." + bribe_contract[-8:],
                token_addr[:10] + "..." + token_addr[-8:],
                balance_display
            )
            
        except Exception as e:
            results_table.add_row(
                pool_name,
                bribe_type,
                token_symbol,
                f"{db_amount:,.2f}" if db_amount > 1 else f"{db_amount:.12f}",
                bribe_contract[:15] + "..." + bribe_contract[-8:],
                token_addr[:10] + "..." + token_addr[-8:],
                f"[red]Error: {str(e)[:20]}[/red]"
            )

console.print(results_table)

console.print(f"\n[bold cyan]Analysis:[/bold cyan]\n")
console.print("[yellow]Key Questions:[/yellow]")
console.print("1. If on-chain balance > 0: Bribes exist but weren't distributed to you")
console.print("2. If on-chain balance = 0: Bribes were claimed/distributed or never existed")
console.print("3. If error: Token address may be invalid or contract not responding\n")

console.print("[cyan]Next steps to investigate discrepancies:[/cyan]")
console.print("• Check if bribe distribution happens at epoch end or requires manual claim")
console.print("• Verify gauge → bribe contract mapping is correct")
console.print("• Check if there are time delays in distribution")

conn.close()
