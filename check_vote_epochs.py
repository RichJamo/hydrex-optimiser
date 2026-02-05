#!/usr/bin/env python3
import sys
sys.path.insert(0, 'src')

from database import Database
from src.database import Vote
from datetime import datetime

db = Database('data.db')
session = db.get_session()

bribe_epoch_ts = 1768435200
prev_epoch_ts = bribe_epoch_ts - 604800  # 1 week earlier
next_epoch_ts = bribe_epoch_ts + 604800  # 1 week later

bribe_date = datetime.utcfromtimestamp(bribe_epoch_ts).strftime('%Y-%m-%d')
prev_date = datetime.utcfromtimestamp(prev_epoch_ts).strftime('%Y-%m-%d')
next_date = datetime.utcfromtimestamp(next_epoch_ts).strftime('%Y-%m-%d')

print(f"Checking Vote table for votes around epoch {bribe_date}")
print()

# Check votes in previous epoch
votes_prev = session.query(Vote).filter_by(epoch=prev_epoch_ts).all()
print(f"{prev_date} (epoch {prev_epoch_ts}): {len(votes_prev)} vote records")
if votes_prev:
    total_votes_prev = sum(v.total_votes for v in votes_prev)
    print(f"  Total votes: {total_votes_prev:,.0f}")

# Check votes in current epoch
votes_current = session.query(Vote).filter_by(epoch=bribe_epoch_ts).all()
print(f"{bribe_date} (epoch {bribe_epoch_ts}): {len(votes_current)} vote records")
if votes_current:
    total_votes_current = sum(v.total_votes for v in votes_current)
    print(f"  Total votes: {total_votes_current:,.0f}")

# Check votes in next epoch
votes_next = session.query(Vote).filter_by(epoch=next_epoch_ts).all()
print(f"{next_date} (epoch {next_epoch_ts}): {len(votes_next)} vote records")
if votes_next:
    total_votes_next = sum(v.total_votes for v in votes_next)
    print(f"  Total votes: {total_votes_next:,.0f}")

session.close()
