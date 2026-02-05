#!/usr/bin/env python3
import os
import sys
from datetime import datetime, timedelta
from dotenv import load_dotenv

sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'src'))

from subgraph_client import SubgraphClient
from config import Config

load_dotenv()

YOUR_ESCROW = "0x768a675B8542F23C428C6672738E380176E7635C"

YOUR_GAUGES = {
    "0x07388f67042bc2dc54876e0c99e543625bd2a9da",
    "0x22f0afdda80fbca0d96e29384814a897cbadab59",
    "0xac396cabf5832a49483b78225d902c0999829993",
    "0xee5f8bf7cdb1ad421993a368b15d06ad58122dab",
}

client = SubgraphClient()

print(f"Querying subgraph directly for all your votes (not limited to database epochs)...")
print()

# Get all gauge votes directly from subgraph
all_votes = client.fetch_all_paginated(
    client.fetch_gauge_votes,
    voter=YOUR_ESCROW.lower(),
)

# Organize by epoch
your_votes_by_epoch = {}

for vote in all_votes:
    epoch = int(vote["epoch"])
    gauge = vote["gauge"]["address"].lower()
    weight = float(vote["weight"]) / 1e18
    
    if gauge in [g.lower() for g in YOUR_GAUGES]:
        if epoch not in your_votes_by_epoch:
            your_votes_by_epoch[epoch] = {}
        your_votes_by_epoch[epoch][gauge] = weight

if your_votes_by_epoch:
    print("All your voting epochs (from subgraph):")
    print()
    for epoch in sorted(your_votes_by_epoch.keys(), reverse=True):
        epoch_date = datetime.utcfromtimestamp(epoch).strftime('%Y-%m-%d')
        total = sum(your_votes_by_epoch[epoch].values())
        print(f"{epoch_date} (ts={epoch}):")
        for gauge, votes in sorted(your_votes_by_epoch[epoch].items()):
            print(f"  {gauge[:10]}... {votes:,.2f}")
        print(f"  Total: {total:,.2f}")
        print()
else:
    print("No votes found for your escrow address")
