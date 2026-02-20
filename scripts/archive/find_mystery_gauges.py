#!/usr/bin/env python3
"""
Find which gauges the 5 mystery bribe contracts belong to
"""

from web3 import Web3
import sqlite3

w3 = Web3(Web3.HTTPProvider("https://base-mainnet.g.alchemy.com/v2/oFfvEpXYjGo8Nj4QQIkU3kXd6Z0JvfJZ"))
print(f"Connected: {w3.is_connected()}\n")

# The 5 bribe contracts that paid rewards
mystery_bribes = [
    ("0xdbd3da2c3183a4db0d6a1e648a06b14b593db7b5", "WETH + cbBTC", 246.75),
    ("0x71aae818cd357f62c3ad25b5012cc27587442aae", "cbBTC + USDC", 236.11),
    ("0x7c02e7a38774317dfc72c2506fd642de2c55a7de", "USDC + kVCM", 10.71),
    ("0xc96802e581c7b7ecc4ccff37e0ee2b60bbe6741f", "WETH + BNKR", 227.67),
    ("0x6b4e7d1752257cdc266b380b0f980cf75d3a2465", "kVCM", 235.09),
]

print("FINDING GAUGES FOR MYSTERY BRIBE CONTRACTS")
print("=" * 80)

# Query database
db = sqlite3.connect("data.db")
cursor = db.cursor()

found_in_db = []
not_found = []

for bribe_addr, tokens, value in mystery_bribes:
    cursor.execute("""
        SELECT address, pool, internal_bribe, external_bribe 
        FROM gauges 
        WHERE LOWER(internal_bribe) = LOWER(?) OR LOWER(external_bribe) = LOWER(?)
    """, (bribe_addr, bribe_addr))
    
    result = cursor.fetchone()
    
    print(f"\nBribe Contract: {bribe_addr}")
    print(f"  Rewards: {tokens} (${value:.2f})")
    
    if result:
        gauge_addr, pool_addr, internal, external = result
        bribe_type = "INTERNAL" if internal.lower() == bribe_addr.lower() else "EXTERNAL"
        print(f"  ✓ Found in database!")
        print(f"    Gauge: {gauge_addr}")
        print(f"    Pool:  {pool_addr}")
        print(f"    Type:  {bribe_type}")
        found_in_db.append((bribe_addr, gauge_addr, pool_addr, bribe_type, value))
    else:
        print(f"  ❌ Not in database")
        not_found.append((bribe_addr, tokens, value))

db.close()

print("\n" + "=" * 80)
print("SUMMARY")
print("=" * 80)

print(f"\nFound in database: {len(found_in_db)}/5")
print(f"Not in database: {len(not_found)}/5")

if found_in_db:
    print(f"\nRewards from gauges in database:")
    for bribe, gauge, pool, btype, value in found_in_db:
        print(f"  ${value:7.2f} - {pool[:10]}... ({btype})")

if not_found:
    print(f"\nRewards from unknown gauges (need to add to database):")
    for bribe, tokens, value in not_found:
        print(f"  ${value:7.2f} - {bribe[:10]}... ({tokens})")

print(f"\nTotal rewards: ${sum(v for _, _, _, _, v in found_in_db) + sum(v for _, _, v in not_found):.2f}")
