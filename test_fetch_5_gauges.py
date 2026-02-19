#!/usr/bin/env python3
"""
Fetch 5 gauges from VoterV5 contract to test database write
"""

from web3 import Web3
import sqlite3
import sys

print("Initializing...")
w3 = Web3(Web3.HTTPProvider("https://base-mainnet.g.alchemy.com/v2/oFfvEpXYjGo8Nj4QQIkU3kXd6Z0JvfJZ"))
print(f"Connected to Base: {w3.is_connected()}\n")

# Connect to database
db = sqlite3.connect("data.db")
cursor = db.cursor()

# VoterV5 address
voterv5_addr = w3.to_checksum_address("0xc69E3eF39E3fFBcE2A1c570f8d3ADF76909ef17b")

# Minimal VoterV5 ABI
voterv5_abi = [
    {
        "inputs": [],
        "name": "length",
        "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function"
    },
    {
        "inputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
        "name": "pools",
        "outputs": [{"internalType": "address", "name": "", "type": "address"}],
        "stateMutability": "view",
        "type": "function"
    },
    {
        "inputs": [{"internalType": "address", "name": "", "type": "address"}],
        "name": "gauges",
        "outputs": [{"internalType": "address", "name": "", "type": "address"}],
        "stateMutability": "view",
        "type": "function"
    },
    {
        "inputs": [{"internalType": "address", "name": "", "type": "address"}],
        "name": "internal_bribes",
        "outputs": [{"internalType": "address", "name": "", "type": "address"}],
        "stateMutability": "view",
        "type": "function"
    },
    {
        "inputs": [{"internalType": "address", "name": "", "type": "address"}],
        "name": "external_bribes",
        "outputs": [{"internalType": "address", "name": "", "type": "address"}],
        "stateMutability": "view",
        "type": "function"
    },
    {
        "inputs": [{"internalType": "address", "name": "", "type": "address"}],
        "name": "isAlive",
        "outputs": [{"internalType": "bool", "name": "", "type": "bool"}],
        "stateMutability": "view",
        "type": "function"
    }
]

voterv5 = w3.eth.contract(address=voterv5_addr, abi=voterv5_abi)

print(f"VoterV5: {voterv5_addr}")
print("=" * 80)

# Get total number of pools
try:
    total_pools = voterv5.functions.length().call()
    print(f"✓ Total pools in VoterV5: {total_pools}\n")
except Exception as e:
    print(f"❌ Error: {e}")
    sys.exit(1)

# Fetch first 5 gauges
print("Fetching first 5 gauges...")
print("=" * 80)

gauges_found = []

for i in range(min(20, total_pools)):  # Check up to 20 pools to find 5 gauges
    if len(gauges_found) >= 5:
        break
    
    try:
        pool_addr = voterv5.functions.pools(i).call()
        gauge_addr = voterv5.functions.gauges(pool_addr).call()
        
        # Skip if no gauge
        if gauge_addr == "0x0000000000000000000000000000000000000000":
            continue
        
        internal_bribe = voterv5.functions.internal_bribes(gauge_addr).call()
        external_bribe = voterv5.functions.external_bribes(gauge_addr).call()
        is_alive = voterv5.functions.isAlive(gauge_addr).call()
        
        gauges_found.append({
            'pool': pool_addr,
            'gauge': gauge_addr,
            'internal_bribe': internal_bribe,
            'external_bribe': external_bribe,
            'is_alive': is_alive
        })
        
        print(f"{len(gauges_found)}. Gauge: {gauge_addr[:10]}... Pool: {pool_addr[:10]}... Alive: {is_alive}")
        
    except Exception as e:
        print(f"Error at index {i}: {e}")
        continue

print(f"\n✓ Found {len(gauges_found)} gauges")

# Write to database
print("\nWriting to database...")
print("=" * 80)

inserted = 0
skipped = 0

for gauge_data in gauges_found:
    # Check if exists
    cursor.execute(
        "SELECT address FROM gauges WHERE LOWER(address) = LOWER(?)",
        (gauge_data['gauge'],)
    )
    
    if cursor.fetchone():
        skipped += 1
        print(f"  Skip {gauge_data['gauge'][:10]}... (already exists)")
    else:
        cursor.execute("""
            INSERT INTO gauges 
            (address, pool, internal_bribe, external_bribe, is_alive, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (
            gauge_data['gauge'],
            gauge_data['pool'],
            gauge_data['internal_bribe'],
            gauge_data['external_bribe'],
            gauge_data['is_alive'],
            0  # created_at placeholder
        ))
        inserted += 1
        print(f"  ✓ Insert {gauge_data['gauge'][:10]}...")

db.commit()
db.close()

print(f"\n✓ Inserted: {inserted}")
print(f"✓ Skipped: {skipped}")
print("✓ Done!")
