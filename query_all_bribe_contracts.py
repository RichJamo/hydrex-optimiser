#!/usr/bin/env python3
"""
Query VoterV5 to get all bribe contracts, then check each for reward tokens
"""

import json
import sys
from web3 import Web3
import time

# Initialize Web3
w3 = Web3(Web3.HTTPProvider("https://mainnet.base.org/"))
print(f"Connected to Base: {w3.is_connected()}\n")

# Load ABIs
with open("bribev2_abi.json", "r") as f:
    bribev2_abi = json.load(f)

# Read user gauges from database
import sqlite3
db = sqlite3.connect("data.db")
cursor = db.cursor()

# Get all gauges from database
cursor.execute("""
    SELECT address, internal_bribe, external_bribe
    FROM gauges 
    ORDER BY address
""")
gauges_db = cursor.fetchall()
db.close()

# Known missing tokens from actual rewards
missing_tokens = {
    "BETR": "0x051024b653e8ec69e72693f776c41c2a9401fb07",
    "FACY": "0xfac77f01957ed1b3dd1cbea992199b8f85b6e886",
    "FUEGO": "0x36912b5cf63e509f18e53ac98b3012fa79e77bf5",
    "PIGGY": "0xe3cf8dbcbdc9b220ddead0bd6342e245daff934d",
    "axlREGEN": "0x2e6c05f1f7d1f4eb9a088bf12257f1647682b754",
    "chubal": "0xd302a92fb82ea59aa676ae3d5799ac296afa7390",
    "metacademax": "0xbb2db41e62abf596b7f8ca7bd4733a7b357f5ab9",
}

# VoterV5 address
voterv5_addr = w3.to_checksum_address("0xc69E3eF39E3fFBcE2A1c570f8d3ADF76909ef17b")

print(f"Querying VoterV5 for bribe contract addresses")
print("=" * 80)

# Create VoterV5 contract instance (use minimal ABI for the functions we need)
voterv5_abi = [
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
    }
]

voterv5 = w3.eth.contract(address=voterv5_addr, abi=voterv5_abi)

all_found_tokens = {}
contracts_checked = 0
total_contracts = len(gauges_db) * 2

for gauge_addr, internal_bribe, external_bribe in gauges_db:
    gauge_checksum = w3.to_checksum_address(gauge_addr)
    
    print(f"\nGauge: {gauge_addr[:10]}...")
    
    # Query each bribe contract
    for bribe_type, bribe_addr in [("Internal", internal_bribe), ("External", external_bribe)]:
        if bribe_addr == "0x0000000000000000000000000000000000000000":
            continue
        
        contracts_checked += 1
        print(f"  [{contracts_checked}/{total_contracts}] {bribe_type[:3]}: {bribe_addr[:10]}... ", end='', flush=True)
        
        bribe_addr_checksum = w3.to_checksum_address(bribe_addr)
        bribe_contract = w3.eth.contract(address=bribe_addr_checksum, abi=bribev2_abi)
        
        try:
            rewards_count = bribe_contract.functions.rewardsListLength().call()
            print(f"{rewards_count} tokens", flush=True)
            
            if rewards_count > 0:
                
                # Get each token
                for i in range(rewards_count):
                    try:
                        token_addr = bribe_contract.functions.rewardTokens(i).call()
                        token_lower = token_addr.lower()
                        
                        # Check if it's one of our missing tokens
                        for symbol, addr in missing_tokens.items():
                            if addr.lower() == token_lower:
                                if symbol not in all_found_tokens:
                                    all_found_tokens[symbol] = []
                                all_found_tokens[symbol].append({
                                    'contract': bribe_addr,
                                    'type': bribe_type,
                                    'gauge': gauge_addr
                                })
                                print(f"      ✓ Found {symbol}!")
                                break
                    except Exception as e:
                        # Rate limiting or other errors - skip
                        pass
                
                # Small delay to avoid rate limiting
                time.sleep(0.1)
                
        except Exception as e:
            print(f"Error: {type(e).__name__}", flush=True)

print("\n" + "=" * 80)
print("SUMMARY")
print("=" * 80)

if all_found_tokens:
    print(f"\nMissing tokens found in bribe contracts:")
    for symbol, locations in all_found_tokens.items():
        print(f"\n  {symbol}:")
        for loc in locations:
            print(f"    • {loc['type']:8} in {loc['gauge']} → {loc['contract']}")
else:
    print("\n❌ No missing tokens found in any bribe contracts")

print(f"\nMatched tokens: {len(all_found_tokens)}/{len(missing_tokens)}")
