#!/usr/bin/env python3
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'src'))

from database import Database
from database import Bribe, Gauge

db = Database('data.db')
session = db.get_session()

# Your gauges from Jan 22 vote
your_jan22_gauges = {
    "0x0a2918e8",
    "0x18494f88",
    "0x1df220b4",
    "0x42b49967",
    "0x6321d730",
    "0x69d66e75",
    "0x89ef3f3e",
    "0xcd7115cf",
    "0xd5a8c8f2",
    "0xfb5f8eee",
}

epoch = 1769040000

# Get all gauges and their bribe contracts
gauge_to_bribe_contracts = {}
for gauge in session.query(Gauge).all():
    if gauge.internal_bribe or gauge.external_bribe:
        contracts = []
        if gauge.internal_bribe:
            contracts.append(('internal', gauge.internal_bribe.lower()))
        if gauge.external_bribe:
            contracts.append(('external', gauge.external_bribe.lower()))
        gauge_to_bribe_contracts[gauge.address.lower()] = contracts

# Check for bribes
bribes = session.query(Bribe).filter(Bribe.epoch == epoch).all()

print(f'Total bribes in epoch {datetime.utcfromtimestamp(epoch).strftime("%Y-%m-%d")} (ts={epoch}): {len(bribes)}')
print()

if bribes:
    # Check if any match your gauges
    bribe_contracts_in_epoch = set(b.bribe_contract.lower() for b in bribes)
    
    print(f'Unique bribe contracts with activity: {len(bribe_contracts_in_epoch)}')
    print()
    
    # Find which gauges have bribes
    gauges_with_bribes = []
    for gauge_addr, contracts in gauge_to_bribe_contracts.items():
        for bribe_type, contract in contracts:
            if contract in bribe_contracts_in_epoch:
                gauges_with_bribes.append(gauge_addr)
                break
    
    print(f'Gauges with bribes in this epoch: {len(gauges_with_bribes)}')
    if gauges_with_bribes:
        for g in gauges_with_bribes[:5]:
            print(f'  {g[:10]}...')
    
    # Sample bribes
    print()
    print('Sample bribes:')
    for b in bribes[:3]:
        print(f'  {b.bribe_contract[:10]}... token={b.reward_token[:10]}... amount={b.amount}')
else:
    print('NO BRIBES in database for this epoch!')

session.close()
