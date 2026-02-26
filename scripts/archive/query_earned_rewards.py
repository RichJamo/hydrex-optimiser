#!/usr/bin/env python3
"""
Query BribeV2 contracts directly to find all available rewards per gauge.
Shows which tokens are actually registered in each internal/external bribe contract.
"""

import sys
import json
import time
from datetime import datetime
from web3 import Web3
from config import Config
from src.database import Database

# Load BribeV2 ABI
with open('bribev2_abi.json', 'r') as f:
    BRIBEV2_ABI = json.load(f)

def query_bribe_rewards(w3, bribe_address):
    """
    Query a single BribeV2 contract for all registered reward tokens.
    
    Returns:
        dict: {
            "contract": bribe_address,
            "tokens": [token_addr1, token_addr2, ...]
        }
    """
    if not bribe_address or bribe_address.lower() == "0x0000000000000000000000000000000000000000":
        return None
    
    try:
        bribe = w3.eth.contract(
            address=Web3.to_checksum_address(bribe_address),
            abi=BRIBEV2_ABI
        )
        
        # Get number of reward tokens
        length = bribe.functions.rewardsListLength().call()
        
        tokens = []
        for i in range(length):
            try:
                # Get token address using rewardTokens(index)
                token_addr = bribe.functions.rewardTokens(i).call()
                tokens.append(token_addr)
            except Exception as e:
                print(f"      ⚠️  Error fetching token {i}: {str(e)[:60]}")
                continue
        
        return {
            "contract": bribe_address,
            "token_count": length,
            "tokens": tokens
        }
        
    except Exception as e:
        print(f"    ❌ Error querying contract {bribe_address[:10]}...: {str(e)[:100]}")
        return None


