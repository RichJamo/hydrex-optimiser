"""
Resolve pool names and token pairs from gauge addresses.

Gauges have a stakeToken() function that returns the pool address.
Pools have token0() and token1() functions that return token addresses.
Tokens have name() and symbol() functions.

This script walks this chain to identify what tokens are in each pool.
"""

from web3 import Web3
import json
import time


def resolve_pool_tokens(gauge_address: str, w3: Web3) -> dict:
    """
    Resolve pool tokens from a gauge address.
    
    Returns:
        dict with keys: gauge, pool, token0, token1, pair_name
    """
    
    # ABI for Gauge contract
    GAUGE_ABI = json.loads('''[
        {
            "name": "stakeToken",
            "inputs": [],
            "outputs": [{"type": "address"}],
            "type": "function",
            "stateMutability": "view"
        }
    ]''')

    # ABI for Pool/LP contract
    POOL_ABI = json.loads('''[
        {
            "name": "token0",
            "inputs": [],
            "outputs": [{"type": "address"}],
            "type": "function",
            "stateMutability": "view"
        },
        {
            "name": "token1",
            "inputs": [],
            "outputs": [{"type": "address"}],
            "type": "function",
            "stateMutability": "view"
        }
    ]''')

    # ABI for ERC20 token
    TOKEN_ABI = json.loads('''[
        {
            "name": "name",
            "inputs": [],
            "outputs": [{"type": "string"}],
            "type": "function",
            "stateMutability": "view"
        },
        {
            "name": "symbol",
            "inputs": [],
            "outputs": [{"type": "string"}],
            "type": "function",
            "stateMutability": "view"
        }
    ]''')
    
    result = {
        'gauge': gauge_address,
        'pool': None,
        'token0_symbol': '???',
        'token0_name': 'Unknown',
        'token1_symbol': '???',
        'token1_name': 'Unknown',
    }
    
    try:
        time.sleep(1)  # Rate limit protection
        
        # Get pool address from gauge
        gauge_contract = w3.eth.contract(
            address=Web3.to_checksum_address(gauge_address),
            abi=GAUGE_ABI
        )
        pool_addr = gauge_contract.functions.stakeToken().call()
        result['pool'] = pool_addr
        
        time.sleep(1)
        
        # Get token0 and token1 from pool
        pool_contract = w3.eth.contract(
            address=Web3.to_checksum_address(pool_addr),
            abi=POOL_ABI
        )
        token0_addr = pool_contract.functions.token0().call()
        token1_addr = pool_contract.functions.token1().call()
        
        time.sleep(1)
        
        # Get symbol from token0
        token0_contract = w3.eth.contract(
            address=Web3.to_checksum_address(token0_addr),
            abi=TOKEN_ABI
        )
        try:
            result['token0_symbol'] = token0_contract.functions.symbol().call()
        except:
            pass
        try:
            result['token0_name'] = token0_contract.functions.name().call()
        except:
            pass
        
        time.sleep(1)
        
        # Get symbol from token1
        token1_contract = w3.eth.contract(
            address=Web3.to_checksum_address(token1_addr),
            abi=TOKEN_ABI
        )
        try:
            result['token1_symbol'] = token1_contract.functions.symbol().call()
        except:
            pass
        try:
            result['token1_name'] = token1_contract.functions.name().call()
        except:
            pass
            
    except Exception as e:
        result['error'] = str(e)
        
    return result


def main():
    # Connect to Base network
    RPC_URL = "https://mainnet.base.org"
    w3 = Web3(Web3.HTTPProvider(RPC_URL))

    if not w3.is_connected():
        print("Failed to connect to Base network")
        exit(1)

    print("Connected to Base network\n")

    top_5_gauges = [
        '0x5d4a13c782502e9f21fa6e257b5b78b4d8eb9f80',
        '0xee102ec3883f1a1f1c346e317c581e0636dfce6f',
        '0x7d1bb380a7275a47603dab3b6521d5a8712dfba5',
        '0x4328ce8adc23f1c4e5a3049f63ffbdd8e73f99ce',
        '0xe76006468ec888ed1a4c4aa4d6b07dcf5a745c25'
    ]

    print("Resolving top 5 gauge pool tokens:\n")
    print("=" * 100)

    for i, gauge_addr in enumerate(top_5_gauges, 1):
        result = resolve_pool_tokens(gauge_addr, w3)
        
        print(f"\n{i}. Gauge: {result['gauge']}")
        if result.get('pool'):
            print(f"   Pool:  {result['pool']}")
            print(f"   Pair:  {result['token0_symbol']} / {result['token1_symbol']}")
            print(f"          ({result['token0_name']} / {result['token1_name']})")
        if result.get('error'):
            print(f"   Error: {result['error']}")

    print("\n" + "=" * 100)


if __name__ == "__main__":
    main()
