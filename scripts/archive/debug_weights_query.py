#!/usr/bin/env python3
"""
Debug why weights() is returning 0 for all pools.
"""

import json
from web3 import Web3

# Configuration
RPC_URL = "https://base-mainnet.g.alchemy.com/v2/oFfvEpXYjGo8Nj4QQIkU3kXd6Z0JvfJZ"
VOTER_ADDRESS = "0xc69E3eF39E3fFBcE2A1c570f8d3ADF76909ef17b"

# Setup Web3
w3 = Web3(Web3.HTTPProvider(RPC_URL))
print(f"Connected to Base: {w3.is_connected()}\n")

# Load ABI
with open("voterv5_abi.json", "r") as f:
    voter_abi = json.load(f)

voter = w3.eth.contract(
    address=Web3.to_checksum_address(VOTER_ADDRESS),
    abi=voter_abi
)

print("=" * 80)
print("DEBUGGING WEIGHTS QUERY")
print("=" * 80)

# Get epoch timestamp
epoch_timestamp = voter.functions._epochTimestamp().call()
print(f"\nCurrent epoch timestamp: {epoch_timestamp}")

# Get total weight
total_weight = voter.functions.totalWeight().call()
print(f"Total weight: {total_weight:,}")

# Scan through ALL pools to find ANY with votes
print("\n" + "=" * 80)
print("SCANNING ALL 291 POOLS FOR VOTES")
print("=" * 80)

total_pools = voter.functions.length().call()
print(f"\nTotal pools: {total_pools}")

pools_with_votes = []
pools_checked = 0

for i in range(total_pools):
    try:
        pool_addr = voter.functions.pools(i).call()
        gauge_addr = voter.functions.gauges(pool_addr).call()
        
        # Skip if no gauge
        if gauge_addr == "0x0000000000000000000000000000000000000000":
            continue
        
        pools_checked += 1
        votes = voter.functions.weights(pool_addr).call()
        
        if votes > 0:
            pools_with_votes.append({
                'index': i,
                'pool': pool_addr,
                'gauge': gauge_addr,
                'votes': votes
            })
        
        # Progress indicator
        if (i + 1) % 50 == 0:
            print(f"Checked {i + 1}/{total_pools} pools... (found {len(pools_with_votes)} with votes)")
            
    except Exception as e:
        print(f"Error on pool {i}: {e}")
        continue

print(f"\n" + "=" * 80)
print(f"RESULTS: Found {len(pools_with_votes)} pools with votes out of {pools_checked} pools with gauges")
print("=" * 80)

if pools_with_votes:
    print("\nTop 20 pools by votes:")
    print("-" * 80)
    sorted_pools = sorted(pools_with_votes, key=lambda x: x['votes'], reverse=True)
    for rank, p in enumerate(sorted_pools[:20], 1):
        pct = (p['votes'] / total_weight * 100) if total_weight > 0 else 0
        print(f"{rank:2d}. Pool #{p['index']:3d}: {p['votes']:>25,} votes ({pct:>6.2f}%)")
        print(f"    Pool:  {p['pool']}")
        print(f"    Gauge: {p['gauge']}")
else:
    print("\n⚠️  NO POOLS HAVE ANY VOTES IN CURRENT EPOCH")
    print("\nThis suggests either:")
    print("1. We just flipped to a new epoch and nobody has voted yet")
    print("2. There's an issue with how we're querying weights()")
    print("3. The votes are stored differently than expected")
    
print("\n" + "=" * 80)
