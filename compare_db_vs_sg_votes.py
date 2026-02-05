#!/usr/bin/env python3
import os
import sys
from datetime import datetime
from dotenv import load_dotenv

sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'src'))

from database import Database
from subgraph_client import SubgraphClient
from src.database import Vote

load_dotenv()

bribe_epoch_ts = 1768435200
epoch_date = datetime.utcfromtimestamp(bribe_epoch_ts).strftime('%Y-%m-%d')

print(f"Comparing database vs subgraph vote totals for epoch: {epoch_date} (ts={bribe_epoch_ts})")
print()

# Your 4 gauges (from previous output)
your_gauges = {
    "0x07388f67042bc2dc54876e0c99e543625bd2a9da": "0x07388f",
    "0x22f0afdda80fbca0d96e29384814a897cbadab59": "0x22f0af",
    "0xac396cabf5832a49483b78225d902c0999829993": "0xac396c",
    "0xee5f8bf7cdb1ad421993a368b15d06ad58122dab": "0xee5f8b",
}

client = SubgraphClient()
db = Database('data.db')
session = db.get_session()

print(f"{'Gauge':<10} {'Database Total':<18} {'Subgraph Total':<18} {'Ratio (SG/DB)':<15} {'Match?':<10}")
print("-" * 75)

for full_addr, short_addr in your_gauges.items():
    # Get database total
    db_votes = session.query(Vote).filter_by(epoch=bribe_epoch_ts, gauge=full_addr).all()
    db_total = sum(v.total_votes for v in db_votes)
    
    # Get subgraph total - fetch all gauge votes for this gauge in this epoch
    sg_votes = client.fetch_all_paginated(
        client.fetch_gauge_votes,
        epoch=bribe_epoch_ts,
        gauge=full_addr,
    )
    sg_total = sum(float(v["weight"]) / 1e18 for v in sg_votes)
    
    ratio = sg_total / db_total if db_total > 0 else 0
    match = "✓ MATCH" if abs(ratio - 1.0) < 0.01 else "✗ DIFF"
    
    print(f"{short_addr:<10} {db_total:<18,.2f} {sg_total:<18,.2f} {ratio:<14.2f}x {match:<10}")

print()
print("If SUBGRAPH totals are significantly higher than DATABASE, it means:")
print("- The Vote table is missing votes")
print("- Or votes are recorded differently in database vs subgraph")

session.close()
