#!/usr/bin/env python3
"""Check for bribes in blocks where we have votes."""

from config import Config  
from src.subgraph_client import SubgraphClient
from web3 import Web3

client = SubgraphClient(Config.SUBGRAPH_URL)
w3 = Web3(Web3.HTTPProvider(Config.RPC_URL))

# We have votes from epoch 1757548800 to 1769040000
# Let's get block numbers around these times
print("Getting block numbers for epochs with votes...\n")

# Get first gauge with bribe contracts
gauges = client.fetch_gauges(first=5)
if not gauges:
    print("No gauges found!")
    exit()

gauge = gauges[0]
internal_bribe = Web3.to_checksum_address(gauge['internalBribe'])

print(f"Using internal bribe: {internal_bribe}\n")

# Check recent blocks (where votes happened)
test_ranges = [
    (40000000, 40001000, "Recent period 1"),
    (41000000, 41001000, "Recent period 2"),
    (38000000, 38001000, "Mid period"),
]

total_events = 0

for from_block, to_block, label in test_ranges:
    try:
        logs = w3.eth.get_logs({
            'address': internal_bribe,
            'fromBlock': from_block,
            'toBlock': to_block,
            'topics': ['0xe2403640ba68fed3a2f88b7557551d1993f84b99bb10ff833f0cf8db0c5e0486']
        })
        print(f"{label} (blocks {from_block}-{to_block}): {len(logs)} NotifyReward events")
        total_events += len(logs)
        
        if logs:
            print(f"  First event at block {logs[0]['blockNumber']}")
            
    except Exception as e:
        print(f"{label}: Error - {e}")

print(f"\nTotal NotifyReward events found: {total_events}")

if total_events == 0:
    print("\n⚠️  No bribe events found on-chain!")
    print("This means either:")
    print("  1. No one has added bribes to these gauges yet")
    print("  2. Bribes are on different bribe contracts")
    print("  3. The subgraph needs to track these bribe contracts as templates")
