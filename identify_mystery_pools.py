#!/usr/bin/env python3
"""
Identify what pools the mystery gauges correspond to.
Query VoterV5 to get pool info and compare to user's voted pools.
"""

import sqlite3
from web3 import Web3
import json

# Connect to Base via Alchemy
w3 = Web3(Web3.HTTPProvider('https://base-mainnet.g.alchemy.com/v2/oFfvEpXYjGo8Nj4QQIkU3kXd6Z0JvfJZ'))

# VoterV5 contract
VOTER_V5_ADDRESS = '0xc69E3eF39E3fFBcE2A1c570f8d3ADF76909ef17b'

# VoterV5 ABI - minimal for what we need
VOTER_V5_ABI = json.loads('''[
    {"inputs":[{"internalType":"address","name":"","type":"address"}],"name":"gauges","outputs":[{"internalType":"address","name":"","type":"address"}],"stateMutability":"view","type":"function"},
    {"inputs":[{"internalType":"address","name":"","type":"address"}],"name":"poolForGauge","outputs":[{"internalType":"address","name":"","type":"address"}],"stateMutability":"view","type":"function"},
    {"inputs":[{"internalType":"address","name":"","type":"address"}],"name":"isAlive","outputs":[{"internalType":"bool","name":"","type":"bool"}],"stateMutability":"view","type":"function"}
]''')

voterv5 = w3.eth.contract(address=Web3.to_checksum_address(VOTER_V5_ADDRESS), abi=VOTER_V5_ABI)

# Mystery gauges from previous script
mystery_gauges = [
    "0xee5f8bf7cdb1ad421993a368b15d06ad58122dab",  # $246.75 internal
    "0xe63cd99406e98d909ab6d702b11dd4cd31a425a2",  # $236.11 internal
    "0xdc470dc0b3247058ea4605dba6e48a9b2a083971",  # $245.80 (both)
    "0x1df220b45408a11729302ec84a1443d98beccc57",  # $227.67 internal
]

# User's voted pools
voted_pools = [
    "0x19FF35059452Faa793DdDF9894a1571c5D41003e",
    "0xe62a34Ae5e0B9FdE3501Aeb72DC9585Bb3B72A7e",
    "0xF19787f048b3401546aa7A979afa79D555C114Dd",
    "0xE539b14a87D3Db4a2945ac99b29A69DE61531592",
]

print("\nIDENTIFYING MYSTERY POOLS")
print("=" * 80)

# Connect to database to get pool names if available
conn = sqlite3.connect('/Users/richardjamieson/Documents/GitHub/hydrex-optimiser/hydrex_data.db')
cursor = conn.cursor()

for gauge_addr in mystery_gauges:
    print(f"\nGauge: {gauge_addr}")
    
    # Query VoterV5 for the pool this gauge corresponds to
    gauge_checksum = Web3.to_checksum_address(gauge_addr)
    try:
        pool_addr = voterv5.functions.poolForGauge(gauge_checksum).call()
        print(f"  Pool: {pool_addr}")
        
        # Check if this is one of the user's voted pools
        if pool_addr.lower() in [p.lower() for p in voted_pools]:
            print(f"  ✓ THIS IS ONE OF YOUR VOTED POOLS!")
        else:
            print(f"  ❌ This is NOT one of your voted pools")
        
        # Try to get pool info from database
        cursor.execute("""
            SELECT symbol0, symbol1, stable
            FROM pools
            WHERE LOWER(address) = LOWER(?)
        """, (pool_addr,))
        pool_info = cursor.fetchone()
        
        if pool_info:
            symbol0, symbol1, stable = pool_info
            pool_type = "Stable" if stable else "Volatile"
            print(f"  Type: {pool_type} {symbol0}/{symbol1}")
        
        # Check if gauge is alive
        is_alive = voterv5.functions.isAlive(gauge_checksum).call()
        print(f"  Status: {'✓ Alive' if is_alive else '❌ Dead'}")
        
    except Exception as e:
        print(f"  Error querying pool: {e}")

conn.close()

print("\n" + "=" * 80)
print("USER'S VOTED POOLS")
print("=" * 80)
for pool in voted_pools:
    print(f"  {pool}")
