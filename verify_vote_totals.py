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

print(f"Verifying total vote counts for epoch: {epoch_date} (ts={bribe_epoch_ts})")
print()

# Your gauges
your_gauges = [
    "0x07388f",  # truncated for display
    "0x22f0af",
    "0xac396c",
    "0xee5f8b",
]

# Database totals (from our earlier analysis)
db_totals = {
    "0x07388f": 10406198.50,
    "0x22f0af": 7575231.69,
    "0xac396c": 17996216.14,
    "0xee5f8b": 8084928.88,
}

# Get subgraph data
client = SubgraphClient()

print(f"{'Gauge':<10} {'Database Votes':<18} {'Subgraph Votes':<18} {'Ratio (SG/DB)':<15}")
print("-" * 65)

for gauge_prefix in your_gauges:
    # This is tricky - we need the full gauge address
    # From our earlier analysis, we know these are 4 gauges you voted for
    # Let me fetch from subgraph for all votes in that epoch and filter
    pass

# Actually, let me do this differently - fetch all gauge votes for the epoch 
# and show which gauges had votes, then we can match them

all_gauge_votes = client.fetch_all_paginated(
    client.fetch_gauge_votes,
    epoch=bribe_epoch_ts,
    voter="0x0000000000000000000000000000000000000000",  # dummy to get all
)

print("This approach won't work - need to fetch per gauge or get all votes differently")
print()

# Better approach: get your votes to identify the 4 gauge addresses
escrow = "0x768a675B8542F23C428C6672738E380176E7635C"
your_votes = client.fetch_all_paginated(
    client.fetch_gauge_votes,
    epoch=bribe_epoch_ts,
    voter=escrow,
)

your_gauge_addrs = {v["gauge"]["address"].lower(): v for v in your_votes}

print(f"Your 4 gauges from subgraph:")
for gauge_addr in your_gauge_addrs.keys():
    print(f"  {gauge_addr}")

print()
print(f"{'Gauge':<10} {'Your Votes':<18} {'Database Total':<18} {'Subgraph Total':<18} {'Ratio (SG/DB)':<15}")
print("-" * 85)

# Now get database totals for these gauges
db = Database('data.db')
session = db.get_session()

db_votes = session.query(Vote).filter_by(epoch=bribe_epoch_ts).all()
db_gauge_totals = {}
for vote in db_votes:
    gauge_addr = vote.gauge.lower()
    if gauge_addr not in db_gauge_totals:
        db_gauge_totals[gauge_addr] = 0.0
    db_gauge_totals[gauge_addr] += vote.total_votes

# Compare
for gauge_addr, your_vote_data in your_gauge_addrs.items():
    your_votes_wei = float(your_vote_data["weight"])
    your_votes_norm = your_votes_wei / 1e18
    
    db_total = db_gauge_totals.get(gauge_addr, 0.0)
    
    # Try to get subgraph total - this would require a different query
    # For now just show database vs your votes
    
    print(f"{gauge_addr[:8]}... {your_votes_norm:<18,.2f} {db_total:<18,.2f} {'TBD':<18} {db_total/your_votes_norm if your_votes_norm > 0 else 0:<14.2f}x")

print()
print("Note: To get actual subgraph totals, we'd need to query the subgraph for GaugeVote")
print("      aggregated per gauge (not per voter). This may require a different query.")
print()
print("What we can verify:")
print("- Database totals seem reasonable if they're aggregating all Vote records")
print("- Your share appears to be 1.6%-3.9% based on these numbers")
print("- If your actual reward was 5.29x higher, it would imply database vote totals are 5.29x too low")

session.close()
