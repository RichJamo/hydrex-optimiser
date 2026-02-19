#!/usr/bin/env python3
"""
Fetch specific gauge from VoterV5 and verify data.
"""

import sys
sys.path.insert(0, '/Users/richardjamieson/Documents/GitHub/hydrex-optimiser')

from web3 import Web3
import json
from src.database import Database, Gauge
from datetime import datetime

# Your known gauge info for verification
KNOWN_POOL = "0x19FF35059452Faa793DdDF9894a1571c5D41003e"
KNOWN_GAUGE = "0x632f2D41Ba9e6E80035D578DDD48b019e4403F86"
KNOWN_INTERNAL = "0x1b9Cf48a27601d3cE57d95cF706e4D9FA7545cbf"
KNOWN_EXTERNAL = "0xBa61F238C215e997A4EC41561aFd80B2408F6BF7"

print("FETCHING GAUGE DATA FROM VOTERV5")
print("=" * 80)

# Connect to blockchain
w3 = Web3(Web3.HTTPProvider("https://base-mainnet.g.alchemy.com/v2/oFfvEpXYjGo8Nj4QQIkU3kXd6Z0JvfJZ"))
print(f"Connected: {w3.is_connected()}")

VOTER_V5_ADDRESS = '0xc69E3eF39E3fFBcE2A1c570f8d3ADF76909ef17b'

VOTER_V5_ABI = json.loads('''[
    {"inputs":[{"internalType":"address","name":"","type":"address"}],"name":"gauges","outputs":[{"internalType":"address","name":"","type":"address"}],"stateMutability":"view","type":"function"},
    {"inputs":[{"internalType":"address","name":"","type":"address"}],"name":"internal_bribes","outputs":[{"internalType":"address","name":"","type":"address"}],"stateMutability":"view","type":"function"},
    {"inputs":[{"internalType":"address","name":"","type":"address"}],"name":"external_bribes","outputs":[{"internalType":"address","name":"","type":"address"}],"stateMutability":"view","type":"function"},
    {"inputs":[{"internalType":"address","name":"","type":"address"}],"name":"isAlive","outputs":[{"internalType":"bool","name":"","type":"bool"}],"stateMutability":"view","type":"function"},
    {"inputs":[{"internalType":"address","name":"","type":"address"}],"name":"poolForGauge","outputs":[{"internalType":"address","name":"","type":"address"}],"stateMutability":"view","type":"function"}
]''')

voterv5 = w3.eth.contract(address=Web3.to_checksum_address(VOTER_V5_ADDRESS), abi=VOTER_V5_ABI)

print(f"\nQuerying VoterV5 for pool: {KNOWN_POOL}")
print("-" * 80)

# Query using pool address
pool_checksum = Web3.to_checksum_address(KNOWN_POOL)
gauge_from_pool = voterv5.functions.gauges(pool_checksum).call()

print(f"VoterV5.gauges(pool):")
print(f"  Returns: {gauge_from_pool}")
print(f"  Expected: {KNOWN_GAUGE}")
print(f"  Match: {gauge_from_pool.lower() == KNOWN_GAUGE.lower()}")

# Query bribe contracts using the gauge address we found
gauge_checksum = Web3.to_checksum_address(gauge_from_pool)
internal_bribe = voterv5.functions.internal_bribes(gauge_checksum).call()
external_bribe = voterv5.functions.external_bribes(gauge_checksum).call()
is_alive = voterv5.functions.isAlive(gauge_checksum).call()

print(f"\nVoterV5.internal_bribes(gauge):")
print(f"  Returns: {internal_bribe}")
print(f"  Expected: {KNOWN_INTERNAL}")
print(f"  Match: {internal_bribe.lower() == KNOWN_INTERNAL.lower()}")

print(f"\nVoterV5.external_bribes(gauge):")
print(f"  Returns: {external_bribe}")
print(f"  Expected: {KNOWN_EXTERNAL}")
print(f"  Match: {external_bribe.lower() == KNOWN_EXTERNAL.lower()}")

print(f"\nVoterV5.isAlive(gauge):")
print(f"  Returns: {is_alive}")

# Now save to database
print("\n" + "=" * 80)
print("SAVING TO DATABASE")
print("=" * 80)

db = Database('/Users/richardjamieson/Documents/GitHub/hydrex-optimiser/hydrex_data.db')
db.create_tables()

session = db.get_session()

# Check if gauge already exists
existing = session.query(Gauge).filter(
    Gauge.address == gauge_from_pool.lower()
).first()

if existing:
    print(f"Gauge already exists in database, updating...")
    existing.pool = KNOWN_POOL.lower()
    existing.internal_bribe = internal_bribe.lower()
    existing.external_bribe = external_bribe.lower()
    existing.is_alive = is_alive
else:
    print(f"Adding new gauge to database...")
    new_gauge = Gauge(
        address=gauge_from_pool.lower(),
        pool=KNOWN_POOL.lower(),
        internal_bribe=internal_bribe.lower(),
        external_bribe=external_bribe.lower(),
        is_alive=is_alive,
        created_at=int(datetime.now().timestamp())
    )
    session.add(new_gauge)

session.commit()
print("✓ Saved to database")

# Verify from database
print("\n" + "=" * 80)
print("VERIFYING FROM DATABASE")
print("=" * 80)

db_gauge = session.query(Gauge).filter(
    Gauge.address == KNOWN_GAUGE.lower()
).first()

if db_gauge:
    print(f"✓ Gauge found in database")
    print(f"  Pool:     {db_gauge.pool}")
    print(f"  Internal: {db_gauge.internal_bribe}")
    print(f"  External: {db_gauge.external_bribe}")
    print(f"  Is Alive: {db_gauge.is_alive}")
    
    print("\nVerification:")
    all_match = True
    if db_gauge.pool.lower() == KNOWN_POOL.lower():
        print("  ✓ Pool matches")
    else:
        print("  ❌ Pool mismatch")
        all_match = False
    
    if db_gauge.internal_bribe.lower() == KNOWN_INTERNAL.lower():
        print("  ✓ Internal bribe matches")
    else:
        print("  ❌ Internal bribe mismatch")
        all_match = False
    
    if db_gauge.external_bribe.lower() == KNOWN_EXTERNAL.lower():
        print("  ✓ External bribe matches")
    else:
        print("  ❌ External bribe mismatch")
        all_match = False
    
    if all_match:
        print("\n✓ ALL DATA VERIFIED - database is correct!")
    else:
        print("\n❌ Some data doesn't match")
else:
    print(f"❌ Gauge NOT found in database")

session.close()
