#!/usr/bin/env python3
"""Quick test - query ONE bribe contract to verify it works"""

import json
from web3 import Web3

w3 = Web3(Web3.HTTPProvider("https://mainnet.base.org/"))
print(f"Connected: {w3.is_connected()}")

with open("bribev2_abi.json", "r") as f:
    abi = json.load(f)

# Test with one known contract
test_addr = w3.to_checksum_address("0xc8fab1cba87f12c64700049ffb57e662778ce499")
print(f"Testing: {test_addr}")

contract = w3.eth.contract(address=test_addr, abi=abi)

try:
    count = contract.functions.rewardsListLength().call()
    print(f"✓ Rewards: {count}")
except Exception as e:
    print(f"❌ Error: {e}")
