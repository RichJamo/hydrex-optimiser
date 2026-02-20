#!/usr/bin/env python3
"""
Query database for full details on the mystery gauges.
"""

import sqlite3

conn = sqlite3.connect('/Users/richardjamieson/Documents/GitHub/hydrex-optimiser/hydrex_data.db')
cursor = conn.cursor()

mystery_gauges = [
    "0xee5f8bf7cdb1ad421993a368b15d06ad58122dab",  # $246.75
    "0xe63cd99406e98d909ab6d702b11dd4cd31a425a2",  # $236.11
    "0xdc470dc0b3247058ea4605dba6e48a9b2a083971",  # $245.80
    "0x1df220b45408a11729302ec84a1443d98beccc57",  # $227.67
]

print("\nMYSTERY GAUGE DATABASE DETAILS")
print("=" * 80)

for gauge_addr in mystery_gauges:
    print(f"\nGauge: {gauge_addr}")
    
    cursor.execute("""
        SELECT address, pool, internal_bribe, external_bribe, is_alive
        FROM gauges
        WHERE LOWER(address) = LOWER(?)
    """, (gauge_addr,))
    
    result = cursor.fetchone()
    if result:
        address, pool, internal, external, is_alive = result
        print(f"  Pool in DB:     {pool}")
        print(f"  Internal Bribe: {internal}")
        print(f"  External Bribe: {external}")
        print(f"  Is Alive:       {is_alive}")
        
        # Check if pool == gauge (which would be unusual)
        if pool.lower() == gauge_addr.lower():
            print(f"  ⚠️  Pool address equals gauge address (unusual!)")
    else:
        print(f"  ❌ Not found in database")

conn.close()

print("\n" + "=" * 80)
print("\nThis explains everything! These gauges have pool == gauge address,")
print("which means they're not standard LP pool gauges.")
print("They could be:")
print("  - Fee distributor gauges (auto-distribute fees to voters)")
print("  - Protocol earnings gauges")
print("  - Special reward gauges")
print("\nUser votes for LP pools, but receives share of protocol-wide fee")
print("distribution from these special gauges!")
