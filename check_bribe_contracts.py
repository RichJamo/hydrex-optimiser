#!/usr/bin/env python3
"""Check if gauges have bribe contracts and if those contracts have events."""

from config import Config
from src.subgraph_client import SubgraphClient
from web3 import Web3

client = SubgraphClient(Config.SUBGRAPH_URL)
w3 = Web3(Web3.HTTPProvider(Config.RPC_URL))

print("Checking gauge bribe contracts...\n")

# Get a few gauges
gauges = client.fetch_gauges(first=5)
print(f"Found {len(gauges)} gauges\n")

if gauges:
    gauge = gauges[0]
    print(f"First gauge: {gauge['address']}")
    print(f"  Internal bribe: {gauge['internalBribe']}")
    print(f"  External bribe: {gauge['externalBribe']}")
    
    # Check if these bribe contracts have any code
    internal_bribe = Web3.to_checksum_address(gauge['internalBribe'])
    external_bribe = Web3.to_checksum_address(gauge['externalBribe'])
    
    print(f"\nChecking if bribe contracts are deployed:")
    try:
        internal_code = w3.eth.get_code(internal_bribe)
        print(f"  Internal bribe code length: {len(internal_code)} bytes")
        
        external_code = w3.eth.get_code(external_bribe)
        print(f"  External bribe code length: {len(external_code)} bytes")
        
        if len(internal_code) > 2 or len(external_code) > 2:
            print("\n✅ Bribe contracts are deployed!")
            
            # Check NotifyReward event signature
            print("\nNotifyReward event signature: 0xe2403640ba68fed3a2f88b7557551d1993f84b99bb10ff833f0cf8db0c5e0486")
            print("(NotifyReward(indexed address,indexed address,uint256))")
            
            # Try to get logs from one bribe contract
            from_block = 35273810
            to_block = 35274810  # Just 1000 blocks
            
            print(f"\nChecking logs from internal bribe {internal_bribe[:10]}...")
            print(f"  Block range: {from_block} to {to_block}")
            
            try:
                logs = w3.eth.get_logs({
                    'address': internal_bribe,
                    'fromBlock': from_block,
                    'toBlock': to_block,
                    'topics': ['0xe2403640ba68fed3a2f88b7557551d1993f84b99bb10ff833f0cf8db0c5e0486']
                })
                print(f"  Found {len(logs)} NotifyReward events in sample range")
                
                if len(logs) > 0:
                    print(f"\n✅ NotifyReward events exist on-chain!")
                    print(f"  First event block: {logs[0]['blockNumber']}")
                else:
                    print(f"\n⚠️ No NotifyReward events in sample range")
                    print(f"  This might mean no bribes were added yet, or they're in later blocks")
                    
            except Exception as e:
                print(f"  Error fetching logs: {e}")
                
    except Exception as e:
        print(f"  Error checking contracts: {e}")
else:
    print("No gauges found!")
