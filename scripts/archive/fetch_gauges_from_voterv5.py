#!/usr/bin/env python3
"""
Fetch all active gauges directly from VoterV5 contract via RPC
"""

from web3 import Web3
import json
import sqlite3

print("Initializing...")
w3 = Web3(Web3.HTTPProvider("https://base-mainnet.g.alchemy.com/v2/oFfvEpXYjGo8Nj4QQIkU3kXd6Z0JvfJZ"))
print(f"Connected to Base: {w3.is_connected()}\n")

# Connect to database
db = sqlite3.connect("data.db")
cursor = db.cursor()

# Load VoterV5 ABI (minimal - just what we need)
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

voterv5_addr = w3.to_checksum_address(Config.VOTER_ADDRESS)
voterv5 = w3.eth.contract(address=voterv5_addr, abi=voterv5_abi)

print(f"VoterV5: {voterv5_addr}")
print("=" * 80)

# Get total number of pools
try:
    total_pools = voterv5.functions.length().call()
    print(f"✓ Total pools in VoterV5: {total_pools}\n")
except Exception as e:
    print(f"❌ Error getting pool count: {e}")
    sys.exit(1)

# Fetch all pools and their gauges
print("Fetching pools and gauges...")
print("=" * 80)

all_gauges = []

for i in range(total_pools):
    if i % 50 == 0:
        print(f"Progress: {i}/{total_pools}...", flush=True)
    
    try:
        pool_addr = voterv5.functions.pools(i).call()
        gauge_addr = voterv5.functions.gauges(pool_addr).call()
        
        # Skip if no gauge exists for this pool
        if gauge_addr == "0x0000000000000000000000000000000000000000":
            continue
        
        # Get bribe contracts
        internal_bribe = voterv5.functions.internal_bribes(gauge_addr).call()
        external_bribe = voterv5.functions.external_bribes(gauge_addr).call()
        is_alive = voterv5.functions.isAlive(gauge_addr).call()
        
        all_gauges.append({
            'pool': pool_addr,
            'gauge': gauge_addr,
            'internal_bribe': internal_bribe,
            'external_bribe': external_bribe,
            'is_alive': is_alive
        })
        
    except Exception as e:
        print(f"Error fetching pool {i}: {e}")
        continue

print(f"\n✓ Fetched {len(all_gauges)} gauges from {total_pools} pools")

# Update database
print("\nUpdating database...")
print("=" * 80)

new_gauges = 0
existing_gauges = 0
updated_gauges = 0

for gauge_data in all_gauges:
    # Check if gauge already exists
    cursor.execute(
        "SELECT address, is_alive FROM gauges WHERE LOWER(address) = LOWER(?)",
        (gauge_data['gauge'],)
    )
    result = cursor.fetchone()
    
    if result:
        existing_gauges += 1
        # Update is_alive status if changed
        if result[1] != gauge_data['is_alive']:
            cursor.execute(
                "UPDATE gauges SET is_alive = ? WHERE LOWER(address) = LOWER(?)",
                (gauge_data['is_alive'], gauge_data['gauge'])
            )
            updated_gauges += 1
    else:
        # Insert new gauge
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
            0  # We don't have creation timestamp from RPC
        ))
        new_gauges += 1

db.commit()

print(f"✓ New gauges added: {new_gauges}")
print(f"✓ Existing gauges: {existing_gauges}")
print(f"✓ Updated gauges: {updated_gauges}")
print(f"✓ Total gauges in database: {new_gauges + existing_gauges}")

# Verify the 3 missing user gauges
print("\n" + "=" * 80)
print("Checking user's previously missing gauges...")
print("=" * 80)

missing_gauges = [
    "0x0a2918e8c5ef5ec8bc37de77a03f0b1ad66ae23e",
    "0x1df220b4c8b7e3d8a48f3eb77e0c8a7c9e9b3f0c",
    "0x6321d73080fbac4c99c5e9a8b8e8f7e6d5c4b3a2",
]

for gauge_addr in missing_gauges:
    cursor.execute(
        "SELECT address, internal_bribe, external_bribe, is_alive FROM gauges WHERE LOWER(address) = LOWER(?)",
        (gauge_addr,)
    )
    result = cursor.fetchone()
    
    if result:
        print(f"✓ {gauge_addr}")
        print(f"  Internal: {result[1]}")
        print(f"  External: {result[2]}")
        print(f"  Alive: {result[3]}")
    else:
        print(f"❌ {gauge_addr} - STILL NOT FOUND")

db.close()
print("\n✓ Done!")
