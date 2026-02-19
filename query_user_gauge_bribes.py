#!/usr/bin/env python3
"""
Query ONLY the user's voted gauges for missing reward tokens
"""

import json
import sys
from web3 import Web3
import time

w3 = Web3(Web3.HTTPProvider("https://base-mainnet.g.alchemy.com/v2/oFfvEpXYjGo8Nj4QQIkU3kXd6Z0JvfJZ"))
print(f"Connected to Base: {w3.is_connected()}\n")

with open("bribev2_abi.json", "r") as f:
    bribev2_abi = json.load(f)

# User's 10 voted gauges
user_gauges = [
    "0x07388f67042bc2dc54876e0c99e543625bd2a9da",
    "0x22f0afdda80fbca0d96e29384814a897cbadab59",
    "0xac396cabf5832a49483b78225d902c0999829993",
    "0xee5f8bf7cdb1ad421993a368b15d06ad58122dab",
    "0x0a2918e8c5ef5ec8bc37de77a03f0b1ad66ae23e",
    "0x1df220b4c8b7e3d8a48f3eb77e0c8a7c9e9b3f0c",
    "0x6321d73080fbac4c99c5e9a8b8e8f7e6d5c4b3a2",
    "0x5d4a13c782502e9f21fa6e257b5b78b4d8eb9f80",
    "0xee102ec3883f1a1f1c346e317c581e0636dfce6f",
    "0x7d1bb380a7275a47603dab3b6521d5a8712dfba5",
]

# Missing tokens
missing_tokens = {
    "BETR": "0x051024b653e8ec69e72693f776c41c2a9401fb07",
    "FACY": "0xfac77f01957ed1b3dd1cbea992199b8f85b6e886",
    "FUEGO": "0x36912b5cf63e509f18e53ac98b3012fa79e77bf5",
    "PIGGY": "0xe3cf8dbcbdc9b220ddead0bd6342e245daff934d",
    "axlREGEN": "0x2e6c05f1f7d1f4eb9a088bf12257f1647682b754",
    "chubal": "0xd302a92fb82ea59aa676ae3d5799ac296afa7390",
    "metacademax": "0xbb2db41e62abf596b7f8ca7bd4733a7b357f5ab9",
}

# Get bribe contracts from database
import sqlite3
db = sqlite3.connect("data.db")
cursor = db.cursor()

found_tokens = {}

print("Checking user's 10 voted gauges...")
print("=" * 80)

for gauge_addr in user_gauges:
    cursor.execute("""
        SELECT internal_bribe, external_bribe 
        FROM gauges 
        WHERE LOWER(address) = LOWER(?)
    """, (gauge_addr,))
    result = cursor.fetchone()
    
    if not result:
        print(f"\n❌ {gauge_addr[:10]}... NOT IN DATABASE")
        continue
    
    internal_bribe, external_bribe = result
    print(f"\n✓ {gauge_addr[:10]}...")
    
    for bribe_type, bribe_addr in [("Internal", internal_bribe), ("External", external_bribe)]:
        if bribe_addr == "0x0000000000000000000000000000000000000000":
            continue
        
        print(f"  {bribe_type[:3]}: {bribe_addr[:10]}... ", end='', flush=True)
        
        try:
            bribe_contract = w3.eth.contract(
                address=w3.to_checksum_address(bribe_addr), 
                abi=bribev2_abi
            )
            
            rewards_count = bribe_contract.functions.rewardsListLength().call()
            print(f"{rewards_count} tokens →", end='', flush=True)
            
            # Check each token
            found_here = []
            for i in range(rewards_count):
                try:
                    token_addr = bribe_contract.functions.rewardTokens(i).call()
                    token_lower = token_addr.lower()
                    
                    for symbol, missing_addr in missing_tokens.items():
                        if missing_addr.lower() == token_lower:
                            if symbol not in found_tokens:
                                found_tokens[symbol] = []
                            found_tokens[symbol].append({
                                'gauge': gauge_addr,
                                'contract': bribe_addr,
                                'type': bribe_type
                            })
                            found_here.append(symbol)
                except:
                    pass
            
            if found_here:
                print(f" ✓ {', '.join(found_here)}")
            else:
                print(" none")
            
            time.sleep(0.1)
            
        except Exception as e:
            print(f" Error: {type(e).__name__}")

db.close()

print("\n" + "=" * 80)
print("SUMMARY")
print("=" * 80)

if found_tokens:
    print(f"\nMissing tokens found in YOUR voted gauges:")
    for symbol in sorted(found_tokens.keys()):
        print(f"\n  {symbol}:")
        for loc in found_tokens[symbol]:
            print(f"    • {loc['type']:8} {loc['gauge'][:10]}... → {loc['contract'][:10]}...")
else:
    print("\n❌ No missing tokens found in your voted gauges")

print(f"\nFound: {len(found_tokens)}/7 missing tokens")

still_missing = set(missing_tokens.keys()) - set(found_tokens.keys())
if still_missing:
    print(f"\nStill missing: {', '.join(sorted(still_missing))}")
