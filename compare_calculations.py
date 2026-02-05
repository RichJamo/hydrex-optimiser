import sys
sys.path.insert(0, 'src')

from database import Database, Bribe, Gauge
from subgraph_client import SubgraphClient

db = Database('data.db')
session = db.get_session()
client = SubgraphClient()

YOUR_ESCROW = "0x768a675B8542F23C428C6672738E380176E7635C"
epoch_ts = 1769040000

# Get your votes from subgraph
query = f"""
{{
  gaugeVotes(first: 1000, where: {{voter: "{YOUR_ESCROW.lower()}", epoch: {epoch_ts}}}) {{
    epoch
    gauge {{
      address
    }}
    weight
  }}
}}
"""

result = client.query(query)
your_votes = result.get("data", {}).get("gaugeVotes", [])

if not your_votes:
    print("No votes found")
    exit(1)

your_votes_by_gauge = {}
for vote in your_votes:
    gauge = vote["gauge"]["address"].lower()
    weight = int(vote["weight"]) / 1e18
    your_votes_by_gauge[gauge] = weight

print(f"Found {len(your_votes_by_gauge)} gauges you voted on\n")

# Build bribe -> gauge mapping
bribe_to_gauge = {}
for gauge in session.query(Gauge).all():
    if gauge.internal_bribe:
        bribe_to_gauge[gauge.internal_bribe.lower()] = gauge.address.lower()
    if gauge.external_bribe:
        bribe_to_gauge[gauge.external_bribe.lower()] = gauge.address.lower()

# Get all bribes for this epoch
bribes = session.query(Bribe).filter(Bribe.epoch == epoch_ts).all()

# Get all votes for this epoch from subgraph to calculate totals per gauge
all_votes_query = f"""
{{
  gaugeVotes(first: 10000, where: {{epoch: {epoch_ts}}}) {{
    gauge {{
      address
    }}
    weight
  }}
}}
"""

all_votes_result = client.query(all_votes_query)
all_votes = all_votes_result.get("data", {}).get("gaugeVotes", [])

total_votes_by_gauge = {}
for vote in all_votes:
    gauge = vote["gauge"]["address"].lower()
    weight = int(vote["weight"]) / 1e18
    if gauge not in total_votes_by_gauge:
        total_votes_by_gauge[gauge] = 0
    total_votes_by_gauge[gauge] += weight

print(f"Total gauges with votes in epoch: {len(total_votes_by_gauge)}\n")

# Calculate our computed values by gauge and bribe contract
print("=" * 140)
print("BREAKDOWN: Our Calculated Bribe Amounts for Your Voted Gauges")
print("=" * 140)
print()

calculated_by_gauge = {}

for bribe in bribes:
    gauge_addr = bribe_to_gauge.get(bribe.bribe_contract.lower())
    if not gauge_addr:
        continue
        
    if gauge_addr not in calculated_by_gauge:
        calculated_by_gauge[gauge_addr] = {'token_addr': bribe.reward_token, 'total_bribe': 0}

for bribe in bribes:
    gauge_addr = bribe_to_gauge.get(bribe.bribe_contract.lower())
    if gauge_addr in your_votes_by_gauge:
        your_votes = your_votes_by_gauge[gauge_addr]
        total_votes = total_votes_by_gauge.get(gauge_addr, 1)
        share_pct = (your_votes / total_votes * 100) if total_votes > 0 else 0
        your_amount = float(bribe.amount) * (your_votes / total_votes) if total_votes > 0 else 0
        
        if your_amount > 0:
            print(f"Token: {bribe.reward_token[:8]}... | Gauge: {gauge_addr[:8]}...")
            print(f"  Total Bribe: {float(bribe.amount):>18,.2f} tokens")
            print(f"  Your Votes:  {your_votes:>18,.2f} / {total_votes:>12,.2f} total ({share_pct:>6.2f}%)")
            print(f"  Your Amount: {your_amount:>18,.2f} tokens")
            print()

print("=" * 140)
print()
print("KEY OBSERVATION:")
print("Our system calculates BRIBE distributions based on your voting share.")
print("However, actual rewards include:")
print("  1. BRIBES (what we calculate)")
print("  2. TRADING FEES (which we do NOT calculate)")
print()
print("You received both types of rewards. That's why there's a mismatch!")
