#!/usr/bin/env python3
import os
import sys
from datetime import datetime
from dotenv import load_dotenv

sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'src'))

from database import Database
from subgraph_client import SubgraphClient
from database import Vote, Bribe, Gauge
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

# CORRECTED: Bribes from epoch N apply to epoch N+1 rewards
# Your MOST RECENT votes: 2026-01-15 (ts=1768435200)
# Your OLD votes: 2026-01-08 (ts=1767830400)

vote_epoch_old = 1767830400        # 2026-01-08: Your old votes
vote_epoch_recent = 1768435200     # 2026-01-15: Your RECENT votes

bribe_epoch_old = 1767830400       # Bribes from 2026-01-08 votes
claim_epoch_1 = 1768435200         # 2026-01-15: Claims from old votes

bribe_epoch_recent = 1768435200    # Bribes from 2026-01-15 votes
claim_epoch_2 = 1769040000         # 2026-01-22: Claims from recent votes

print(f"CORRECTED ANALYSIS - Bribes from epoch N apply to epoch N+1")
print()
print(f"SCENARIO 1: Old votes from 2026-01-08")
print(f"  → Bribes from 2026-01-08 (ts={bribe_epoch_old})")
print(f"  → Claims available in 2026-01-15 (ts={claim_epoch_1})")
print()
print(f"SCENARIO 2: Recent votes from 2026-01-15")
print(f"  → Bribes from 2026-01-15 (ts={bribe_epoch_recent})")
print(f"  → Claims available in 2026-01-22 (ts={claim_epoch_2})")
print()
print("-" * 80)
print()

db = Database('data.db')
session = db.get_session()
client = SubgraphClient()
price_feed = PriceFeed()

def get_your_votes_for_epoch(epoch_ts):
    """Fetch your actual votes per gauge for a given epoch"""
    your_votes_per_gauge = {}
    for gauge in YOUR_GAUGES:
        votes = client.fetch_all_paginated(
            client.fetch_gauge_votes,
            epoch=epoch_ts,
            gauge=gauge,
        )
        your_votes = sum(float(v["weight"]) / 1e18 for v in votes if v["voter"].lower() == YOUR_ESCROW.lower())
        your_votes_per_gauge[gauge] = your_votes
    return your_votes_per_gauge

def analyze_scenario(vote_epoch_ts, bribe_epoch_ts, claim_epoch_ts, scenario_label):
    """Analyze bribes from vote epoch that apply to claim epoch"""
    print(f"=== {scenario_label} ===")
    print(f"Your votes from {datetime.utcfromtimestamp(vote_epoch_ts).strftime('%Y-%m-%d')} (ts={vote_epoch_ts})")
    print(f"Bribes from {datetime.utcfromtimestamp(bribe_epoch_ts).strftime('%Y-%m-%d')} (ts={bribe_epoch_ts})")
    print(f"Claims in {datetime.utcfromtimestamp(claim_epoch_ts).strftime('%Y-%m-%d')} (ts={claim_epoch_ts})")
    print()
    
    # Get your votes
    your_votes = get_your_votes_for_epoch(vote_epoch_ts)
    
    print("Your votes per gauge:")
    for gauge, votes in sorted(your_votes.items()):
        print(f"  {gauge[:10]}... {votes:,.2f}")
    print()
    
    # Build bribe_contract -> gauge mapping
    bribe_to_gauge = {}
    for gauge in session.query(Gauge).all():
        if gauge.internal_bribe:
            bribe_to_gauge[gauge.internal_bribe.lower()] = gauge.address.lower()
        if gauge.external_bribe:
            bribe_to_gauge[gauge.external_bribe.lower()] = gauge.address.lower()
    
    # Get bribes from bribe epoch
    bribes = session.query(Bribe).filter(Bribe.epoch == bribe_epoch_ts).all()
    
    # Get all unique tokens for batch price fetching
    unique_tokens = list(set(b.reward_token for b in bribes))
    token_prices = price_feed.get_batch_prices_for_timestamp(unique_tokens, bribe_epoch_ts, granularity="hour")
    
    # Aggregate bribes by gauge
    total_by_gauge = {}
    for bribe in bribes:
        gauge_addr = bribe_to_gauge.get(bribe.bribe_contract.lower())
        if gauge_addr and gauge_addr in [g.lower() for g in YOUR_GAUGES]:
            if gauge_addr not in total_by_gauge:
                total_by_gauge[gauge_addr] = 0.0
            
            # Get price at bribe epoch
            price = token_prices.get(bribe.reward_token.lower(), 0.0)
            value = float(bribe.amount) * price
            total_by_gauge[gauge_addr] += value
    
    # Calculate your returns
    epoch_total = 0.0
    print(f"{'Gauge':<12} {'Total Bribes':<18} {'Your Votes':<18} {'Gauge Total':<18} {'Your Share':<12} {'Your Return':<15}")
    print("-" * 95)
    
    for gauge in sorted(YOUR_GAUGES):
        gauge_lower = gauge.lower()
        
        # Get total votes for this gauge (from voting epoch)
        db_votes = session.query(Vote).filter_by(epoch=vote_epoch_ts, gauge=gauge).all()
        gauge_total_votes = sum(v.total_votes for v in db_votes)
        
        your_vote_amount = your_votes.get(gauge, 0)
        gauge_bribes = total_by_gauge.get(gauge_lower, 0)
        your_share = your_vote_amount / gauge_total_votes if gauge_total_votes > 0 else 0
        your_return = gauge_bribes * your_share
        
        epoch_total += your_return
        
        print(f"{gauge[:10]}... ${gauge_bribes:>15,.2f} {your_vote_amount:>17,.2f} {gauge_total_votes:>17,.2f} {your_share:>10.2%} ${your_return:>13,.2f}")
    
    print()
    print(f"SCENARIO TOTAL: ${epoch_total:,.2f}")
    print()
    return epoch_total

# Analyze both scenarios
total_old = analyze_scenario(vote_epoch_old, bribe_epoch_old, claim_epoch_1, "SCENARIO 1: Old Votes → Old Bribes")
total_recent = analyze_scenario(vote_epoch_recent, bribe_epoch_recent, claim_epoch_2, "SCENARIO 2: Recent Votes → Recent Bribes")

print("=" * 80)
print(f"SCENARIO 1 (Old votes/bribes): ${total_old:,.2f}")
print(f"SCENARIO 2 (Recent votes/bribes): ${total_recent:,.2f}")
print(f"COMBINED TOTAL: ${total_old + total_recent:,.2f}")
print(f"Your actual received: $1,800.00")
if total_old + total_recent > 0:
    ratio = 1800 / (total_old + total_recent)
    print(f"Ratio: {ratio:.2f}x")
else:
    print("Ratio: N/A (calculated total is 0)")

session.close()
