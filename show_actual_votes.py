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

# 2026-01-22 epoch is when bribes are paid
# Votes from 2026-01-15 apply to it
bribe_epoch_ts = 1768435200  # 2026-01-22
vote_epoch_ts = 1767830400   # 2026-01-15

bribe_date = datetime.utcfromtimestamp(bribe_epoch_ts).strftime('%Y-%m-%d')
vote_date = datetime.utcfromtimestamp(vote_epoch_ts).strftime('%Y-%m-%d')

print(f"Vote epoch: {vote_date} (ts={vote_epoch_ts})")
print(f"Bribe epoch: {bribe_date} (ts={bribe_epoch_ts})")
print()

escrow = "0x768a675B8542F23C428C6672738E380176E7635C"
client = SubgraphClient()

print("Fetching your actual votes from subgraph...")
actual_votes = client.fetch_all_paginated(
    client.fetch_gauge_votes,
    epoch=vote_epoch_ts,
    voter=escrow,
)

print(f"\nFound {len(actual_votes)} gauge votes in subgraph:")
print(f"{'Gauge':<10} {'Wei':<30} {'Normalized':<15}")
print("-" * 55)

total_wei = 0
for vote in actual_votes:
    gauge = vote["gauge"]["address"]
    weight_wei = float(vote["weight"])
    weight_normalized = weight_wei / 1e18
    total_wei += weight_wei
    print(f"{gauge[:8]}... {weight_wei:<30.0f} {weight_normalized:<15,.2f}")

print("-" * 55)
total_normalized = total_wei / 1e18
print(f"TOTAL:     {total_wei:<30.0f} {total_normalized:<15,.2f}")
print()
print(f"Your voting power: 1,530,896")
print(f"Total allocation: {total_normalized:,.2f} ({total_normalized/1530896*100:.1f}% of power)")
