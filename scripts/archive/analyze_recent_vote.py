#!/usr/bin/env python3
"""
Analyze user's specific vote → claim flow for 4 pools
"""

from web3 import Web3
import sqlite3

w3 = Web3(Web3.HTTPProvider("https://base-mainnet.g.alchemy.com/v2/oFfvEpXYjGo8Nj4QQIkU3kXd6Z0JvfJZ"))
print(f"Connected: {w3.is_connected()}\n")

db = sqlite3.connect("data.db")
cursor = db.cursor()

# User's 4 voted pools
voted_pools = [
    "0x19FF35059452Faa793DdDF9894a1571c5D41003e",
    "0xe62a34Ae5e0B9FdE3501Aeb72DC9585Bb3B72A7e",
    "0xF19787f048b3401546aa7A979afa79D555C114Dd",
    "0xE539b14a87D3Db4a2945ac99b29A69DE61531592",
]

# Bribe contracts that sent tokens (from claim tx) - FULL ADDRESSES
bribe_contracts_paid = [
    "0xdbd3DA2c3183a4db0d6a1E648a06B14b593dB7B5",
    "0x71aaE818Cd357f62C3aD25B5012cC27587442AaE",
    "0x7c02E7A38774317DFC72c2506FD642De2C55A7de",
    "0xC96802e581c7B7ecC4ccFF37e0eE2b60bBe6741f",
    "0x6b4e7d1752257cdc266b380b0F980cF75D3a2465",
]

# Tokens received with values
tokens_received = [
    {"token": "WETH", "amount": 0.054639611306253636, "value": 123.67, "from": "0xdbd3DA2c3183a4db0d6a1E648a06B14b593dB7B5"},
    {"token": "cbBTC", "amount": 0.00161239, "value": 123.08, "from": "0xdbd3DA2c3183a4db0d6a1E648a06B14b593dB7B5"},
    {"token": "cbBTC", "amount": 0.00163285, "value": 124.64, "from": "0x71aaE818Cd357f62C3aD25B5012cC27587442AaE"},
    {"token": "USDC", "amount": 111.46623, "value": 111.47, "from": "0x71aaE818Cd357f62C3aD25B5012cC27587442AaE"},
    {"token": "USDC", "amount": 1.999759, "value": 2.00, "from": "0x7c02E7A38774317DFC72c2506FD642De2C55A7de"},
    {"token": "kVCM", "amount": 96.918577040758863272, "value": 8.71, "from": "0x7c02E7A38774317DFC72c2506FD642De2C55A7de"},
    {"token": "WETH", "amount": 0.058558857516738677, "value": 132.54, "from": "0xC96802e581c7B7ecC4ccFF37e0eE2b60bBe6741f"},
    {"token": "BNKR", "amount": 171235.812464587854919736, "value": 95.13, "from": "0xC96802e581c7B7ecC4ccFF37e0eE2b60bBe6741f"},
    {"token": "kVCM", "amount": 2614.531578534736797237, "value": 235.09, "from": "0x6b4e7d1752257cdc266b380b0F980cF75D3a2465"},
]

print("USER'S VOTE ANALYSIS")
print("=" * 80)
print(f"Voted for 4 pools (25% each)")
print(f"Total rewards received: ${sum(t['value'] for t in tokens_received):.2f}\n")

# VoterV5 to query gauges
voterv5_abi = [
    {"inputs": [{"internalType": "address", "name": "", "type": "address"}], "name": "gauges", "outputs": [{"internalType": "address", "name": "", "type": "address"}], "stateMutability": "view", "type": "function"},
    {"inputs": [{"internalType": "address", "name": "", "type": "address"}], "name": "internal_bribes", "outputs": [{"internalType": "address", "name": "", "type": "address"}], "stateMutability": "view", "type": "function"},
    {"inputs": [{"internalType": "address", "name": "", "type": "address"}], "name": "external_bribes", "outputs": [{"internalType": "address", "name": "", "type": "address"}], "stateMutability": "view", "type": "function"},
]

