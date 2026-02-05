#!/usr/bin/env python3
import os
import sys
from datetime import datetime
from dotenv import load_dotenv

sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'src'))

from database import Database
from subgraph_client import SubgraphClient
from src.database import Vote, Bribe
from src.config import Config
from src.price_feed import PriceFeed

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

# Your votes were in epoch 1767830400 (2026-01-15 to 2026-01-22)
# They apply to epoch 1768435200 (2026-01-22 to 2026-01-29)
vote_epoch_ts = 1767830400
bribe_epoch_1 = 1768435200  # 2026-01-22
bribe_epoch_2 = 1769040000  # 2026-01-29

print(f"Analyzing your returns across TWO epochs:")
print(f"  Vote Epoch: {datetime.utcfromtimestamp(vote_epoch_ts).strftime('%Y-%m-%d')} (ts={vote_epoch_ts})")
print(f"  Bribe Epoch 1: {datetime.utcfromtimestamp(bribe_epoch_1).strftime('%Y-%m-%d')} (ts={bribe_epoch_1})")
print(f"  Bribe Epoch 2: {datetime.utcfromtimestamp(bribe_epoch_2).strftime('%Y-%m-%d')} (ts={bribe_epoch_2})")
print()

db = Database('data.db')
session = db.get_session()
client = SubgraphClient()
price_feed = PriceFeed()

# Get your votes per gauge
your_votes_per_gauge = {}
for gauge in YOUR_GAUGES:
    votes = client.fetch_all_paginated(
        client.fetch_gauge_votes,
        epoch=vote_epoch_ts,
        gauge=gauge,
    )
    your_votes = sum(float(v["weight"]) / 1e18 for v in votes if v["voter"].lower() == YOUR_ESCROW.lower())
    your_votes_per_gauge[gauge] = your_votes

print("Your votes per gauge (from subgraph):")
for gauge, votes in sorted(your_votes_per_gauge.items()):
    print(f"  {gauge[:10]}... {votes:,.2f}")
print()

def analyze_bribe_epoch(bribe_epoch_ts, label):
    print(f"=== {label} (ts={bribe_epoch_ts}) ===")
    
    bribes = session.query(Bribe).filter(
        Bribe.epoch == bribe_epoch_ts,
        Bribe.gauge.in_(YOUR_GAUGES)
    ).all()
    
    total_by_gauge = {}
    tokens_found = {}
    
    for bribe in bribes:
        if bribe.gauge not in total_by_gauge:
            total_by_gauge[bribe.gauge] = 0
            tokens_found[bribe.gauge] = {}
        
        # Get price
        price = price_feed.get_price(bribe.token, bribe_epoch_ts)
        value = float(bribe.amount) * price
        
        total_by_gauge[bribe.gauge] += value
        
        if bribe.token not in tokens_found[bribe.gauge]:
            tokens_found[bribe.gauge] = {}
        tokens_found[bribe.gauge][bribe.token] = {
            'amount': bribe.amount,
            'price': price,
            'value': value
        }
    
    # Calculate your share per gauge
    epoch_total = 0
    for gauge in YOUR_GAUGES:
        # Get total votes for this gauge
        db_votes = session.query(Vote).filter_by(epoch=bribe_epoch_ts, gauge=gauge).all()
        gauge_total_votes = sum(v.total_votes for v in db_votes)
        
        your_votes = your_votes_per_gauge.get(gauge, 0)
        your_share = your_votes / gauge_total_votes if gauge_total_votes > 0 else 0
        your_return = total_by_gauge.get(gauge, 0) * your_share
        
        epoch_total += your_return
        
        print(f"  {gauge[:10]}...")
        print(f"    Total bribes: ${total_by_gauge.get(gauge, 0):,.2f}")
        print(f"    Your votes: {your_votes:,.2f} / {gauge_total_votes:,.2f} = {your_share:.2%}")
        print(f"    Your return: ${your_return:,.2f}")
        print()
    
    print(f"  EPOCH TOTAL: ${epoch_total:,.2f}")
    print()
    return epoch_total

total_epoch_1 = analyze_bribe_epoch(bribe_epoch_1, "Epoch 1 (2026-01-22)")
total_epoch_2 = analyze_bribe_epoch(bribe_epoch_2, "Epoch 2 (2026-01-29)")

print(f"COMBINED TOTAL: ${total_epoch_1 + total_epoch_2:,.2f}")
print(f"Your actual received: $1,800.00")
print(f"Ratio: {1800 / (total_epoch_1 + total_epoch_2):.2f}x")

session.close()
