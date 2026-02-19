#!/usr/bin/env python3
"""
Test the 4 pools you voted for last time to see their current votes.
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
print("TESTING YOUR 4 POOLS FROM LAST VOTE")
print("=" * 80)

# These are the pool addresses from your last vote
your_pools = [
    ("WETH/cbBTC", "0x3f9b8247c4de44f898bb0aec6f23c89bbe3f993f"),
    ("USDC/cbBTC", "0x0ba636c985611e470e1dc098f3e2d79842415851"),
    ("kVCM/USDC", "0x6805ab7c8f125044737b791a5c4e6c48d0f36d24"),
    ("BNKR/WETH", "0xef96827ad5b7239c3d39a13fe1c0fb3b5b68e88f"),
]

total_weight = voter.functions.totalWeight().call()
print(f"\nTotal weight across all pools: {total_weight:,}\n")

for name, pool_addr in your_pools:
    print(f"{name}:")
    print(f"  Pool address: {pool_addr}")
    
    try:
        pool_checksum = Web3.to_checksum_address(pool_addr)
        
        # Get gauge
        gauge = voter.functions.gauges(pool_checksum).call()
        print(f"  Gauge: {gauge}")
        
        if gauge == "0x0000000000000000000000000000000000000000":
            print("  ❌ NO GAUGE REGISTERED FOR THIS POOL")
            print()
            continue
        
        # Get votes using checksummed pool address
        votes = voter.functions.weights(pool_checksum).call()
        print(f"  Votes: {votes:,}")
        
        if votes > 0:
            pct = (votes / total_weight * 100) if total_weight > 0 else 0
            print(f"  Percentage: {pct:.4f}%")
            print(f"  ✓ HAS VOTES")
        else:
            print(f"  ⚠️  ZERO VOTES")
        
        # Verify pool-gauge mapping
        pool_from_gauge = voter.functions.poolForGauge(gauge).call()
        if pool_from_gauge.lower() == pool_addr.lower():
            print(f"  ✓ Pool-gauge mapping correct")
        else:
            print(f"  ❌ Pool-gauge mismatch!")
            print(f"     Pool from gauge: {pool_from_gauge}")
        
    except Exception as e:
        print(f"  ❌ Error: {e}")
    
    print()

print("=" * 80)
