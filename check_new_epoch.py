import os
import requests
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

# Feb 5, 2026 00:00 UTC
current_epoch_ts = int(datetime(2026, 2, 5).timestamp())
print(f"Current epoch boundary: {current_epoch_ts} ({datetime.fromtimestamp(current_epoch_ts)})")

# Check if there are votes in this epoch
YOUR_ESCROW = "0x768a675B8542F23C428C6672738E380176E7635C"
subgraph_url = os.getenv("SUBGRAPH_URL")

query = f"""
{{
  gaugeVotes(first: 100, where: {{voter: "{YOUR_ESCROW.lower()}", epoch: {current_epoch_ts}}}) {{
    epoch
    gauge {{
      address
    }}
    weight
  }}
}}
"""

response = requests.post(subgraph_url, json={"query": query}, timeout=30)
result = response.json()

if "data" in result and result["data"]:
    votes = result["data"]["gaugeVotes"]
    if votes:
        print(f"\nFound {len(votes)} votes in Feb 5 epoch!")
        for vote in votes[:3]:
            print(f"  Gauge: {vote['gauge']['address']}, Weight: {int(vote['weight'])/1e18:,.2f}")
    else:
        print("\nNo votes found in Feb 5 epoch (current)")
else:
    print("\nError querying:", result)
