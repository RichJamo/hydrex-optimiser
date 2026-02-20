#!/usr/bin/env python3
"""
Complete analysis of last vote: pools → gauges → bribe contracts → payments
"""

import json
from web3 import Web3
from rich.console import Console
from rich.table import Table
from rich.panel import Panel

console = Console()

# Setup web3
RPC_URL = "https://base-mainnet.g.alchemy.com/v2/oFfvEpXYjGo8Nj4QQIkU3kXd6Z0JvfJZ"
w3 = Web3(Web3.HTTPProvider(RPC_URL))

# Load VoterV5 ABI
with open("voterv5_abi.json", "r") as f:
    voter_abi = json.load(f)

VOTER_ADDRESS = "0xc69E3eF39E3fFBcE2A1c570f8d3ADF76909ef17b"
voter = w3.eth.contract(address=Web3.to_checksum_address(VOTER_ADDRESS), abi=voter_abi)

# Your vote data
vote_data = [
    {
        "pool": "0x3f9b863EF4B295d6Ba370215bcCa3785FCC44f44",
        "gauge": "0xee5f8bf7cdb1ad421993a368b15d06ad58122dab",
        "name": "WETH/cbBTC"
    },
    {
        "pool": "0x0BA69825c4C033e72309F6AC0Bde0023b15Cc97c",
        "gauge": "0xe63cd99406e98d909ab6d702b11dd4cd31a425a2",
        "name": "USDC/cbBTC"
    },
    {
        "pool": "0xEf96Ec76eEB36584FC4922e9fA268e0780170f33",
        "gauge": "0xdc470dc0b3247058ea4605dba6e48a9b2a083971",
        "name": "kVCM/USDC"
    },
    {
        "pool": "0x680581725840958141Bb328666D8Fc185aC4FA49",
        "gauge": "0x1df220b45408a11729302ec84a1443d98beccc57",
        "name": "BNKR/WETH"
    }
]

# Payment data
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

console.print(Panel.fit(
    "[bold cyan]Your Last Vote: Complete Analysis[/bold cyan]\n"
    "Pools → Gauges → Bribe Contracts → Payments",
    border_style="cyan"
))

# Step 1: Query bribe contracts for each gauge
console.print("\n[bold cyan]Step 1: Querying Bribe Contracts[/bold cyan]")
console.print("=" * 100)

for vote in vote_data:
    gauge_checksum = Web3.to_checksum_address(vote["gauge"])
    
    try:
        internal_bribe = voter.functions.internal_bribes(gauge_checksum).call()
        external_bribe = voter.functions.external_bribes(gauge_checksum).call()
        
        vote["internal_bribe"] = internal_bribe
        vote["external_bribe"] = external_bribe
        
        console.print(f"✓ {vote['name']}")
        console.print(f"  Gauge: {vote['gauge']}")
        console.print(f"  Internal: {internal_bribe}")
        console.print(f"  External: {external_bribe}")
        
    except Exception as e:
        console.print(f"[red]✗ {vote['name']}: Error - {e}[/red]")
        vote["internal_bribe"] = None
        vote["external_bribe"] = None

# Step 2: Match payments to pools
console.print("\n[bold cyan]Step 2: Matching Payments to Pools[/bold cyan]")
console.print("=" * 100)

for payment in payments:
    payment_contract = payment["contract"].lower()
    matched = False
    
    for vote in vote_data:
        if vote.get("internal_bribe") and payment_contract == vote["internal_bribe"].lower():
            payment["pool"] = vote["name"]
            payment["type"] = "Internal"
            payment["pool_addr"] = vote["pool"]
            matched = True
            break
        elif vote.get("external_bribe") and payment_contract == vote["external_bribe"].lower():
            payment["pool"] = vote["name"]
            payment["type"] = "External"
            payment["pool_addr"] = vote["pool"]
            matched = True
            break
    
    if not matched:
        payment["pool"] = "Unknown"
        payment["type"] = "Unknown"
        payment["pool_addr"] = None

# Step 3: Create summary
console.print("\n[bold cyan]Step 3: Complete Summary[/bold cyan]")
console.print("=" * 100)