def main():
    print("=" * 100)
    print("QUERYING BRIBEV2 CONTRACTS FOR EARNED REWARDS")
    print("=" * 100)
    print()
    
    # Initialize Web3
    w3 = Web3(Web3.HTTPProvider("https://mainnet.base.org"))
    if not w3.is_connected():
        print("❌ Failed to connect to Base RPC")
        sys.exit(1)
    
    # Initialize database
    db = Database(Config.DATABASE_PATH)
    owner_address = Config.MY_ESCROW_ADDRESS
    
    # Target epoch
    epoch_timestamp = 1769040000  # Jan 29, 2026
    epoch_date = datetime.utcfromtimestamp(epoch_timestamp).strftime('%Y-%m-%d')
    
    print(f"Owner: {owner_address}")
    print(f"Epoch: {epoch_date} (ts={epoch_timestamp})")
    print()
    
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
    
    print(f"Querying {len(your_gauges)} gauges...")
    print()
    
    # Load gauge data
    session = db.get_session()
    from src.database import Gauge
    
    all_gauges = session.query(Gauge).all()
    gauge_map = {g.address.lower(): g for g in all_gauges}
    session.close()
    
    all_tokens_found = {}  # address -> {contract, amount}
    gauge_results = {}
    
    for i, gauge_addr in enumerate(your_gauges, 1):
        gauge_addr_lower = gauge_addr.lower()
        gauge = gauge_map.get(gauge_addr_lower)
        
        if not gauge:
            print(f"{i}. {gauge_addr[:10]}... ⚠️  NOT IN DATABASE")
            continue
        
        print(f"{i}. Gauge: {gauge_addr[:10]}...")
        gauge_results[gauge_addr_lower] = {"internal": None, "external": None}
        
        # Query internal bribe
        if gauge.internal_bribe:
            print(f"   Internal: {gauge.internal_bribe[:10]}...", end="", flush=True)
            internal_result = query_bribe_rewards(w3, gauge.internal_bribe)
            time.sleep(0.5)  # Rate limiting
            
            if internal_result:
                print(f" → {internal_result['token_count']} tokens")
                for token_addr in internal_result['tokens']:
                    print(f"     • {token_addr}")
                    
                    if token_addr not in all_tokens_found:
                        all_tokens_found[token_addr] = {"contracts": []}
                    if gauge.internal_bribe not in all_tokens_found[token_addr]["contracts"]:
                        all_tokens_found[token_addr]["contracts"].append(gauge.internal_bribe)
                gauge_results[gauge_addr_lower]["internal"] = internal_result
            else:
                print(f" ❌ Unable to query")
        else:
            print(f"   Internal: None")
        
        # Query external bribe
        if gauge.external_bribe:
            print(f"   External: {gauge.external_bribe[:10]}...", end="", flush=True)
            external_result = query_bribe_rewards(w3, gauge.external_bribe)
            time.sleep(0.5)  # Rate limiting
            
            if external_result:
                print(f" → {external_result['token_count']} tokens")
                for token_addr in external_result['tokens']:
                    print(f"     • {token_addr}")
                    
                    if token_addr not in all_tokens_found:
                        all_tokens_found[token_addr] = {"contracts": []}
                    if gauge.external_bribe not in all_tokens_found[token_addr]["contracts"]:
                        all_tokens_found[token_addr]["contracts"].append(gauge.external_bribe)
                gauge_results[gauge_addr_lower]["external"] = external_result
            else:
                print(f" ❌ Unable to query")
        else:
            print(f"   External: None")
        
        print()
    
    # Summary
    print()
    print("=" * 100)
    print("SUMMARY")
    print("=" * 100)
    print()
    
    print(f"Total unique token addresses found across all contracts: {len(all_tokens_found)}")
    print()
    
    if all_tokens_found:
        print("Tokens Found:")
        print(f"{'Token Address':<44} {'Found in Contracts':<40}")
        print("-" * 85)
        
        for token_addr in sorted(all_tokens_found.keys()):
            contracts = all_tokens_found[token_addr]["contracts"]
            print(f"{token_addr:<44} {len(contracts)} contract(s)")
            for contract in contracts:
                print(f"{'':44} • {contract[:10]}...")
    else:
        print("⚠️  No tokens found in BribeV2 contracts")
    
    print()
    print("=" * 100)
    print("COMPARISON WITH ACTUAL REWARDS RECEIVED")
    print("=" * 100)
    print()
    
    print("Your actual Jan 29 rewards included these 10 unique token types:")
    actual_tokens = {
        "0x00000e7efa313f4e11bfff432471ed9423ac6b30": "HYDX",
        "0x051024b653e8ec69e72693f776c41c2a9401fb07": "BETR",
        "0xa1136031150e50b015b41f1ca6b2e99e49d8cb78": "oHYDX",
        "0xbb2db41e62abf596b7f8ca7bd4733a7b357f5ab9": "metacademax",
        "0xfac77f01957ed1b3dd1cbea992199b8f85b6e886": "FACY",
        "0x36912b5cf63e509f18e53ac98b3012fa79e77bf5": "FUEGO",
        "0xff8104251e7761163fac3211ef5583fb3f8583d6": "REPPO",  # Placeholder for OTTO
        "0xe3cf8dbcbdc9b220ddead0bd6342e245daff934d": "PIGGY",
        "0x2e6c05f1f7d1f4eb9a088bf12257f1647682b754": "axlREGEN",
        "0xd302a92fb82ea59aa676ae3d5799ac296afa7390": "chubal",
    }
    
    print()
    print("Checking if these were found in BribeV2 contracts:")
    print(f"{'Symbol':<10} {'Address':<44} {'Found in BribeV2':<15}")
    print("-" * 70)
    
    found_count = 0
    for token_addr, symbol in sorted(actual_tokens.items(), key=lambda x: x[1]):
        found = "✓ YES" if token_addr.lower() in all_tokens_found else "❌ NO"
        if token_addr.lower() in all_tokens_found:
            found_count += 1
        print(f"{symbol:<10} {token_addr:<44} {found:<15}")
    
    print()
    print(f"Total found: {found_count}/{len(actual_tokens)}")
    print()
    
    if found_count == 0:
        print("⚠️  NONE of the tokens you received are registered in BribeV2 contracts")
        print()
        print("This suggests:")
        print("1. You claimed from a different contract, OR")
        print("2. The tokens were claimed but not indexed in BribeV2 query, OR")
        print("3. Rewards came from outside the BribeV2 system entirely")
    
    print()


if __name__ == "__main__":
    main()

