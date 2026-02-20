#!/usr/bin/env python3
"""
Verify gauge -> pool mapping by calling stakeToken() on gauge contracts.
The gauge's stakeToken is the actual pool (LP token) address.
"""

from web3 import Web3

# Connect to Base via Alchemy
w3 = Web3(Web3.HTTPProvider('https://base-mainnet.g.alchemy.com/v2/oFfvEpXYjGo8Nj4QQIkU3kXd6Z0JvfJZ'))

# Minimal Gauge ABI - just need stakeToken
GAUGE_ABI = [
    {
        "inputs": [],
        "name": "stakeToken",
        "outputs": [{"internalType": "address", "name": "", "type": "address"}],
        "stateMutability": "view",
        "type": "function"
    }
]

# Test with the user's voted gauge
test_gauge = "0x632f2D41Ba9e6E80035D578DDD48b019e4403F86"
print(f"Testing Gauge: {test_gauge}")
print("=" * 80)

gauge_contract = w3.eth.contract(
    address=Web3.to_checksum_address(test_gauge), 
    abi=GAUGE_ABI
)

try:
    pool_address = gauge_contract.functions.stakeToken().call()
    print(f"✓ Pool Address (stakeToken): {pool_address}")
    print(f"\nCompare to expected: 0x19FF35059452Faa793DdDF9894a1571c5D41003e")
    
    if pool_address.lower() == "0x19FF35059452Faa793DdDF9894a1571c5D41003e".lower():
        print("✓ MATCH! This is the correct pool address.")
    else:
        print("❌ MISMATCH!")
        
except Exception as e:
    print(f"❌ Error calling stakeToken(): {e}")

print("\n" + "=" * 80)
print("Now checking the mystery gauges:")
print("=" * 80)

mystery_gauges = [
    ("0xee5f8bf7cdb1ad421993a368b15d06ad58122dab", "$246.75"),
    ("0xe63cd99406e98d909ab6d702b11dd4cd31a425a2", "$236.11"),
    ("0xdc470dc0b3247058ea4605dba6e48a9b2a083971", "$245.80"),
    ("0x1df220b45408a11729302ec84a1443d98beccc57", "$227.67"),
]

for gauge_addr, reward_value in mystery_gauges:
    print(f"\nGauge: {gauge_addr} (paid {reward_value})")
    
    gauge_contract = w3.eth.contract(
        address=Web3.to_checksum_address(gauge_addr), 
        abi=GAUGE_ABI
    )
    
    try:
        pool_address = gauge_contract.functions.stakeToken().call()
        print(f"  Actual Pool: {pool_address}")
        
        if pool_address.lower() == gauge_addr.lower():
            print(f"  ⚠️  Pool == Gauge (this would be unusual)")
        else:
            print(f"  ✓ Pool != Gauge (correct)")
            
    except Exception as e:
        print(f"  ❌ Error: {e}")
