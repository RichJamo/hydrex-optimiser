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

# Check specific epochs
test_epochs = [
    1768435200,    # 2026-01-15
    1769040000,    # 2026-01-22
    1769645600,    # 2026-01-29
    1770251200,    # 2026-02-05
]

print("Checking specific epochs for your votes:")
print()

for epoch in test_epochs:
    epoch_date = datetime.utcfromtimestamp(epoch).strftime('%Y-%m-%d')
    
    found_votes = False
    for gauge in YOUR_GAUGES:
        votes = client.fetch_all_paginated(
            client.fetch_gauge_votes,
            epoch=epoch,
            gauge=gauge,
        )
        your_votes = sum(float(v["weight"]) / 1e18 for v in votes if v["voter"].lower() == YOUR_ESCROW.lower())
        
        if your_votes > 0:
            if not found_votes:
                print(f"{epoch_date} (ts={epoch}):")
                found_votes = True
            print(f"  {gauge[:10]}... {your_votes:,.2f}")
    
    if found_votes:
        print()
    else:
        print(f"{epoch_date} (ts={epoch}): No votes")
        print()
