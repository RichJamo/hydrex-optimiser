#!/usr/bin/env python3
"""
Query the gauge contract directly to verify it exists and get its pool.
"""

from web3 import Web3
import json

# Your known gauge
KNOWN_GAUGE = "0x632f2D41Ba9e6E80035D578DDD48b019e4403F86"

print("QUERYING GAUGE CONTRACT DIRECTLY")
print("=" * 80)

w3 = Web3(Web3.HTTPProvider("https://base-mainnet.g.alchemy.com/v2/oFfvEpXYjGo8Nj4QQIkU3kXd6Z0JvfJZ"))
print(f"Connected: {w3.is_connected()}")

# Minimal Gauge ABI to get stakeToken
GAUGE_ABI = json.loads('''[
    {"inputs":[],"name":"stakeToken","outputs":[{"internalType":"address","name":"","type":"address"}],"stateMutability":"view","type":"function"},
    {"inputs":[],"name":"internal_bribe","outputs":[{"internalType":"address","name":"","type":"address"}],"stateMutability":"view","type":"function"},
    {"inputs":[],"name":"external_bribe","outputs":[{"internalType":"address","name":"","type":"address"}],"stateMutability":"view","type":"function"}
]''')

gauge_checksum = Web3.to_checksum_address(KNOWN_GAUGE)

print(f"\nGauge: {KNOWN_GAUGE}")
print("-" * 80)

try:
    # Check if contract exists
    code = w3.eth.get_code(gauge_checksum)
    if code == b'' or code == b'0x':
        print("❌ No contract code at this address!")
    else:
        print(f"✓ Contract exists (code size: {len(code)} bytes)")
        
        gauge = w3.eth.contract(address=gauge_checksum, abi=GAUGE_ABI)
        
        # Try to get stakeToken
        try:
            stake_token = gauge.functions.stakeToken().call()
            print(f"\nstakeToken(): {stake_token}")
        except Exception as e:
            print(f"\n❌ stakeToken() failed: {e}")
        
        # Try to get internal_bribe
        try:
            internal = gauge.functions.internal_bribe().call()
            print(f"internal_bribe(): {internal}")
        except Exception as e:
            print(f"❌ internal_bribe() failed: {e}")
        
        # Try to get external_bribe
        try:
            external = gauge.functions.external_bribe().call()
            print(f"external_bribe(): {external}")
        except Exception as e:
            print(f"❌ external_bribe() failed: {e}")
            
except Exception as e:
    print(f"❌ Error: {e}")

# Also check if this is the correct VoterV5
print("\n" + "=" * 80)
print("CHECKING VOTERV5 CONTRACT")
print("=" * 80)

VOTER_V5_ADDRESS = '0xc69E3eF39E3fFBcE2A1c570f8d3ADF76909ef17b'

VOTER_V5_ABI = json.loads('''[
    {"inputs":[],"name":"length","outputs":[{"internalType":"uint256","name":"","type":"uint256"}],"stateMutability":"view","type":"function"}
]''')

voterv5_checksum = Web3.to_checksum_address(VOTER_V5_ADDRESS)
voterv5 = w3.eth.contract(address=voterv5_checksum, abi=VOTER_V5_ABI)

try:
    pool_count = voterv5.functions.length().call()
    print(f"VoterV5 address: {VOTER_V5_ADDRESS}")
    print(f"Total pools: {pool_count}")
    print("✓ VoterV5 contract is responding")
except Exception as e:
    print(f"❌ VoterV5.length() failed: {e}")
    print("This might be the wrong VoterV5 contract!")
