#!/usr/bin/env python3
"""
Test vote weights on known popular pools to verify the weights() function works.
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
print("TESTING VOTE WEIGHTS ON POPULAR POOLS")
print("=" * 80)

# Get total weight across all pools
total_weight = voter.functions.totalWeight().call()
print(f"\n✓ Total votes across ALL pools: {total_weight:,}\n")

# Known popular pools from your last vote
popular_pools = [
    ("WETH/cbBTC", "0x3f9b8247c4de44f898bb0aec6f23c89bbe3f993f"),  # Your top pool
    ("USDC/cbBTC", "0x0ba636c985611e470e1dc098f3e2d79842415851"),  # Your 2nd pool
    ("kVCM/USDC", "0x6805ab7c8f125044737b791a5c4e6c48d0f36d24"),   # Your 3rd pool
    ("BNKR/WETH", "0xef96827ad5b7239c3d39a13fe1c0fb3b5b68e88f"),   # Your 4th pool
]

print("Checking votes on pools you voted for last time:")
print("-" * 80)

for name, pool_addr in popular_pools:
    try:
        pool_checksum = Web3.to_checksum_address(pool_addr)
        votes = voter.functions.weights(pool_checksum).call()
        gauge_addr = voter.functions.gauges(pool_checksum).call()
        
        if votes > 0:
            pct_of_total = (votes / total_weight * 100) if total_weight > 0 else 0
            print(f"✓ {name:20s} {votes:>15,} votes ({pct_of_total:>6.2f}% of total)")
            print(f"  Pool:  {pool_addr}")
            print(f"  Gauge: {gauge_addr}")
        else:
            print(f"  {name:20s} {votes:>15,} votes (0.00% of total)")
        print()
        
    except Exception as e:
        print(f"❌ Error checking {name}: {e}\n")

print("=" * 80)
print("CHECKING FIRST 10 POOLS IN CONTRACT")
print("=" * 80)

total_pools = voter.functions.length().call()
print(f"\nTotal pools in contract: {total_pools}")
print("\nFirst 10 pools with their votes:")
print("-" * 80)

found_with_votes = 0
for i in range(min(10, total_pools)):
    try:
        pool_addr = voter.functions.pools(i).call()
        gauge_addr = voter.functions.gauges(pool_addr).call()
        
        # Skip if no gauge
        if gauge_addr == "0x0000000000000000000000000000000000000000":
            continue
            
        votes = voter.functions.weights(pool_addr).call()
        
        if votes > 0:
            pct_of_total = (votes / total_weight * 100) if total_weight > 0 else 0
            print(f"Pool {i:3d}: {votes:>15,} votes ({pct_of_total:>6.2f}%)")
            print(f"         {pool_addr}")
            found_with_votes += 1
        
    except Exception as e:
        print(f"Error on pool {i}: {e}")
        continue

print(f"\n✓ Found {found_with_votes} pools with votes in first 10")
print("=" * 80)
