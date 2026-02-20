#!/usr/bin/env python3
"""
Simple comparison of mystery pools vs voted pools
"""

from web3 import Web3
import json

w3 = Web3(Web3.HTTPProvider('https://base-mainnet.g.alchemy.com/v2/oFfvEpXYjGo8Nj4QQIkU3kXd6Z0JvfJZ'))

POOL_ABI = json.loads('''[
    {"inputs":[],"name":"token0","outputs":[{"internalType":"address","name":"","type":"address"}],"stateMutability":"view","type":"function"},
    {"inputs":[],"name":"token1","outputs":[{"internalType":"address","name":"","type":"address"}],"stateMutability":"view","type":"function"}
]''')

ERC20_ABI = json.loads('''[
    {"inputs":[],"name":"symbol","outputs":[{"internalType":"string","name":"","type":"string"}],"stateMutability":"view","type":"function"}
]''')

def get_pool_tokens(pool_addr):
    try:
        pool = w3.eth.contract(address=Web3.to_checksum_address(pool_addr), abi=POOL_ABI)
        token0_addr = pool.functions.token0().call()
        token1_addr = pool.functions.token1().call()
        
        token0 = w3.eth.contract(address=token0_addr, abi=ERC20_ABI)
        token1 = w3.eth.contract(address=token1_addr, abi=ERC20_ABI)
        
        symbol0 = token0.functions.symbol().call()
        symbol1 = token1.functions.symbol().call()
        
        return f"{symbol0}/{symbol1}"
    except Exception as e:
        return f"Error: {e}"

mystery_pools = [
    "0x3f9b863EF4B295d6Ba370215bcCa3785FCC44f44",
    "0x0BA69825c4C033e72309F6AC0Bde0023b15Cc97c",
    "0xEf96Ec76eEB36584FC4922e9fA268e0780170f33",
    "0x680581725840958141Bb328666D8Fc185aC4FA49",
]

voted_pools = [
    "0x19FF35059452Faa793DdDF9894a1571c5D41003e",
    "0xe62a34Ae5e0B9FdE3501Aeb72DC9585Bb3B72A7e",
    "0xF19787f048b3401546aa7A979afa79D555C114Dd",
    "0xE539b14a87D3Db4a2945ac99b29A69DE61531592",
]

print("\nPOOLS THAT PAID REWARDS (Mystery Pools)")
print("=" * 80)
for addr in mystery_pools:
    tokens = get_pool_tokens(addr)
    print(f"{addr}  →  {tokens}")

print("\n\nPOOLS YOU VOTED FOR")
print("=" * 80)
for addr in voted_pools:
    tokens = get_pool_tokens(addr)
    print(f"{addr}  →  {tokens}")

print("\n\nDIRECT COMPARISON")
print("=" * 80)
print("Do any addresses match?")
for voted in voted_pools:
    if voted.lower() in [m.lower() for m in mystery_pools]:
        print(f"  ✓ MATCH: {voted}")
        
if not any(voted.lower() in [m.lower() for m in mystery_pools] for voted in voted_pools):
    print("  ❌ NO MATCHES - These are completely different pools!")
