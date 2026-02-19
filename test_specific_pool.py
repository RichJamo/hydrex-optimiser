#!/usr/bin/env python3
"""
Test the exact pool address from block explorer.
"""

import json
from web3 import Web3

# Configuration
RPC_URL = "https://base-mainnet.g.alchemy.com/v2/oFfvEpXYjGo8Nj4QQIkU3kXd6Z0JvfJZ"
VOTER_ADDRESS = "0xc69E3eF39E3fFBcE2A1c570f8d3ADF76909ef17b"

# The pool address you tested on block explorer
TEST_POOL = "0x19FF35059452Faa793DdDF9894a1571c5D41003e"
EXPECTED_VOTES = 3555800149096668538821

# Setup Web3
w3 = Web3(Web3.HTTPProvider(RPC_URL))
print(f"Connected to Base: {w3.is_connected()}\n")

# Load ABI
with open("voterv5_abi.json", "r") as f:
    voter_abi = json.load(f)

voter = w3.eth.contract(
    address=Web3.to_checksum_address(VOTER_ADDRESS),
    abi=voter_abi
)

print("=" * 80)
print("TESTING EXACT POOL FROM BLOCK EXPLORER")
print("=" * 80)

print(f"\nPool address: {TEST_POOL}")
print(f"Expected votes: {EXPECTED_VOTES:,}\n")

# Test 1: Direct query with checksum
print("Test 1: Query with checksum address")
try:
    pool_checksum = Web3.to_checksum_address(TEST_POOL)
    votes = voter.functions.weights(pool_checksum).call()
    print(f"  Result: {votes:,}")
    if votes == EXPECTED_VOTES:
        print("  ✓ MATCH!")
    elif votes == 0:
        print("  ❌ Got 0 (expected non-zero)")
    else:
        print(f"  ⚠️  Different value (expected {EXPECTED_VOTES:,})")
except Exception as e:
    print(f"  ❌ Error: {e}")

# Test 2: Query without checksum (lowercase)
print("\nTest 2: Query with lowercase address")
try:
    votes = voter.functions.weights(TEST_POOL.lower()).call()
    print(f"  Result: {votes:,}")
    if votes == EXPECTED_VOTES:
        print("  ✓ MATCH!")
    elif votes == 0:
        print("  ❌ Got 0 (expected non-zero)")
except Exception as e:
    print(f"  ❌ Error: {e}")

# Test 3: Check if this pool exists in the voter contract
print("\nTest 3: Check if pool is registered in voter")
try:
    pool_checksum = Web3.to_checksum_address(TEST_POOL)
    gauge = voter.functions.gauges(pool_checksum).call()
    print(f"  Gauge address: {gauge}")
    if gauge == "0x0000000000000000000000000000000000000000":
        print("  ⚠️  No gauge registered for this pool!")
    else:
        print("  ✓ Gauge exists")
        
        # Try to get pool back from gauge
        pool_from_gauge = voter.functions.poolForGauge(gauge).call()
        print(f"  Pool from gauge: {pool_from_gauge}")
        if pool_from_gauge.lower() == TEST_POOL.lower():
            print("  ✓ Pool mapping is correct")
except Exception as e:
    print(f"  Error: {e}")

print("\n" + "=" * 80)
