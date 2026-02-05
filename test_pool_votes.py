#!/usr/bin/env python3
import os
import requests
from web3 import Web3
from dotenv import load_dotenv

load_dotenv()

w3 = Web3(Web3.HTTPProvider('https://base.llamarpc.com'))

# Test with a gauge from our database that we know exists
gauge_addr = '0x4328ce8adc23f1c4e5a3049f63ffbdd8e73f99ce'
gauge_addr = Web3.to_checksum_address(gauge_addr)
print(f'Testing gauge: {gauge_addr}')

# Get the code at this address to see if there's a contract
code = w3.eth.get_code(gauge_addr)
print(f'Code length: {len(code)} bytes')
if len(code) == 0:
    print('No contract at this address!')
else:
    print('Contract exists')

# Try calling stakeToken() to get pool
GAUGE_ABI = [
    {
        'inputs': [],
        'name': 'stakeToken',
        'outputs': [{'internalType': 'address', 'name': '', 'type': 'address'}],
        'stateMutability': 'view',
        'type': 'function'
    }
]

try:
    gauge_contract = w3.eth.contract(
        address=Web3.to_checksum_address(gauge_addr),
        abi=GAUGE_ABI
    )
    pool = gauge_contract.functions.stakeToken().call()
    print(f'Pool address: {pool}')
except Exception as e:
    print(f'Error calling stakeToken(): {e}')
    
    # Try a different approach - check what's actually on the chain
    print('\nTrying to get pool from database instead...')
    from src.database import Database
    db = Database('data.db')
    gauge = db.get_gauge(gauge_addr)
    if gauge and gauge.pool:
        print(f'Pool from DB: {gauge.pool}')
    else:
        print('No pool in database either')

# Query the subgraph to see what gauges are voting
subgraph_url = os.getenv('SUBGRAPH_URL')
query = '''
{
  gaugeVotes(first: 5, where: {epoch: 1769040000}) {
    gauge {
      address
    }
    weight
  }
}
'''

print(f'\nQuerying subgraph for sample votes...')
resp = requests.post(subgraph_url, json={'query': query}, timeout=30)
result = resp.json()

if 'data' in result and result['data'].get('gaugeVotes'):
    votes = result['data']['gaugeVotes']
    print(f'\nFound {len(votes)} sample votes')
    for v in votes[:3]:
        gauge_from_subgraph = v["gauge"]["address"]
        print(f'  Gauge (from subgraph): {gauge_from_subgraph}')
        # Check if this gauge is in our database
        from src.database import Database
        db = Database('data.db')
        gauge_obj = db.get_gauge(gauge_from_subgraph)
        if gauge_obj and gauge_obj.pool:
            print(f'    → Pool in DB: {gauge_obj.pool}')
        else:
            print(f'    → Not in database or no pool')
else:
    print(f'Error: {result}')

