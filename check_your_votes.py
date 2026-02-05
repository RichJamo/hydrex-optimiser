#!/usr/bin/env python3
import os
import sys
from datetime import datetime
from dotenv import load_dotenv

sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'src'))

from subgraph_client import SubgraphClient

load_dotenv()

escrow = "0x768a675B8542F23C428C6672738E380176E7635C"
client = SubgraphClient()

# You voted on 2026-01-21 (during epoch 2026-01-15 to 2026-01-22)
# Those votes apply to epoch 2026-01-22 to 2026-01-29 (ts=1768435200)
vote_epoch_ts = 1767830400   # 2026-01-15 to 2026-01-22 (when you voted)
claim_epoch_ts = 1768435200  # 2026-01-22 to 2026-01-29 (when you can claim)

vote_date = datetime.utcfromtimestamp(vote_epoch_ts).strftime('%Y-%m-%d')
claim_date = datetime.utcfromtimestamp(claim_epoch_ts).strftime('%Y-%m-%d')

# Your on-chain votes
your_gauges = [
    "0x51f0B932855986B0E621c9D4DB6Eee1f4644D3D2",
    "0x2Df4Af05F8C4AFf0d3FbfC327595dbb7Fc6498BF",
    "0x3f9b863EF4B295d6Ba370215bcCa3785FCC44f44",
    "0x82dbe18346a8656dBB5E76F74bf3AE279cC16B29",
]

print(f"Checking votes for escrow: {escrow}")
print(f"Voted on: 2026-01-21 (during epoch {vote_date})")
print(f"Applies to: epoch {claim_date}")
print()

# Check votes in the VOTING epoch (where you cast the votes)
print("=== SUBGRAPH DATA FOR VOTING EPOCH ===")
print(f"Epoch: {vote_date} (ts={vote_epoch_ts})")
actual_votes_voting = client.fetch_all_paginated(
    client.fetch_gauge_votes,
    epoch=vote_epoch_ts,
    voter=escrow,
)

print(f"Found {len(actual_votes_voting)} gauges in subgraph for voting epoch:")
print(f"{'Gauge':<10} {'Wei':<30} {'Normalized':<15} {'Match?':<10}")
print("-" * 65)

for vote in actual_votes_voting:
    gauge = vote["gauge"]["address"].lower()
    weight_wei = float(vote["weight"])
    weight_normalized = weight_wei / 1e18
    is_match = "✓ YES" if gauge in [g.lower() for g in your_gauges] else "✗ NO"
    print(f"{gauge[:8]}... {weight_wei:<30.0f} {weight_normalized:<15,.2f} {is_match:<10}")

print()
print("=== SUBGRAPH DATA FOR CLAIMING EPOCH ===")
print(f"Epoch: {claim_date} (ts={claim_epoch_ts})")
actual_votes_claim = client.fetch_all_paginated(
    client.fetch_gauge_votes,
    epoch=claim_epoch_ts,
    voter=escrow,
)

print(f"Found {len(actual_votes_claim)} gauges in subgraph for claiming epoch:")
print(f"{'Gauge':<10} {'Wei':<30} {'Normalized':<15} {'Match?':<10}")
print("-" * 65)

for vote in actual_votes_claim:
    gauge = vote["gauge"]["address"].lower()
    weight_wei = float(vote["weight"])
    weight_normalized = weight_wei / 1e18
    is_match = "✓ YES" if gauge in [g.lower() for g in your_gauges] else "✗ NO"
    print(f"{gauge[:8]}... {weight_wei:<30.0f} {weight_normalized:<15,.2f} {is_match:<10}")

print()
print("=== ANALYSIS ===")
print(f"Your on-chain vote: 2500 votes × 4 gauges = 10,000 total votes")
if actual_votes_voting:
    total_voting = sum(float(v["weight"]) / 1e18 for v in actual_votes_voting)
    print(f"Subgraph voting epoch total: {total_voting:,.2f} normalized votes")
if actual_votes_claim:
    total_claim = sum(float(v["weight"]) / 1e18 for v in actual_votes_claim)
    print(f"Subgraph claiming epoch total: {total_claim:,.2f} normalized votes")
