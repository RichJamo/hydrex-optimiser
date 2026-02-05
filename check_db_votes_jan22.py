#!/usr/bin/env python3
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'src'))

from database import Database, Vote

db = Database('data.db')
session = db.get_session()

epoch = 1769040000

# Your Jan 22 gauges
your_gauges = [
    "0x0a2918e8034737576fc9877c741f628876dcf491",
    "0x1df220b4",  # These are just the short versions from the output
]

# Check if these gauges have vote records in the database for this epoch
print(f"Checking database votes for epoch {epoch}:")
print()

for gauge in your_gauges:
    votes = session.query(Vote).filter_by(epoch=epoch, gauge=gauge).all()
    if votes:
        total = sum(v.total_votes for v in votes)
        print(f"{gauge[:10]}... - {len(votes)} record(s), total votes: {total:,.2f}")
    else:
        print(f"{gauge[:10]}... - NO VOTES IN DATABASE")

# Also check what gauges DO have votes in this epoch
print()
print("All gauges with votes in epoch 1769040000:")
all_votes = session.query(Vote).filter_by(epoch=epoch).all()
print(f"Total vote records: {len(all_votes)}")

gauges_in_epoch = {}
for v in all_votes:
    if v.gauge not in gauges_in_epoch:
        gauges_in_epoch[v.gauge] = 0
    gauges_in_epoch[v.gauge] += v.total_votes

print(f"Unique gauges: {len(gauges_in_epoch)}")

session.close()
