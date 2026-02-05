#!/usr/bin/env python3
import os
import sys
from datetime import datetime
from dotenv import load_dotenv

sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'src'))

from config import Config
from database import Database
from price_feed import PriceFeed
from subgraph_client import SubgraphClient

load_dotenv()

escrow = "0x768a675B8542F23C428C6672738E380176E7635C"

# Based on analysis output, the epoch with 4 matching gauges is 2026-01-15 (ts=1768435200)
# Your votes from that epoch apply to bribes in that same epoch (per your contract analysis)
bribe_epoch_ts = 1768435200

epoch_date = datetime.utcfromtimestamp(bribe_epoch_ts).strftime('%Y-%m-%d')
print(f"Analyzing epoch: {epoch_date} (ts={bribe_epoch_ts})")
print()

# Get subgraph data for your actual votes
client = SubgraphClient()
actual_votes_data = client.fetch_all_paginated(
    client.fetch_gauge_votes,
    epoch=bribe_epoch_ts,
    voter=escrow,
)

print(f"Found {len(actual_votes_data)} gauges with your votes in subgraph")
print()

# Get database data
db = Database('data.db')
session = db.get_session()

from src.database import Epoch, Bribe, Vote

# Get the epoch object - Epoch uses timestamp as primary key
epoch = session.query(Epoch).filter_by(timestamp=bribe_epoch_ts).first()
if not epoch:
    print(f"ERROR: Epoch {bribe_epoch_ts} not found in database")
    sys.exit(1)

# Get all bribes for this epoch - Bribe uses epoch field
bribes = session.query(Bribe).filter_by(epoch=bribe_epoch_ts).all()
print(f"Found {len(bribes)} bribe events in database for this epoch")
print()

# Get all votes for this epoch - Vote uses epoch field
votes = session.query(Vote).filter_by(epoch=bribe_epoch_ts).all()
gauge_total_votes = {}
for vote in votes:
    gauge_addr = vote.gauge.lower()
    if gauge_addr not in gauge_total_votes:
        gauge_total_votes[gauge_addr] = 0
    gauge_total_votes[gauge_addr] += vote.total_votes

print(f"Found {len(gauge_total_votes)} gauges with votes in database")
print()

# Get prices
unique_tokens = list(set(bribe.reward_token for bribe in bribes))
token_prices = PriceFeed(Config.COINGECKO_API_KEY, db).get_batch_prices_for_timestamp(
    unique_tokens, bribe_epoch_ts, granularity="hour"
)

# Get all gauges to build bribe->gauge mapping
from src.database import Gauge
gauges = session.query(Gauge).all()
bribe_to_gauge = {}
for gauge in gauges:
    if gauge.internal_bribe:
        bribe_to_gauge[gauge.internal_bribe.lower()] = ("internal", gauge.address.lower())
    if gauge.external_bribe:
        bribe_to_gauge[gauge.external_bribe.lower()] = ("external", gauge.address.lower())

# Calculate bribes per gauge (split internal/external and by token)
gauge_bribes = {}
gauge_bribe_tokens = {}
for bribe in bribes:
    bribe_contract_lower = bribe.bribe_contract.lower()
    gauge_info = bribe_to_gauge.get(bribe_contract_lower)
    
    if not gauge_info:
        continue
    
    bribe_type, gauge_addr = gauge_info
    token_addr = bribe.reward_token.lower()
    price = token_prices.get(token_addr, 0.0)
    usd_value = bribe.amount * price
    
    if gauge_addr not in gauge_bribes:
        gauge_bribes[gauge_addr] = {"internal_usd": 0.0, "external_usd": 0.0}
    if gauge_addr not in gauge_bribe_tokens:
        gauge_bribe_tokens[gauge_addr] = {"internal": {}, "external": {}}
    
    if bribe_type == "internal":
        gauge_bribes[gauge_addr]["internal_usd"] += usd_value
        token_entry = gauge_bribe_tokens[gauge_addr]["internal"].setdefault(token_addr, {"amount": 0.0, "usd": 0.0})
    else:
        gauge_bribes[gauge_addr]["external_usd"] += usd_value
        token_entry = gauge_bribe_tokens[gauge_addr]["external"].setdefault(token_addr, {"amount": 0.0, "usd": 0.0})
    
    token_entry["amount"] += bribe.amount
    token_entry["usd"] += usd_value

print()
print("=" * 140)
print("DETAILED BREAKDOWN BY GAUGE (INTERNAL vs EXTERNAL)")
print("=" * 140)
print(
    f"{'Gauge':<10} {'Your Votes':<15} {'DB Total Votes':<15} "
    f"{'Your Share %':<15} {'Internal USD':<15} {'External USD':<15} "
    f"{'Total USD':<15} {'Your Return':<15}"
)
print("-" * 140)

total_your_return = 0.0
for vote_data in actual_votes_data:
    gauge_addr = vote_data["gauge"]["address"].lower()
    your_votes_wei = float(vote_data["weight"])
    your_votes_norm = your_votes_wei / 1e18
    
    db_total_votes = gauge_total_votes.get(gauge_addr, 0)
    bribe_totals = gauge_bribes.get(gauge_addr, {"internal_usd": 0.0, "external_usd": 0.0})
    internal_usd = bribe_totals["internal_usd"]
    external_usd = bribe_totals["external_usd"]
    gauge_bribes_usd = internal_usd + external_usd
    
    if db_total_votes > 0:
        your_share = your_votes_norm / db_total_votes
    else:
        your_share = 0
    
    your_return = gauge_bribes_usd * your_share
    total_your_return += your_return
    
    print(
        f"{gauge_addr[:8]}... {your_votes_norm:<15,.2f} {db_total_votes:<15,.2f} "
        f"{your_share*100:<14.2f}% ${internal_usd:<14,.2f} ${external_usd:<14,.2f} "
        f"${gauge_bribes_usd:<14,.2f} ${your_return:<14,.2f}"
    )
    
    # Token breakdown per gauge
    token_breakdown = gauge_bribe_tokens.get(gauge_addr, {"internal": {}, "external": {}})
    if token_breakdown["internal"]:
        print("  Internal bribes:")
        for token_addr, info in sorted(token_breakdown["internal"].items(), key=lambda x: x[1]["usd"], reverse=True):
            print(f"    {token_addr[:10]}... amount={info['amount']:.6f}, usd=${info['usd']:.2f}")
    if token_breakdown["external"]:
        print("  External bribes:")
        for token_addr, info in sorted(token_breakdown["external"].items(), key=lambda x: x[1]["usd"], reverse=True):
            print(f"    {token_addr[:10]}... amount={info['amount']:.6f}, usd=${info['usd']:.2f}")

print("-" * 120)
print(f"{'TOTAL':<10} {'':<15} {'':<15} {'':<15} {'':<15} ${total_your_return:<14,.2f}")
print("=" * 120)
print()
print(f"Calculated return: ${total_your_return:,.2f}")
print(f"Actual received:   $1,800.00")
print(f"Scaling factor:    {1800 / total_your_return:.2f}x" if total_your_return > 0 else "N/A")

session.close()
