#!/usr/bin/env python3
import os
import sys
from datetime import datetime
from dotenv import load_dotenv

sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'src'))

from subgraph_client import SubgraphClient

load_dotenv()

YOUR_ESCROW = "0x768a675B8542F23C428C6672738E380176E7635C"

client = SubgraphClient()

target_epoch = 1769040000
epoch_date = datetime.utcfromtimestamp(target_epoch).strftime('%Y-%m-%d')

print(f"Your complete vote breakdown in epoch {epoch_date} (ts={target_epoch}):")
print()

# Fetch all votes for this epoch without gauge filter
query = """
{
  gaugeVotes(first: 1000, where: {epoch: %d, voter: "%s"}) {
    id
    voter
    gauge {
      address
    }
    weight
    epoch
  }
}
""" % (target_epoch, YOUR_ESCROW.lower())

import requests
subgraph_url = os.getenv("SUBGRAPH_URL")

response = requests.post(
    subgraph_url,
    json={"query": query},
    timeout=30
)

result = response.json()

if "data" in result and result["data"] and result["data"].get("gaugeVotes"):
    votes = result["data"]["gaugeVotes"]
    
    print(f"{'Gauge':<12} {'Votes':<18}")
    print("-" * 35)
    
    total_votes = 0
    for vote in sorted(votes, key=lambda v: float(v["weight"]), reverse=True):
        weight = float(vote["weight"]) / 1e18
        gauge = vote["gauge"]["address"]
        total_votes += weight
        print(f"{gauge[:10]}... {weight:>16,.2f}")
    
    print("-" * 35)
    print(f"{'TOTAL':<10} {total_votes:>17,.2f}")
    print()
    print(f"Number of gauges voted: {len(votes)}")

else:
    print("Error fetching votes:", result)
