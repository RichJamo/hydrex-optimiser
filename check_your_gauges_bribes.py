#!/usr/bin/env python3
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'src'))

from database import Database
from database import Bribe, Gauge

db = Database('data.db')
session = db.get_session()

epoch = 1769040000

# Get all gauges and their bribe contracts
gauge_to_address = {}
for gauge in session.query(Gauge).all():
    contracts = []
    if gauge.internal_bribe:
        contracts.append(gauge.internal_bribe.lower())
    if gauge.external_bribe:
        contracts.append(gauge.external_bribe.lower())
    if contracts:
        gauge_to_address[gauge.address.lower()] = contracts

# Get all bribes for this epoch
bribes = session.query(Bribe).filter(Bribe.epoch == epoch).all()

# Build reverse mapping: bribe_contract -> gauge
contract_to_gauge = {}
for gauge_addr, contracts in gauge_to_address.items():
    for contract in contracts:
        contract_to_gauge[contract] = gauge_addr

# Check which gauges have bribes
gauges_with_bribes = {}
for bribe in bribes:
    gauge = contract_to_gauge.get(bribe.bribe_contract.lower())
    if gauge:
        if gauge not in gauges_with_bribes:
            gauges_with_bribes[gauge] = 0
        gauges_with_bribes[gauge] += 1

print(f'Gauges with bribes in epoch {datetime.utcfromtimestamp(epoch).strftime("%Y-%m-%d")}:')
print()
print(f'{"Gauge":<45} {"# of bribes":<12}')
print("-" * 60)
for gauge in sorted(gauges_with_bribes.keys(), key=lambda g: gauges_with_bribes[g], reverse=True)[:10]:
    print(f'{gauge:<45} {gauges_with_bribes[gauge]:<12}')

print()
print(f'Your Jan 22 gauges: ')
your_gauges = [
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
]

for short in your_gauges:
    matching = [g for g in gauges_with_bribes.keys() if g.startswith(short)]
    if matching:
        g = matching[0]
        print(f'  {short}... {g} - YES ({gauges_with_bribes[g]} bribes)')
    else:
        print(f'  {short}... - NO bribes')

session.close()
