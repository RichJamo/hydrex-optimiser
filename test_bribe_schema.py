"""
Test script to verify bribe data once subgraph syncs with RewardAdded events.
Run this after the subgraph has fully synced.
"""

from src.subgraph_client import SubgraphClient
from config import Config

print("Testing Bribe Data from Updated Subgraph")
print("=" * 80)

client = SubgraphClient(Config.SUBGRAPH_URL)

# Test 1: Check if bribes query works with new schema
print("\n1. Testing bribes query with new schema (epoch field)...")
try:
    bribes = client.fetch_bribes(first=5)
    print(f"   ✓ Query successful, found {len(bribes)} bribes")
    
    if bribes:
        print("\n   Sample bribe:")
        bribe = bribes[0]
        print(f"     Epoch: {bribe.get('epoch')}")
        print(f"     Bribe Contract: {bribe.get('bribeContract')}")
        print(f"     Reward Token: {bribe.get('rewardToken')}")
        print(f"     Amount: {int(bribe.get('amount', 0)) / 1e18:.4f}")
        print(f"     Block: {bribe.get('blockNumber')}")
    else:
        print("   ℹ️  No bribes found yet (waiting for distributeFees() to be called)")
        
except Exception as e:
    print(f"   ❌ Error: {e}")

# Test 2: Check if we can query by epoch
print("\n2. Testing epoch filtering...")
try:
    # Try a recent epoch
    import time
    current_time = int(time.time())
    current_epoch = (current_time // 604800) * 604800
    
    bribes = client.fetch_bribes(epoch=current_epoch, first=10)
    print(f"   ✓ Epoch filter works, found {len(bribes)} bribes for epoch {current_epoch}")
    
except Exception as e:
    print(f"   ❌ Error: {e}")

# Test 3: Get all bribes to check total count
print("\n3. Fetching all bribes...")
try:
    all_bribes = client.fetch_all_paginated(client.fetch_bribes)
    print(f"   ✓ Total bribes in subgraph: {len(all_bribes)}")
    
    if all_bribes:
        # Group by epoch
        from collections import defaultdict
        by_epoch = defaultdict(int)
        by_contract = defaultdict(int)
        
        for bribe in all_bribes:
            epoch = int(bribe['epoch'])
            contract = bribe['bribeContract']
            by_epoch[epoch] += 1
            by_contract[contract] += 1
        
        print(f"\n   Bribes across {len(by_epoch)} epochs:")
        for epoch in sorted(by_epoch.keys())[-5:]:  # Show last 5 epochs
            import time
            date = time.strftime('%Y-%m-%d', time.gmtime(epoch))
            print(f"     {date} (epoch {epoch}): {by_epoch[epoch]} rewards")
        
        print(f"\n   Top 3 bribe contracts by event count:")
        for contract, count in sorted(by_contract.items(), key=lambda x: x[1], reverse=True)[:3]:
            print(f"     {contract}: {count} events")
    
except Exception as e:
    print(f"   ❌ Error: {e}")

print("\n" + "=" * 80)
print("\nNext steps:")
print("- If 0 bribes: Wait for VoterV5.distributeFees() to be called")
print("- If bribes found: Run 'python main.py backfill --start-block 35273810' to index them")
print("- Then run 'python main.py historical --epochs 5' to analyze ROI")
