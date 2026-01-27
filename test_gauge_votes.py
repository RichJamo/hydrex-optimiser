#!/usr/bin/env python3
"""Test GaugeVote data from updated subgraph."""

import logging
from config import Config
from src.subgraph_client import SubgraphClient

logging.basicConfig(level=logging.INFO)

client = SubgraphClient(Config.SUBGRAPH_URL)

print(f"Testing subgraph: {Config.SUBGRAPH_URL}\n")

# Test 1: Get some gauges
print("1. Testing gauges:")
gauges = client.fetch_gauges(first=3)
print(f"   Found {len(gauges)} gauges")
if gauges:
    print(f"   First gauge: {gauges[0]['address']}")

# Test 2: Test GaugeVote entity
print("\n2. Testing GaugeVote data:")
try:
    gauge_votes = client.fetch_gauge_votes(first=10)
    print(f"   Found {len(gauge_votes)} gauge votes")
    if gauge_votes:
        gv = gauge_votes[0]
        print(f"   First: Epoch {gv['epoch']}, Gauge {gv['gauge']['address']}, Weight {gv['weight']}")
        print(f"   Voter: {gv['voter']}")
except Exception as e:
    print(f"   Error: {e}")

# Test 3: Get bribes
print("\n3. Testing bribes:")
try:
    bribes = client.fetch_bribes(first=5)
    print(f"   Found {len(bribes)} bribes")
    if bribes:
        b = bribes[0]
        print(f"   First: {b['rewardToken']} amount {b['amount']}")
except Exception as e:
    print(f"   Error: {e}")

print("\n✅ Subgraph test complete!")
