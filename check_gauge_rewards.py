#!/usr/bin/env python3
"""
Check for notifyRewardAmount events on Gauge contracts.
Based on the distribution flow, oHYDX is distributed via Gauge.notifyRewardAmount(), not Bribe.NotifyReward().
"""

from web3 import Web3
from config import Config
import json
from src.database import Database, Gauge

# Initialize database
db = Database("data.db")

# notifyRewardAmount event signature
# event notifyRewardAmount(address indexed token, uint256 amount)
NOTIFY_REWARD_AMOUNT_TOPIC = Web3.keccak(text="notifyRewardAmount(address,uint256)").hex()

print(f"notifyRewardAmount event signature: {NOTIFY_REWARD_AMOUNT_TOPIC}")

w3 = Web3(Web3.HTTPProvider(Config.RPC_URL))

# Get gauge addresses from database
session = db.get_session()
gauges = session.query(Gauge).limit(10).all()
session.close()

print(f"\nChecking {len(gauges)} gauges for notifyRewardAmount events...\n")

# Check recent blocks for events
start_block = 41_000_000
end_block = 41_001_000

for gauge in gauges:
    gauge_address = Web3.to_checksum_address(gauge.address)
    print(f"Gauge: {gauge_address}")
    print(f"  Pool: {gauge.pool}")
    
    try:
        # Get all logs from this gauge in the block range
        logs = w3.eth.get_logs({
            'address': gauge_address,
            'fromBlock': start_block,
            'toBlock': end_block,
        })
        
        print(f"  Total events: {len(logs)}")
        
        # Filter for notifyRewardAmount events
        notify_events = [log for log in logs if log['topics'][0].hex() == NOTIFY_REWARD_AMOUNT_TOPIC]
        
        if notify_events:
            print(f"  ✅ Found {len(notify_events)} notifyRewardAmount events!")
            for event in notify_events[:3]:  # Show first 3
                token = Web3.to_checksum_address('0x' + event['topics'][1].hex()[-40:])
                amount = int(event['data'].hex(), 16)
                print(f"    Block {event['blockNumber']}: token={token}, amount={amount}")
        else:
            print(f"  No notifyRewardAmount events found")
        
    except Exception as e:
        print(f"  Error: {e}")
    
    print()

# Also check if we can find the oHYDX token address
print("\n" + "="*80)
print("Checking for oHYDX token address in recent gauge reward events...")
print("="*80 + "\n")

# Get the most voted gauge
session = db.get_session()
from src.database import Vote
from sqlalchemy import func

top_gauge_result = session.query(
    Vote.gauge,
    func.sum(Vote.total_votes).label('total')
).group_by(Vote.gauge).order_by(func.sum(Vote.total_votes).desc()).first()

if top_gauge_result:
    top_gauge_address = Web3.to_checksum_address(top_gauge_result[0])
    print(f"Most voted gauge: {top_gauge_address}")
    print(f"Total votes: {top_gauge_result[1]:.2e}\n")
    
    # Check wider block range for this gauge
    print(f"Scanning blocks {start_block}-{end_block} for notifyRewardAmount events...")
    
    try:
        logs = w3.eth.get_logs({
            'address': top_gauge_address,
            'topics': [NOTIFY_REWARD_AMOUNT_TOPIC],
            'fromBlock': start_block,
            'toBlock': end_block,
        })
        
        print(f"Found {len(logs)} notifyRewardAmount events\n")
        
        if logs:
            print("Sample events:")
            for log in logs[:5]:
                token = Web3.to_checksum_address('0x' + log['topics'][1].hex()[-40:])
                amount = int(log['data'].hex(), 16)
                print(f"  Block {log['blockNumber']}: token={token}, amount={amount:.2e}")
        else:
            print("No events found in this range. Trying wider ranges...")
            
            # Try multiple ranges
            ranges = [
                (35_273_810, 36_000_000),  # Right after VoterV5 deployment
                (38_000_000, 39_000_000),  # Mid range
                (40_000_000, 41_000_000),  # Recent
            ]
            
            for start, end in ranges:
                print(f"\nTrying blocks {start}-{end}...")
                logs = w3.eth.get_logs({
                    'address': top_gauge_address,
                    'topics': [NOTIFY_REWARD_AMOUNT_TOPIC],
                    'fromBlock': start,
                    'toBlock': end,
                })
                print(f"  Found {len(logs)} events")
                if logs:
                    for log in logs[:3]:
                        token = Web3.to_checksum_address('0x' + log['topics'][1].hex()[-40:])
                        amount = int(log['data'].hex(), 16)
                        print(f"    Block {log['blockNumber']}: token={token}, amount={amount:.2e}")
                    break
                
    except Exception as e:
        print(f"Error: {e}")

session.close()
