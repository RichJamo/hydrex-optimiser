#!/usr/bin/env python3
import sys
sys.path.insert(0, 'src')

from database import Database
from src.database import Epoch, Bribe, Gauge
from datetime import datetime

db = Database('data.db')
session = db.get_session()

bribe_epoch_ts = 1768435200
epoch_date = datetime.utcfromtimestamp(bribe_epoch_ts).strftime('%Y-%m-%d')

print(f"Analyzing bribes for epoch: {epoch_date} (ts={bribe_epoch_ts})")
print()

# Get all bribes
bribes = session.query(Bribe).filter_by(epoch=bribe_epoch_ts).all()
print(f"Total bribe events in database: {len(bribes)}")
print()

# Build bribe->gauge mapping
gauges = session.query(Gauge).all()
bribe_to_gauge = {}
internal_count = 0
external_count = 0

for gauge in gauges:
    if gauge.internal_bribe:
        bribe_to_gauge[gauge.internal_bribe.lower()] = ('internal', gauge.address.lower())
        internal_count += 1
    if gauge.external_bribe:
        bribe_to_gauge[gauge.external_bribe.lower()] = ('external', gauge.address.lower())
        external_count += 1

print(f"Bribe contract mappings:")
print(f"  Internal bribes: {internal_count}")
print(f"  External bribes: {external_count}")
print(f"  Total mappings: {len(bribe_to_gauge)}")
print()

# Categorize bribes by whether they map to a gauge
mapped_bribes = 0
unmapped_bribes = 0
mapped_internal = 0
mapped_external = 0

for bribe in bribes:
    bribe_contract_lower = bribe.bribe_contract.lower()
    if bribe_contract_lower in bribe_to_gauge:
        mapped_bribes += 1
        bribe_type, _ = bribe_to_gauge[bribe_contract_lower]
        if bribe_type == 'internal':
            mapped_internal += 1
        else:
            mapped_external += 1
    else:
        unmapped_bribes += 1

print(f"Bribe event categorization:")
print(f"  Mapped to gauges: {mapped_bribes}")
print(f"    - Internal: {mapped_internal}")
print(f"    - External: {mapped_external}")
print(f"  Unmapped: {unmapped_bribes}")
print()

if unmapped_bribes > 0:
    print(f"Sample of unmapped bribe contracts:")
    unmapped = [b.bribe_contract for b in bribes if b.bribe_contract.lower() not in bribe_to_gauge][:5]
    for addr in unmapped:
        print(f"  {addr}")

session.close()
