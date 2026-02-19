#!/usr/bin/env python3
"""
Query subgraph for all reward tokens added to your gauges' bribe contracts.
This shows which tokens have emitted RewardAdded events in your bribe contracts.
"""

import sys
from datetime import datetime
from collections import defaultdict
from config import Config
from src.database import Database
from src.subgraph_client import SubgraphClient

def main():
    print("=" * 100)
    print("QUERYING SUBGRAPH FOR BRIBE TOKENS")
    print("=" * 100)
    print()
    
    # Initialize
    db = Database(Config.DATABASE_PATH)
    client = SubgraphClient()
    
    # Your voted gauges
    your_gauges = [
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
    
    print(f"Loading gauge data for {len(your_gauges)} gauges...")
    print()
    
    # Load gauge data from database
    session = db.get_session()
    from src.database import Gauge
    
    all_gauges = session.query(Gauge).all()
    gauge_map = {g.address.lower(): g for g in all_gauges}
    session.close()
    
    # Collect all bribe contract addresses
    bribe_contracts = []  # [(gauge_addr, contract_addr, type)]
    
    for gauge_addr in your_gauges:
        gauge_addr_lower = gauge_addr.lower()
        gauge = gauge_map.get(gauge_addr_lower)
        
        if not gauge:
            print(f"⚠️  Gauge {gauge_addr[:10]}... not in database")
            continue
        
        if gauge.internal_bribe and gauge.internal_bribe.lower() != "0x0000000000000000000000000000000000000000":
            bribe_contracts.append((gauge_addr_lower, gauge.internal_bribe.lower(), "internal"))
        
        if gauge.external_bribe and gauge.external_bribe.lower() != "0x0000000000000000000000000000000000000000":
            bribe_contracts.append((gauge_addr_lower, gauge.external_bribe.lower(), "external"))
    
    print(f"Total bribe contracts to query: {len(bribe_contracts)}")
    print()
    
    # Query all bribes for these contracts
    print("Querying subgraph for all RewardAdded events in these contracts...")
    print()
    
    all_bribes = client.fetch_all_paginated(client.fetch_bribes)
    
    print(f"Total bribes in subgraph: {len(all_bribes)}")
    print()
    
    # Filter to only your bribe contracts
    bribe_contract_addrs = set(addr for _, addr, _ in bribe_contracts)
    your_bribes = [b for b in all_bribes if b['bribeContract'].lower() in bribe_contract_addrs]
    
    print(f"Bribes in YOUR gauge contracts: {len(your_bribes)}")
    print()
    
    # Group by contract and token
    tokens_by_contract = defaultdict(set)  # contract_addr -> set of token addresses
    tokens_by_gauge = defaultdict(lambda: {"internal": set(), "external": set()})
    
    for bribe in your_bribes:
        contract_addr = bribe['bribeContract'].lower()
        token_addr = bribe['rewardToken'].lower()
        
        tokens_by_contract[contract_addr].add(token_addr)
        
        # Find which gauge this belongs to
        for gauge_addr, bribe_addr, bribe_type in bribe_contracts:
            if bribe_addr == contract_addr:
                tokens_by_gauge[gauge_addr][bribe_type].add(token_addr)
                break
    
    # Display results per gauge
    print("=" * 100)
    print("TOKENS FOUND PER GAUGE")
    print("=" * 100)
    print()
    
    for i, gauge_addr in enumerate(your_gauges, 1):
        gauge_addr_lower = gauge_addr.lower()
        
        if gauge_addr_lower not in tokens_by_gauge:
            print(f"{i}. {gauge_addr[:10]}... - No bribes found")
            continue
        
        print(f"{i}. Gauge: {gauge_addr[:10]}...")
        
        internal_tokens = tokens_by_gauge[gauge_addr_lower]["internal"]
        external_tokens = tokens_by_gauge[gauge_addr_lower]["external"]
        
        if internal_tokens:
            print(f"   Internal ({len(internal_tokens)} tokens):")
            for token in sorted(internal_tokens):
                print(f"     • {token}")
        else:
            print(f"   Internal: No tokens")
        
        if external_tokens:
            print(f"   External ({len(external_tokens)} tokens):")
            for token in sorted(external_tokens):
                print(f"     • {token}")
        else:
            print(f"   External: No tokens")
        
        print()
    
    # Collect all unique tokens
    all_unique_tokens = set()
    for tokens_dict in tokens_by_gauge.values():
        all_unique_tokens.update(tokens_dict["internal"])
        all_unique_tokens.update(tokens_dict["external"])
    
    print()
    print("=" * 100)
    print("SUMMARY")
    print("=" * 100)
    print()
    
    print(f"Total unique reward tokens found: {len(all_unique_tokens)}")
    print()
    
    if all_unique_tokens:
        print("All unique token addresses:")
        for token in sorted(all_unique_tokens):
            # Count how many contracts have this token
            contract_count = sum(1 for tokens in tokens_by_contract.values() if token in tokens)
            print(f"  {token} (in {contract_count} contracts)")
    
    print()
    print("=" * 100)
    print("COMPARISON WITH ACTUAL REWARDS RECEIVED")
    print("=" * 100)
    print()
    
    print("Your actual Jan 29 rewards included these token addresses:")
    actual_tokens = {
        "0x00000e7efa313f4e11bfff432471ed9423ac6b30": "HYDX",
        "0x051024b653e8ec69e72693f776c41c2a9401fb07": "BETR",
        "0xa1136031150e50b015b41f1ca6b2e99e49d8cb78": "oHYDX",
        "0xbb2db41e62abf596b7f8ca7bd4733a7b357f5ab9": "metacademax",
        "0xfac77f01957ed1b3dd1cbea992199b8f85b6e886": "FACY",
        "0x36912b5cf63e509f18e53ac98b3012fa79e77bf5": "FUEGO",
        "0xe3cf8dbcbdc9b220ddead0bd6342e245daff934d": "PIGGY",
        "0x2e6c05f1f7d1f4eb9a088bf12257f1647682b754": "axlREGEN",
        "0xd302a92fb82ea59aa676ae3d5799ac296afa7390": "chubal",
        "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913": "USDC",
        "0x4200000000000000000000000000000000000006": "WETH",
    }
    
    print()
    print(f"{'Symbol':<12} {'Address':<44} {'Found in Subgraph':<20}")
    print("-" * 80)
    
    found_count = 0
    for token_addr, symbol in sorted(actual_tokens.items(), key=lambda x: x[1]):
        found = "✓ YES" if token_addr.lower() in all_unique_tokens else "❌ NO"
        if token_addr.lower() in all_unique_tokens:
            found_count += 1
        print(f"{symbol:<12} {token_addr:<44} {found:<20}")
    
    print()
    print(f"Total found: {found_count}/{len(actual_tokens)}")
    print()
    
    if found_count == 0:
        print("⚠️  CRITICAL: NONE of the tokens you received are in the subgraph's Bribe events")
        print()
        print("This means:")
        print("1. The RewardAdded events for these tokens were never emitted, OR")
        print("2. The subgraph is not indexing them, OR")
        print("3. You claimed from a completely different contract/mechanism")
        print()
        print("Next steps:")
        print("- Check BaseScan for the claim transaction to see which contract you called")
        print("- Verify the subgraph is indexing RewardAdded events correctly")
    elif found_count < len(actual_tokens):
        print(f"⚠️  {len(actual_tokens) - found_count} tokens are missing from subgraph")
        print()
        missing = [symbol for addr, symbol in actual_tokens.items() if addr.lower() not in all_unique_tokens]
        print(f"Missing tokens: {', '.join(missing)}")
    else:
        print("✅ All tokens found in subgraph!")
    
    print()


if __name__ == "__main__":
    main()
