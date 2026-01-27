#!/usr/bin/env python3
"""Check for reward distribution on a specific bribe contract."""

from config import Config  
from src.subgraph_client import SubgraphClient
from web3 import Web3

w3 = Web3(Web3.HTTPProvider(Config.RPC_URL))
client = SubgraphClient(Config.SUBGRAPH_URL)

# Get a gauge that has votes
print("Finding a heavily voted gauge...\n")

# Query for GaugeVotes to find active gauges
import sqlite3
conn = sqlite3.connect('data.db')
cursor = conn.cursor()

# Get gauge with most votes
cursor.execute("""
    SELECT gauge, SUM(total_votes) as total
    FROM votes
    GROUP BY gauge
    ORDER BY total DESC
    LIMIT 1
""")

result = cursor.fetchone()
if result:
    top_gauge = result[0]
    total_votes = result[1]
    print(f"Top voted gauge: {top_gauge}")
    print(f"Total votes: {total_votes:.2e}\n")
    
    # Get gauge info from subgraph
    gauges = client.fetch_gauges(first=300)
    gauge_info = None
    for g in gauges:
        if g['address'].lower() == top_gauge.lower():
            gauge_info = g
            break
    
    if gauge_info:
        internal_bribe = Web3.to_checksum_address(gauge_info['internalBribe'])
        external_bribe = Web3.to_checksum_address(gauge_info['externalBribe'])
        
        print(f"Internal bribe: {internal_bribe}")
        print(f"External bribe: {external_bribe}\n")
        
        # Check for ALL events on these bribe contracts (not just NotifyReward)
        print("Checking ALL events on internal bribe contract...")
        print("(Blocks 40000000-40001000)\n")
        
        try:
            all_logs = w3.eth.get_logs({
                'address': internal_bribe,
                'fromBlock': 40000000,
                'toBlock': 40001000,
            })
            print(f"Total events: {len(all_logs)}")
            
            if all_logs:
                # Show event signatures
                signatures = {}
                for log in all_logs:
                    if log['topics']:
                        sig = log['topics'][0].hex()
                        signatures[sig] = signatures.get(sig, 0) + 1
                
                print(f"\nEvent signatures found:")
                for sig, count in signatures.items():
                    print(f"  {sig}: {count} events")
                    
                # Known signatures
                known = {
                    '0xe2403640ba68fed3a2f88b7557551d1993f84b99bb10ff833f0cf8db0c5e0486': 'NotifyReward(address,address,uint256)',
                    '0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef': 'Transfer(address,address,uint256)',
                }
                
                print(f"\nKnown events:")
                for sig, name in known.items():
                    if sig in signatures:
                        print(f"  ✅ {name}: {signatures[sig]} events")
                    else:
                        print(f"  ❌ {name}: not found")
            else:
                print("  No events found in this range")
                
        except Exception as e:
            print(f"Error: {e}")
            
    else:
        print("Gauge not found in subgraph")
else:
    print("No votes found in database")

conn.close()
