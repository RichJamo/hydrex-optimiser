#!/usr/bin/env python3
import os
import requests
from dotenv import load_dotenv

load_dotenv()

YOUR_ESCROW = "0x768a675B8542F23C428C6672738E380176E7635C"

epochs_to_check = {
    "Jan 15": 1768435200,
    "Nov 27": 1764201600,
}

subgraph_url = os.getenv("SUBGRAPH_URL")

for name, epoch in epochs_to_check.items():
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
    """ % (YOUR_ESCROW.lower(), epoch)

    response = requests.post(
        subgraph_url,
        json={"query": query},
        timeout=30
    )

    result = response.json()

    if "data" in result and result["data"] and result["data"].get("gaugeVotes"):
        votes = result["data"]["gaugeVotes"]
        print(f"{name} epoch ({epoch}):")
        total = 0
        for vote in votes:
            gauge = vote["gauge"]["address"]
            weight = int(vote["weight"]) / 1e18
            total += weight
            print(f"  {gauge}: {weight:,.2f} votes")
        print(f"  TOTAL: {total:,.2f} votes across {len(votes)} gauges")
        print()