summary_table = Table(title="Your Vote Summary", show_header=True, show_lines=True)
summary_table.add_column("Pool", style="cyan", width=15)
summary_table.add_column("Pool Address", style="white", width=20)
summary_table.add_column("Gauge Address", style="yellow", width=20)
summary_table.add_column("Payment Details", style="green", width=40)

pool_totals = {}

for vote in vote_data:
    pool_name = vote["name"]
    pool_addr = vote["pool"]
    gauge_addr = vote["gauge"]
    
    # Find all payments for this pool
    pool_payments = []
    pool_total = 0
    
    for payment in payments:
        if payment.get("pool_addr") and payment["pool_addr"].lower() == pool_addr.lower():
            token_list = ", ".join([f"{t['token']}: ${t['usd']:.2f}" for t in payment["tokens"]])
            pool_payments.append(f"{payment['type']}: {token_list}")
            pool_total += payment["total_usd"]
    
    pool_totals[pool_name] = pool_total
    
    payment_str = "\n".join(pool_payments) if pool_payments else "[red]No payments[/red]"
    payment_str += f"\n[bold]Total: ${pool_total:.2f}[/bold]"
    
    summary_table.add_row(
        pool_name,
        f"{pool_addr[:8]}...{pool_addr[-6:]}",
        f"{gauge_addr[:8]}...{gauge_addr[-6:]}",
        payment_str
    )

console.print(summary_table)

# Step 4: Payment breakdown
console.print("\n[bold cyan]Step 4: Detailed Payment Breakdown[/bold cyan]")
console.print("=" * 100)

payment_table = Table(show_header=True)
payment_table.add_column("Bribe Contract", style="cyan", width=20)
payment_table.add_column("Type", style="yellow", width=10)
payment_table.add_column("Pool", style="green", width=15)
payment_table.add_column("Tokens", style="white", width=40)
payment_table.add_column("Total", style="magenta", justify="right")

for payment in payments:
    token_list = "\n".join([f"{t['token']}: ${t['usd']:.2f}" for t in payment["tokens"]])
    
    payment_table.add_row(
        f"{payment['contract'][:8]}...{payment['contract'][-6:]}",
        payment.get("type", "Unknown"),
        payment.get("pool", "Unknown"),
        token_list,
        f"${payment['total_usd']:.2f}"
    )

console.print(payment_table)

# Final statistics
console.print("\n[bold cyan]Final Statistics[/bold cyan]")
console.print("=" * 100)

total_received = sum(pool_totals.values())
voting_power = 1183272
votes_per_pool = voting_power // 4

stats_table = Table(show_header=False)
stats_table.add_column("Metric", style="cyan", width=30)
stats_table.add_column("Value", style="green", width=30)

stats_table.add_row("Pools Voted", "4")
stats_table.add_row("Voting Power", f"{voting_power:,}")
stats_table.add_row("Votes per Pool", f"{votes_per_pool:,} (equal split)")
stats_table.add_row("", "")
stats_table.add_row("[bold]Total Rewards Received[/bold]", f"[bold]${total_received:.2f}[/bold]")
stats_table.add_row("Return per Vote", f"${total_received/voting_power:.6f}")
stats_table.add_row("Return per 1K Votes", f"${(total_received/voting_power)*1000:.3f}")

console.print(stats_table)

# Pool performance comparison
console.print("\n[bold cyan]Pool Performance Comparison[/bold cyan]")
console.print("=" * 100)

perf_table = Table(show_header=True)
perf_table.add_column("Pool", style="cyan")
perf_table.add_column("Rewards", style="green", justify="right")
perf_table.add_column("$/1K Votes", style="yellow", justify="right")
perf_table.add_column("% of Total", style="magenta", justify="right")

sorted_pools = sorted(pool_totals.items(), key=lambda x: x[1], reverse=True)

for pool_name, reward in sorted_pools:
    pct = (reward / total_received) * 100 if total_received > 0 else 0
    per_1k = (reward / votes_per_pool) * 1000
    perf_table.add_row(
        pool_name,
        f"${reward:.2f}",
        f"${per_1k:.2f}",
        f"{pct:.1f}%"
    )

console.print(perf_table)

console.print("\n" + "=" * 100)
console.print("[bold green]Analysis Complete![/bold green]")
console.print(f"You voted equally across 4 pools and earned ${total_received:.2f}")
console.print(f"Average return: ${total_received/4:.2f} per pool")
