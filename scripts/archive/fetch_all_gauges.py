#!/usr/bin/env python3
"""
Fetch ALL active gauges from subgraph and update database
"""

import sys
sys.path.insert(0, '/Users/richardjamieson/Documents/GitHub/hydrex-optimiser')

from src.subgraph_client import SubgraphClient
from src.database import Database
from config import Config

print("Initializing...")
# Use the new analytics subgraph
client = SubgraphClient(subgraph_url="https://analytics-subgraph.hydrex.fi/")
db = Database(Config.DATABASE_PATH)

print(f"Subgraph URL: https://analytics-subgraph.hydrex.fi/\n")

# Test connection
print("Testing subgraph connection...")
try:
    test_result = client.fetch_gauges(first=1)
    print(f"✓ Connection successful, sample gauge: {test_result[0]['address'] if test_result else 'none'}\n")
except Exception as e:
    print(f"❌ Connection failed: {e}")
    sys.exit(1)

# Fetch all gauges with pagination
print("Fetching all gauges from subgraph...")
print("=" * 80)

all_gauges = []
skip = 0
page = 1
batch_size = 1000

while True:
    print(f"Page {page}: fetching {batch_size} gauges (skip={skip})...", end=' ', flush=True)
    
    try:
        gauges = client.fetch_gauges(first=batch_size, skip=skip)
        
        if not gauges:
            print("done (empty)")
            break
        
        print(f"got {len(gauges)}")
        all_gauges.extend(gauges)
        
        if len(gauges) < batch_size:
            print(f"Last page (got {len(gauges)} < {batch_size})")
            break
        
        skip += batch_size
        page += 1
        
    except Exception as e:
        print(f"ERROR: {e}")
        break

print(f"\n✓ Fetched {len(all_gauges)} total gauges")

# Update database
print("\nUpdating database...")
print("=" * 80)

new_gauges = 0
existing_gauges = 0

for gauge in all_gauges:
    # Check if gauge already exists
    cursor = db.session.execute(
        "SELECT address FROM gauges WHERE LOWER(address) = LOWER(?)",
        (gauge['address'],)
    )
    exists = cursor.fetchone()
    
    if exists:
        existing_gauges += 1
    else:
        # Insert new gauge
        db.session.execute("""
            INSERT OR IGNORE INTO gauges 
            (address, pool, internal_bribe, external_bribe, is_alive, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (
            gauge['address'],
            gauge['pool'],
            gauge['internalBribe'],
            gauge['externalBribe'],
            gauge['isAlive'],
            gauge['blockTimestamp']
        ))
        new_gauges += 1

db.session.commit()

print(f"✓ New gauges added: {new_gauges}")
print(f"✓ Existing gauges: {existing_gauges}")
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
    cursor = db.session.execute(
        "SELECT address, internal_bribe, external_bribe FROM gauges WHERE LOWER(address) = LOWER(?)",
        (gauge_addr,)
    )
    result = cursor.fetchone()
    
    if result:
        print(f"✓ {gauge_addr}")
        print(f"  Internal: {result[1]}")
        print(f"  External: {result[2]}")
    else:
        print(f"❌ {gauge_addr} - STILL NOT FOUND")

db.close()
print("\n✓ Done!")
