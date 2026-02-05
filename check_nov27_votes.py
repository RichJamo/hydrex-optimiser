#!/usr/bin/env python3
import os
import requests
from dotenv import load_dotenv

load_dotenv()

YOUR_ESCROW = "0x768a675B8542F23C428C6672738E380176E7635C"
EPOCH_NOV27 = 1764201600

query = """
{
  gaugeVotes(first: 1000, where: {voter: "%s", epoch: %d}) {
    epoch
    gauge {
      address
    }
    weight
  }
}
""" % (YOUR_ESCROW.lower(), EPOCH_NOV27)

subgraph_url = os.getenv("SUBGRAPH_URL")

response = requests.post(
    subgraph_url,
    json={"query": query},
    timeout=30
)

result = response.json()

if "data" in result and result["data"] and result["data"].get("gaugeVotes"):
    votes = result["data"]["gaugeVotes"]
    print(f"Found {len(votes)} votes for epoch {EPOCH_NOV27} (Nov 27):")
    print()
    total = 0
    for vote in votes:
        gauge = vote["gauge"]["address"]
        weight = int(vote["weight"]) / 1e18
        total += weight
        print(f"  Gauge {gauge}: {weight:,.2f} votes")
    print()
    print(f"Total votes cast: {total:,.2f}")
else:
    print("Error:", result)
