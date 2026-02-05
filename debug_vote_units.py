#!/usr/bin/env python3
"""
Deep dive into what the numbers actually represent.
"""

from src.database import Database
from src.subgraph_client import SubgraphClient
from config import Config

db = Database(Config.DATABASE_PATH)
voter_power = 1530896  # From config

epoch = 1764806400
subgraph = SubgraphClient(Config.SUBGRAPH_URL)
voter = "0x768a675B8542F23C428C6672738E380176E7635C"

# Get your actual votes from subgraph
subgraph_votes = subgraph.fetch_all_paginated(
    subgraph.fetch_gauge_votes,
    epoch=epoch,
    voter=voter,
)

your_subgraph_votes = {}
for vote in subgraph_votes:
    gauge = vote["gauge"]["address"].lower()
    your_subgraph_votes[gauge] = int(vote["weight"])

print(f"\n{'='*100}")
print(f"YOUR VOTING POWER: {voter_power:,}")
print(f"YOUR TOTAL SUBGRAPH VOTES: {sum(your_subgraph_votes.values()):,} wei")
print(f"{'='*100}\n")

# What if the subgraph wei needs to be divided by 1e18 to get real wei?
print("HYPOTHESIS 1: Subgraph votes are in standard wei (need 1e18 conversion)")
for gauge, wei in list(your_subgraph_votes.items())[:2]:
    converted = wei / 1e18
    print(f"  {gauge[:10]}...: {wei} wei → {converted:.2f} tokens")
    
print(f"\nTotal if divided by 1e18: {sum(your_subgraph_votes.values()) / 1e18:.2f}")

# What if they need different divisor?
print("\n\nHYPOTHESIS 2: What divisor would give us your actual voting power?")
total_wei = sum(your_subgraph_votes.values())
if total_wei > 0:
    divisor = total_wei / voter_power
    print(f"  Required divisor: {divisor:.2e}")
    print(f"  {total_wei} / {divisor:.2e} = {total_wei / divisor:.2f}")

# Check what's in the votes table more carefully
print("\n\nVOTES TABLE DETAILS:")
votes = db.get_votes_for_epoch(epoch)
print(f"Total votes in DB: {sum(v.total_votes for v in votes):.2f}")
print(f"Number of vote entries: {len(votes)}")

# Maybe the votes in the database are already your normalized share?
# Or maybe they're total pool votes, not your votes?
print(f"\nDo DB votes match your expected voting power?")
print(f"  Your voting power: {voter_power:,}")
print(f"  Total DB votes: {sum(v.total_votes for v in votes):,.2f}")
print(f"  Ratio: {sum(v.total_votes for v in votes) / voter_power:.2f}x")

# Compare subgraph votes with DB votes per gauge
print(f"\n\nDETAILED COMPARISON (first 5 gauges with subgraph data):")
db_by_gauge = {v.gauge.lower(): v.total_votes for v in votes}
count = 0
for gauge, sg_wei in list(your_subgraph_votes.items())[:5]:
    db_votes = db_by_gauge.get(gauge, 0)
    print(f"\n  Gauge {gauge[:10]}...:")
    print(f"    Subgraph wei:   {sg_wei:>25,}")
    print(f"    DB votes:       {db_votes:>25,.2f}")
    if db_votes > 0:
        # Try different conversions
        print(f"    SG/DB ratio:    {sg_wei / db_votes:>25,.2e}")
        print(f"    SG/(1e18 * DB): {sg_wei / (1e18 * db_votes):>25,.2e}")
        print(f"    SG/1e18:        {sg_wei / 1e18:>25,.2f}")

print(f"\n{'='*100}\n")
