#!/usr/bin/env python3
"""
Fix all gauges in database by calling stakeToken() to get correct pool addresses.
The database currently has gauge addresses stored as pool addresses - this is wrong.
"""

import sqlite3
from web3 import Web3
import time

# Connect to Base via Alchemy
w3 = Web3(Web3.HTTPProvider('https://base-mainnet.g.alchemy.com/v2/oFfvEpXYjGo8Nj4QQIkU3kXd6Z0JvfJZ'))

# Minimal Gauge ABI - just need stakeToken
GAUGE_ABI = [
    {
        "inputs": [],
        "name": "stakeToken",
        "outputs": [{"internalType": "address", "name": "", "type": "address"}],
        "stateMutability": "view",
        "type": "function"
    }
]

# Connect to database
conn = sqlite3.connect('/Users/richardjamieson/Documents/GitHub/hydrex-optimiser/hydrex_data.db')
cursor = conn.cursor()

# Get all gauges
cursor.execute("SELECT address, pool FROM gauges")
all_gauges = cursor.fetchall()

print(f"\nFIXING POOL ADDRESSES FOR {len(all_gauges)} GAUGES")
print("=" * 80)

updated = 0
errors = 0
already_correct = 0

for gauge_addr, stored_pool in all_gauges:
    try:
        # Query gauge contract for actual pool address
        gauge_contract = w3.eth.contract(
            address=Web3.to_checksum_address(gauge_addr), 
            abi=GAUGE_ABI
        )
        
        actual_pool = gauge_contract.functions.stakeToken().call()
        
        # Check if stored pool is wrong (equals gauge address)
        if stored_pool.lower() == gauge_addr.lower():
            # Update with correct pool address
            cursor.execute("""
                UPDATE gauges 
                SET pool = ?
                WHERE LOWER(address) = LOWER(?)
            """, (actual_pool, gauge_addr))
            
            print(f"✓ Fixed {gauge_addr[:10]}... → pool {actual_pool[:10]}...")
            updated += 1
        else:
            # Verify stored pool matches actual pool
            if stored_pool.lower() == actual_pool.lower():
                already_correct += 1
            else:
                print(f"⚠️  {gauge_addr[:10]}... stored={stored_pool[:10]}... actual={actual_pool[:10]}...")
                # Update anyway since it's wrong
                cursor.execute("""
                    UPDATE gauges 
                    SET pool = ?
                    WHERE LOWER(address) = LOWER(?)
                """, (actual_pool, gauge_addr))
                updated += 1
        
        # Rate limit protection
        if (updated + already_correct + errors) % 10 == 0:
            time.sleep(0.1)
            
    except Exception as e:
        print(f"❌ Error for {gauge_addr[:10]}...: {e}")
        errors += 1

# Commit changes
conn.commit()
conn.close()

print("\n" + "=" * 80)
print(f"SUMMARY:")
print(f"  Updated:         {updated}")
print(f"  Already correct: {already_correct}")
print(f"  Errors:          {errors}")
print(f"  Total:           {len(all_gauges)}")
print("=" * 80)
