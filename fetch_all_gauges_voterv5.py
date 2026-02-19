#!/usr/bin/env python3
"""
Fetch ALL gauges from VoterV5 contract and update database
"""

from web3 import Web3
import sqlite3
import sys

print("Initializing...")
w3 = Web3(Web3.HTTPProvider("https://base-mainnet.g.alchemy.com/v2/oFfvEpXYjGo8Nj4QQIkU3kXd6Z0JvfJZ"))
print(f"Connected: {w3.is_connected()}\n")

db = sqlite3.connect("data.db")
cursor = db.cursor()

voterv5_addr = w3.to_checksum_address("0xc69E3eF39E3fFBcE2A1c570f8d3ADF76909ef17b")

voterv5_abi = [
    {"inputs": [], "name": "length", "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}], "stateMutability": "view", "type": "function"},
    {"inputs": [{"internalType": "uint256", "name": "", "type": "uint256"}], "name": "pools", "outputs": [{"internalType": "address", "name": "", "type": "address"}], "stateMutability": "view", "type": "function"},
    {"inputs": [{"internalType": "address", "name": "", "type": "address"}], "name": "gauges", "outputs": [{"internalType": "address", "name": "", "type": "address"}], "stateMutability": "view", "type": "function"},
    {"inputs": [{"internalType": "address", "name": "", "type": "address"}], "name": "internal_bribes", "outputs": [{"internalType": "address", "name": "", "type": "address"}], "stateMutability": "view", "type": "function"},
    {"inputs": [{"internalType": "address", "name": "", "type": "address"}], "name": "external_bribes", "outputs": [{"internalType": "address", "name": "", "type": "address"}], "stateMutability": "view", "type": "function"},
    {"inputs": [{"internalType": "address", "name": "", "type": "address"}], "name": "isAlive", "outputs": [{"internalType": "bool", "name": "", "type": "bool"}], "stateMutability": "view", "type": "function"}
]

voterv5 = w3.eth.contract(address=voterv5_addr, abi=voterv5_abi)

print(f"VoterV5: {voterv5_addr}")
print("=" * 80)

total_pools = voterv5.functions.length().call()
print(f"✓ Total pools: {total_pools}\n")

print("Fetching all gauges...")
print("=" * 80)

all_gauges = []
pools_checked = 0

for i in range(total_pools):
    if i % 50 == 0:
        print(f"Progress: {i}/{total_pools} (found {len(all_gauges)} gauges)...", flush=True)
    
    try:
        pool_addr = voterv5.functions.pools(i).call()
        gauge_addr = voterv5.functions.gauges(pool_addr).call()
        
        if gauge_addr == "0x0000000000000000000000000000000000000000":
            continue
        
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
        pools_checked += 1
        
    except Exception as e:
        print(f"  Error at pool {i}: {e}")
        continue

print(f"\n✓ Found {len(all_gauges)} gauges from {total_pools} pools")

print("\nUpdating database...")
print("=" * 80)

inserted = 0
updated = 0
skipped = 0

for gauge_data in all_gauges:
    cursor.execute(
        "SELECT address, is_alive FROM gauges WHERE LOWER(address) = LOWER(?)",
        (gauge_data['gauge'],)
    )
    result = cursor.fetchone()
    
    if result:
        # Update is_alive if changed
        if result[1] != gauge_data['is_alive']:
            cursor.execute(
                "UPDATE gauges SET is_alive = ? WHERE LOWER(address) = LOWER(?)",
                (gauge_data['is_alive'], gauge_data['gauge'])
            )
            updated += 1
        else:
            skipped += 1
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
            0
        ))
        inserted += 1

db.commit()

print(f"✓ Inserted: {inserted}")
print(f"✓ Updated: {updated}")
print(f"✓ Skipped: {skipped}")

# Check user's missing gauges
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
        "SELECT address, internal_bribe, external_bribe FROM gauges WHERE LOWER(address) = LOWER(?)",
        (gauge_addr,)
    )
    result = cursor.fetchone()
    
    if result:
        print(f"✓ {gauge_addr}")
        print(f"  Internal: {result[1]}")
        print(f"  External: {result[2]}")
    else:
        print(f"❌ {gauge_addr} - NOT FOUND IN VOTERV5")

db.close()
print("\n✓ Done!")
