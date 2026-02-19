#!/usr/bin/env python3
"""
Check what our database has for the specific gauge the user mentioned.
"""

import sqlite3
from web3 import Web3
import json

# User's known gauge info
KNOWN_GAUGE = "0x632f2D41Ba9e6E80035D578DDD48b019e4403F86"
KNOWN_POOL = "0x19FF35059452Faa793DdDF9894a1571c5D41003e"
KNOWN_INTERNAL = "0x1b9Cf48a27601d3cE57d95cF706e4D9FA7545cbf"
KNOWN_EXTERNAL = "0xBa61F238C215e997A4EC41561aFd80B2408F6BF7"

print("\nCHECKING USER'S KNOWN GAUGE")
print("=" * 80)
print(f"Gauge:    {KNOWN_GAUGE}")
print(f"Pool:     {KNOWN_POOL}")
print(f"Internal: {KNOWN_INTERNAL}")
print(f"External: {KNOWN_EXTERNAL}")
print()

# Check database
conn = sqlite3.connect('/Users/richardjamieson/Documents/GitHub/hydrex-optimiser/hydrex_data.db')
cursor = conn.cursor()

print("DATABASE RECORD:")
print("-" * 80)
cursor.execute("""
    SELECT address, pool, internal_bribe, external_bribe, is_alive
    FROM gauges
    WHERE LOWER(address) = LOWER(?)
""", (KNOWN_GAUGE,))

result = cursor.fetchone()
if result:
    db_gauge, db_pool, db_internal, db_external, db_is_alive = result
    print(f"Gauge:    {db_gauge}")
    print(f"Pool:     {db_pool}")
    print(f"Internal: {db_internal}")
    print(f"External: {db_external}")
    print(f"Is Alive: {db_is_alive}")
    
    print("\nCOMPARISON:")
    print("-" * 80)
    
    if db_pool.lower() == KNOWN_POOL.lower():
        print("✓ Pool address MATCHES")
    else:
        print(f"❌ Pool address MISMATCH!")
        print(f"   Expected: {KNOWN_POOL}")
        print(f"   Database: {db_pool}")
    
    if db_internal.lower() == KNOWN_INTERNAL.lower():
        print("✓ Internal bribe MATCHES")
    else:
        print(f"❌ Internal bribe MISMATCH!")
        print(f"   Expected: {KNOWN_INTERNAL}")
        print(f"   Database: {db_internal}")
    
    if db_external.lower() == KNOWN_EXTERNAL.lower():
        print("✓ External bribe MATCHES")
    else:
        print(f"❌ External bribe MISMATCH!")
        print(f"   Expected: {KNOWN_EXTERNAL}")
        print(f"   Database: {db_external}")
else:
    print("❌ NOT FOUND in database!")

print()

# Now let's verify against VoterV5 contract
print("VERIFYING AGAINST VOTERV5 CONTRACT:")
print("-" * 80)

w3 = Web3(Web3.HTTPProvider('https://base-mainnet.g.alchemy.com/v2/oFfvEpXYjGo8Nj4QQIkU3kXd6Z0JvfJZ'))

VOTER_V5_ADDRESS = '0xc69E3eF39E3fFBcE2A1c570f8d3ADF76909ef17b'
VOTER_V5_ABI = json.loads('''[
    {"inputs":[{"internalType":"address","name":"","type":"address"}],"name":"gauges","outputs":[{"internalType":"address","name":"","type":"address"}],"stateMutability":"view","type":"function"},
    {"inputs":[{"internalType":"address","name":"","type":"address"}],"name":"internal_bribes","outputs":[{"internalType":"address","name":"","type":"address"}],"stateMutability":"view","type":"function"},
    {"inputs":[{"internalType":"address","name":"","type":"address"}],"name":"external_bribes","outputs":[{"internalType":"address","name":"","type":"address"}],"stateMutability":"view","type":"function"},
    {"inputs":[{"internalType":"address","name":"","type":"address"}],"name":"poolForGauge","outputs":[{"internalType":"address","name":"","type":"address"}],"stateMutability":"view","type":"function"}
]''')

voterv5 = w3.eth.contract(address=Web3.to_checksum_address(VOTER_V5_ADDRESS), abi=VOTER_V5_ABI)

# Query using pool address
pool_checksum = Web3.to_checksum_address(KNOWN_POOL)
gauge_from_pool = voterv5.functions.gauges(pool_checksum).call()
print(f"VoterV5.gauges({KNOWN_POOL[:10]}...)")
print(f"  Returns: {gauge_from_pool}")
if gauge_from_pool.lower() == KNOWN_GAUGE.lower():
    print("  ✓ MATCHES known gauge")
else:
    print("  ❌ Does NOT match known gauge")

# Query using gauge address
gauge_checksum = Web3.to_checksum_address(KNOWN_GAUGE)
internal_from_gauge = voterv5.functions.internal_bribes(gauge_checksum).call()
external_from_gauge = voterv5.functions.external_bribes(gauge_checksum).call()
pool_from_gauge = voterv5.functions.poolForGauge(gauge_checksum).call()

print(f"\nVoterV5.internal_bribes({KNOWN_GAUGE[:10]}...)")
print(f"  Returns: {internal_from_gauge}")
if internal_from_gauge.lower() == KNOWN_INTERNAL.lower():
    print("  ✓ MATCHES known internal")
else:
    print("  ❌ Does NOT match known internal")

print(f"\nVoterV5.external_bribes({KNOWN_GAUGE[:10]}...)")
print(f"  Returns: {external_from_gauge}")
if external_from_gauge.lower() == KNOWN_EXTERNAL.lower():
    print("  ✓ MATCHES known external")
else:
    print("  ❌ Does NOT match known external")

print(f"\nVoterV5.poolForGauge({KNOWN_GAUGE[:10]}...)")
print(f"  Returns: {pool_from_gauge}")
if pool_from_gauge.lower() == KNOWN_POOL.lower():
    print("  ✓ MATCHES known pool")
else:
    print("  ❌ Does NOT match known pool")

conn.close()
