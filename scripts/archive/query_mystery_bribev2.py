#!/usr/bin/env python3
"""
Query VoterV5 proxy contract using BribeV2 ABI
Since VoterV5 is a proxy that delegatecalls to BribeV2 implementation,
the reward tokens are stored in VoterV5's storage, not in the implementation contract.
"""

import json
import sys
from web3 import Web3

# Initialize Web3
w3 = Web3(Web3.HTTPProvider("https://mainnet.base.org/"))
print(f"Connected to Base: {w3.is_connected()}\n")

# Load BribeV2 ABI
with open("bribev2_abi.json", "r") as f:
    bribev2_abi = json.load(f)

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

# Query VoterV5 (the proxy that holds the actual storage)
voter_v5_addr = w3.to_checksum_address("0xc69E3eF39E3fFBcE2A1c570f8d3ADF76909ef17b")

print(f"Querying VoterV5 proxy using BribeV2 ABI: {voter_v5_addr}")
print("=" * 80)

contract = w3.eth.contract(address=voter_v5_addr, abi=bribev2_abi)

try:
    # Get number of rewards
    rewards_count = contract.functions.rewardsListLength().call()
    print(f"\n✓ Total reward tokens registered: {rewards_count}")
    
    # Get each token address
    print(f"\nTokens in this contract:")
    print("-" * 80)
    
    found_tokens = {}
    
    for i in range(rewards_count):
        try:
            token_addr = contract.functions.rewardTokens(i).call()
            token_addr_lower = token_addr.lower()
            
            # Check if this is one of our missing tokens
            matching_symbol = None
            for symbol, addr in missing_tokens.items():
                if addr.lower() == token_addr_lower:
                    matching_symbol = symbol
                    break
            
            if matching_symbol:
                print(f"{i}. {token_addr} ✓ FOUND: {matching_symbol}")
                found_tokens[matching_symbol] = token_addr
            else:
                print(f"{i}. {token_addr}")
                
        except Exception as e:
            print(f"{i}. Error: {e}")
    
    print("\n" + "=" * 80)
    print("SUMMARY")
    print("=" * 80)
    print(f"\nTokens found that match missing rewards:")
    for symbol, addr in found_tokens.items():
        print(f"  ✓ {symbol}: {addr}")
    
    print(f"\nMatched: {len(found_tokens)}/{len(missing_tokens)}")
    
    missing = set(missing_tokens.keys()) - set(found_tokens.keys())
    if missing:
        print(f"\nStill missing:")
        for symbol in missing:
            print(f"  ❌ {symbol}: {missing_tokens[symbol]}")

except Exception as e:
    print(f"Error querying contract: {e}")
    sys.exit(1)
