#!/usr/bin/env python3
import os
import sys
from datetime import datetime
from dotenv import load_dotenv

sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'src'))

from subgraph_client import SubgraphClient

load_dotenv()

YOUR_ESCROW = "0x768a675B8542F23C428C6672738E380176E7635C"

YOUR_GAUGES = {
    "0x07388f67042bc2dc54876e0c99e543625bd2a9da",
    "0x22f0afdda80fbca0d96e29384814a897cbadab59",
    "0xac396cabf5832a49483b78225d902c0999829993",
    "0xee5f8bf7cdb1ad421993a368b15d06ad58122dab",
}

client = SubgraphClient()

# Your Jan 28 vote should be in epoch 1769040000 (2026-01-22 to 2026-01-29)
target_epoch = 1769040000
epoch_date = datetime.utcfromtimestamp(target_epoch).strftime('%Y-%m-%d')

print(f"Checking epoch {epoch_date} (ts={target_epoch}) for your votes...")
print()

found_votes = False
for gauge in YOUR_GAUGES:
    votes = client.fetch_all_paginated(
        client.fetch_gauge_votes,
        epoch=target_epoch,
        gauge=gauge,
    )
    your_votes = sum(float(v["weight"]) / 1e18 for v in votes if v["voter"].lower() == YOUR_ESCROW.lower())
    
    if your_votes > 0:
        if not found_votes:
            print(f"✓ Found votes in epoch {epoch_date}:")
            found_votes = True
        print(f"  {gauge[:10]}... {your_votes:,.2f}")

if not found_votes:
    print(f"✗ No votes found in epoch {epoch_date}")
    print()
    print("Checking if the subgraph has any data for this epoch...")
    
    # Try to fetch any votes for this gauge/epoch
    votes = client.fetch_all_paginated(
        client.fetch_gauge_votes,
        epoch=target_epoch,
        gauge=YOUR_GAUGES.pop(),
    )
    
    if votes:
        print(f"✓ The subgraph DOES have data for epoch {target_epoch}")
        print(f"  Found {len(votes)} total votes (not necessarily yours)")
    else:
        print(f"✗ The subgraph has NO data for epoch {target_epoch}")
        print(f"  This epoch may not have been indexed yet")
