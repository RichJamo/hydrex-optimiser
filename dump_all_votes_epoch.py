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

print(f"All votes in epoch {epoch_date} (ts={target_epoch}):")
print()

# Fetch all votes for this epoch without gauge filter
query = """
{
  gaugeVotes(first: 1000, where: {epoch: %d}) {
    id
    voter
    gauge {
      address
    }
    weight
    epoch
  }
}
""" % target_epoch

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
    
    # Group by voter
    votes_by_voter = {}
    for vote in votes:
        voter = vote["voter"].lower()
        if voter not in votes_by_voter:
            votes_by_voter[voter] = []
        votes_by_voter[voter].append(vote)
    
    print(f"Found {len(votes)} votes from {len(votes_by_voter)} voters")
    print()
    
    # Check if your address is there
    if YOUR_ESCROW.lower() in votes_by_voter:
        print(f"✓ FOUND YOUR VOTES!")
        for vote in votes_by_voter[YOUR_ESCROW.lower()]:
            weight = float(vote["weight"]) / 1e18
            gauge = vote["gauge"]["address"]
            print(f"  {gauge[:10]}... {weight:,.2f}")
    else:
        print(f"✗ Your address ({YOUR_ESCROW}) not found in this epoch")
        print()
        print("Sample of voters in this epoch:")
        for i, voter in enumerate(list(votes_by_voter.keys())[:5]):
            print(f"  {voter}")

else:
    print("Error fetching votes:", result)
