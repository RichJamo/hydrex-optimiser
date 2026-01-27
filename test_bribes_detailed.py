#!/usr/bin/env python3
"""Test bribe data in detail."""

from config import Config
from src.subgraph_client import SubgraphClient

client = SubgraphClient(Config.SUBGRAPH_URL)

print(f"Testing bribes from: {Config.SUBGRAPH_URL}\n")

# Test 1: Try to get any bribes
print("1. Testing all bribes (no filters):")
try:
    bribes = client.fetch_bribes(first=10)
    print(f"   Found {len(bribes)} bribes")
    if bribes:
        b = bribes[0]
        print(f"   First bribe:")
        print(f"     Contract: {b['bribeContract']}")
        print(f"     Token: {b['rewardToken']}")
        print(f"     Amount: {b['amount']}")
        print(f"     From: {b['from']}")
        print(f"     Block: {b['blockNumber']}")
    else:
        print("   ⚠️ No bribes found at all")
except Exception as e:
    print(f"   ❌ Error: {e}")

# Test 2: Try with block range
print("\n2. Testing bribes in block range (35273810-41000000):")
try:
    bribes = client.fetch_bribes(block_gte=35273810, block_lte=41000000, first=10)
    print(f"   Found {len(bribes)} bribes")
    if bribes:
        for i, b in enumerate(bribes[:3]):
            print(f"   Bribe {i+1}: {b['rewardToken']} amount={b['amount']}")
except Exception as e:
    print(f"   ❌ Error: {e}")

# Test 3: Check the actual GraphQL response
print("\n3. Testing raw GraphQL query:")
query = """
{
  bribes(first: 5, orderBy: blockNumber, orderDirection: desc) {
    id
    bribeContract
    rewardToken
    amount
    from
    blockNumber
    blockTimestamp
    transactionHash
  }
}
"""
try:
    result = client.query(query)
    bribes = result.get('bribes', [])
    print(f"   Raw query returned {len(bribes)} bribes")
    if bribes:
        print(f"   First bribe: {bribes[0]}")
    else:
        print("   Response:", result)
except Exception as e:
    print(f"   ❌ Error: {e}")

print("\n4. Check if Bribe entity exists in schema:")
query = """
{
  __type(name: "Bribe") {
    name
    fields {
      name
      type {
        name
      }
    }
  }
}
"""
try:
    result = client.query(query)
    if result.get('__type'):
        print("   ✅ Bribe entity exists in schema")
        print(f"   Fields: {[f['name'] for f in result['__type']['fields']]}")
    else:
        print("   ❌ Bribe entity NOT in schema!")
except Exception as e:
    print(f"   ❌ Error: {e}")
