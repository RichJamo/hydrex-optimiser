#!/usr/bin/env python3
"""
Query pool contracts to find what token pairs the mystery pools are.
"""

from web3 import Web3
import json

w3 = Web3(Web3.HTTPProvider('https://base-mainnet.g.alchemy.com/v2/oFfvEpXYjGo8Nj4QQIkU3kXd6Z0JvfJZ'))

# Standard pool ABI for token0/token1/symbol
POOL_ABI = json.loads('''[
    {"inputs":[],"name":"token0","outputs":[{"internalType":"address","name":"","type":"address"}],"stateMutability":"view","type":"function"},
    {"inputs":[],"name":"token1","outputs":[{"internalType":"address","name":"","type":"address"}],"stateMutability":"view","type":"function"},
    {"inputs":[],"name":"symbol","outputs":[{"internalType":"string","name":"","type":"string"}],"stateMutability":"view","type":"function"},
    {"inputs":[],"name":"stable","outputs":[{"internalType":"bool","name":"","type":"bool"}],"stateMutability":"view","type":"function"}
]''')

# ERC20 ABI for symbol
ERC20_ABI = json.loads('''[
    {"inputs":[],"name":"symbol","outputs":[{"internalType":"string","name":"","type":"string"}],"stateMutability":"view","type":"function"}
]''')

mystery_pools = [
    ("0x3f9b863EF4B295d6Ba370215bcCa3785FCC44f44", 246.75),
    ("0x0BA69825c4C033e72309F6AC0Bde0023b15Cc97c", 236.11),
    ("0xEf96Ec76eEB36584FC4922e9fA268e0780170f33", 245.80),
    ("0x680581725840958141Bb328666D8Fc185aC4FA49", 227.67),
]

voted_pools = [
    "0x19FF35059452Faa793DdDF9894a1571c5D41003e",
    "0xe62a34Ae5e0B9FdE3501Aeb72DC9585Bb3B72A7e",
    "0xF19787f048b3401546aa7A979afa79D555C114Dd",
    "0xE539b14a87D3Db4a2945ac99b29A69DE61531592",
]

print("\nMYSTERY POOLS (that paid rewards)")
print("=" * 80)

for pool_addr, reward_value in mystery_pools:
    print(f"\n{pool_addr} (${reward_value:.2f})")
    try:
        pool = w3.eth.contract(address=Web3.to_checksum_address(pool_addr), abi=POOL_ABI)
        
        token0_addr = pool.functions.token0().call()
        token1_addr = pool.functions.token1().call()
        
        token0 = w3.eth.contract(address=token0_addr, abi=ERC20_ABI)
        token1 = w3.eth.contract(address=token1_addr, abi=ERC20_ABI)
        
        symbol0 = token0.functions.symbol().call()
        symbol1 = token1.functions.symbol().call()
        
        try:
            stable = pool.functions.stable().call()
            pool_type = "sAMM" if stable else "vAMM"
        except:
            pool_type = "Unknown"
        
        print(f"  {symbol0}/{symbol1} ({pool_type})")
        
    except Exception as e:
        print(f"  Error: {e}")

print("\n" + "=" * 80)
print("YOUR VOTED POOLS")
print("=" * 80)

for pool_addr in voted_pools:
    print(f"\n{pool_addr}")
    try:
        pool = w3.eth.contract(address=Web3.to_checksum_address(pool_addr), abi=POOL_ABI)
        
        token0_addr = pool.functions.token0().call()
        token1_addr = pool.functions.token1().call()
        
        token0 = w3.eth.contract(address=token0_addr, abi=ERC20_ABI)
        token1 = w3.eth.contract(address=token1_addr, abi=ERC20_ABI)
        
        symbol0 = token0.functions.symbol().call()
        symbol1 = token1.functions.symbol().call()
        
        try:
            stable = pool.functions.stable().call()
            pool_type = "sAMM" if stable else "vAMM"
        except:
            pool_type = "Unknown"
        
        print(f"  {symbol0}/{symbol1} ({pool_type})")
        
    except Exception as e:
        print(f"  Error: {e}")
