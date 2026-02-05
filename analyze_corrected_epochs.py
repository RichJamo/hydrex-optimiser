#!/usr/bin/env python3
import os
import sys
from datetime import datetime
from dotenv import load_dotenv

sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'src'))

from database import Database
from subgraph_client import SubgraphClient
from database import Vote, Bribe, Gauge
from config import Config
from price_feed import PriceFeed

load_dotenv()

# User's escrow
YOUR_ESCROW = "0x768a675B8542F23C428C6672738E380176E7635C"

# Your 4 gauges
YOUR_GAUGES = {
    "0x07388f67042bc2dc54876e0c99e543625bd2a9da",
    "0x22f0afdda80fbca0d96e29384814a897cbadab59",
    "0xac396cabf5832a49483b78225d902c0999829993",
    "0xee5f8bf7cdb1ad421993a368b15d06ad58122dab",
}

# CORRECTED LOGIC: Bribes from epoch N apply to epoch N+1 rewards
# User voted most recently in epoch 1768435200 (2026-01-15 to 2026-01-22)
# Those votes determine bribes in epoch 1768435200
# Those bribes pay out in epoch 1769040000 (2026-01-29 onwards)
# User can claim in epoch 1769040000

vote_epoch_ts = 1768435200      # When user voted most recently (2026-01-15)
claim_epoch_1 = 1769040000      # When first rewards from those votes are paid (2026-01-29, bribes from vote_epoch)
claim_epoch_2 = 1769645000      # When second rewards are paid (2026-02-05, bribes from claim_epoch_1)

print(f"CORRECTED ANALYSIS - Bribes from epoch N apply to epoch N+1")
print()
print(f"Vote Epoch: {datetime.utcfromtimestamp(vote_epoch_ts).strftime('%Y-%m-%d')} (ts={vote_epoch_ts})")
print(f"  Your votes: apply to bribes in THIS epoch")
print()
print(f"Claim Epoch 1: {datetime.utcfromtimestamp(claim_epoch_1).strftime('%Y-%m-%d')} (ts={claim_epoch_1})")
print(f"  Bribes from vote_epoch apply here")
print()
print(f"Claim Epoch 2: {datetime.utcfromtimestamp(claim_epoch_2).strftime('%Y-%m-%d')} (ts={claim_epoch_2})")
print(f"  Bribes from claim_epoch_1 apply here")
print()
print("-" * 80)
print()

db = Database('data.db')
session = db.get_session()
client = SubgraphClient()
price_feed = PriceFeed()

# Get your votes per gauge (from voting epoch)
your_votes_per_gauge = {}
for gauge in YOUR_GAUGES:
    votes = client.fetch_all_paginated(
        client.fetch_gauge_votes,
        epoch=vote_epoch_ts,
        gauge=gauge,
    )
    your_votes = sum(float(v["weight"]) / 1e18 for v in votes if v["voter"].lower() == YOUR_ESCROW.lower())
    your_votes_per_gauge[gauge] = your_votes

print("Your votes per gauge (from voting epoch):")
for gauge, votes in sorted(your_votes_per_gauge.items()):
    print(f"  {gauge[:10]}... {votes:,.2f}")
print()
print("=" * 80)
print()

def analyze_bribe_epoch(bribe_epoch_ts, claim_epoch_ts, label):
    print(f"{label}")
    print(f"  Looking at bribes from epoch {datetime.utcfromtimestamp(bribe_epoch_ts).strftime('%Y-%m-%d')} (ts={bribe_epoch_ts})")
    print(f"  These apply to rewards in epoch {datetime.utcfromtimestamp(claim_epoch_ts).strftime('%Y-%m-%d')} (ts={claim_epoch_ts})")
    print()
    
    # Build bribe_contract -> gauge mapping
    bribe_to_gauge = {}
    for gauge in session.query(Gauge).all():
        if gauge.internal_bribe:
            bribe_to_gauge[gauge.internal_bribe.lower()] = gauge.address.lower()
        if gauge.external_bribe:
            bribe_to_gauge[gauge.external_bribe.lower()] = gauge.address.lower()
    
    bribes = session.query(Bribe).filter(
        Bribe.epoch == bribe_epoch_ts
    ).all()
    
    # Get all unique tokens and fetch prices for this epoch
    unique_tokens = list(set(b.reward_token for b in bribes))
    token_prices = price_feed.get_batch_prices_for_timestamp(unique_tokens, bribe_epoch_ts)
    
    # Filter bribes to only your gauges and aggregate
    total_by_gauge = {}
    
    for bribe in bribes:
        gauge_addr = bribe_to_gauge.get(bribe.bribe_contract.lower())
        if gauge_addr and gauge_addr in [g.lower() for g in YOUR_GAUGES]:
            if gauge_addr not in total_by_gauge:
                total_by_gauge[gauge_addr] = 0.0
            
            # Get price at BRIBE epoch (when bribes were placed)
            price = token_prices.get(bribe.reward_token.lower(), 0.0)
            value = float(bribe.amount) * price
            
            total_by_gauge[gauge_addr] += value
    
    # Calculate your share per gauge
    epoch_total = 0.0
    print(f"{'Gauge':<12} {'Total Bribes':<18} {'Your Votes':<18} {'Gauge Total':<18} {'Your Share':<12} {'Your Return':<15}")
    print("-" * 95)
    
    for gauge in sorted(YOUR_GAUGES):
        gauge_lower = gauge.lower()
        # Get total votes for this gauge (from voting epoch)
        db_votes = session.query(Vote).filter_by(epoch=vote_epoch_ts, gauge=gauge).all()
        gauge_total_votes = sum(v.total_votes for v in db_votes)
        
        your_votes = your_votes_per_gauge.get(gauge, 0)
        gauge_bribes = total_by_gauge.get(gauge_lower, 0)
        your_share = your_votes / gauge_total_votes if gauge_total_votes > 0 else 0
        your_return = gauge_bribes * your_share
        
        epoch_total += your_return
        
        print(f"{gauge[:10]}... ${gauge_bribes:>15,.2f} {your_votes:>17,.2f} {gauge_total_votes:>17,.2f} {your_share:>10.2%} ${your_return:>13,.2f}")
    
    print()
    print(f"EPOCH TOTAL: ${epoch_total:,.2f}")
    print()
    return epoch_total

# Analyze first claim epoch (bribes from voting epoch)
total_1 = analyze_bribe_epoch(vote_epoch_ts, claim_epoch_1, "=== CLAIM EPOCH 1 ===")

# Analyze second claim epoch (bribes from first claim epoch)
total_2 = analyze_bribe_epoch(claim_epoch_1, claim_epoch_2, "=== CLAIM EPOCH 2 ===")

print("=" * 80)
print(f"COMBINED TOTAL: ${total_1 + total_2:,.2f}")
print(f"Your actual received: $1,800.00")
if total_1 + total_2 > 0:
    ratio = 1800 / (total_1 + total_2)
    print(f"Ratio: {ratio:.2f}x")
else:
    print("Ratio: N/A (calculated total is 0)")

session.close()