voterv5_addr = w3.to_checksum_address("0xc69E3eF39E3fFBcE2A1c570f8d3ADF76909ef17b")
voterv5 = w3.eth.contract(address=voterv5_addr, abi=voterv5_abi)

print("MAPPING POOLS → GAUGES → BRIBE CONTRACTS")
print("=" * 80)

pool_to_gauge_map = {}

for i, pool_addr in enumerate(voted_pools, 1):
    pool_checksum = w3.to_checksum_address(pool_addr)
    
    try:
        gauge_addr = voterv5.functions.gauges(pool_checksum).call()
        internal_bribe = voterv5.functions.internal_bribes(gauge_addr).call()
        external_bribe = voterv5.functions.external_bribes(gauge_addr).call()
        
        pool_to_gauge_map[pool_addr.lower()] = {
            'gauge': gauge_addr,
            'internal': internal_bribe,
            'external': external_bribe
        }
        
        print(f"\n{i}. Pool: {pool_addr}")
        print(f"   Gauge:    {gauge_addr}")
        print(f"   Internal: {internal_bribe}")
        print(f"   External: {external_bribe}")
        
    except Exception as e:
        print(f"\n{i}. Pool: {pool_addr[:10]}... ERROR: {e}")

print("\n" + "=" * 80)
print("MATCHING BRIBE CONTRACTS TO POOLS")
print("=" * 80)

# Group tokens by bribe contract
tokens_by_bribe = {}
for token in tokens_received:
    bribe = token['from'].lower()
    if bribe not in tokens_by_bribe:
        tokens_by_bribe[bribe] = []
    tokens_by_bribe[bribe].append(token)

# Match each bribe contract to a pool
for bribe_addr, tokens in tokens_by_bribe.items():
    total_value = sum(t['value'] for t in tokens)
    token_list = ', '.join(f"{t['token']} (${t['value']:.2f})" for t in tokens)
    
    print(f"\nBribe Contract: {bribe_addr}")
    print(f"  Tokens: {token_list}")
    print(f"  Total: ${total_value:.2f}")
    
    # Check which pool this bribe contract belongs to
    matched_pool = None
    bribe_type = None
    
    for pool_addr, gauge_data in pool_to_gauge_map.items():
        if gauge_data['internal'].lower() == bribe_addr:
            matched_pool = pool_addr
            bribe_type = "INTERNAL (fees)"
            break
        elif gauge_data['external'].lower() == bribe_addr:
            matched_pool = pool_addr
            bribe_type = "EXTERNAL (bribes)"
            break
    
    if matched_pool:
        print(f"  ✓ Matched: Pool {matched_pool} ({bribe_type})")
    else:
        print(f"  ❌ No match found - this bribe contract not in your 4 voted pools!")

print("\n" + "=" * 80)
print("SUMMARY")
print("=" * 80)

rewards_by_pool = {}
for bribe_addr, tokens in tokens_by_bribe.items():
    matched_pool = None
    for pool_addr, gauge_data in pool_to_gauge_map.items():
        if gauge_data['internal'].lower() == bribe_addr or gauge_data['external'].lower() == bribe_addr:
            matched_pool = pool_addr
            break
    
    if matched_pool:
        if matched_pool not in rewards_by_pool:
            rewards_by_pool[matched_pool] = 0
        rewards_by_pool[matched_pool] += sum(t['value'] for t in tokens)

print(f"\nRewards per pool:")
for pool_addr, total_value in rewards_by_pool.items():
    pct = (total_value / sum(rewards_by_pool.values())) * 100
    print(f"  {pool_addr[:10]}... → ${total_value:.2f} ({pct:.1f}%)")

print(f"\nTotal: ${sum(rewards_by_pool.values()):.2f}")
print(f"Average per pool: ${sum(rewards_by_pool.values()) / len(voted_pools):.2f}")

db.close()
