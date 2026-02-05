#!/usr/bin/env python3
import sys
sys.path.insert(0, 'src')
from database import Database
from src.database import Epoch, Bribe, Vote
from datetime import datetime

db = Database('hydrex.db')
session = db.get_session()

# Target epoch
ts = 1768435200
epoch = session.query(Epoch).filter_by(timestamp=ts).first()

if epoch:
    print(f"Epoch {ts} (2026-01-22):")
    vote_count = session.query(Vote).filter_by(epoch_id=epoch.id).count()
    bribe_count = session.query(Bribe).filter_by(epoch_id=epoch.id).count()
    print(f"  Votes: {vote_count}")
    print(f"  Bribes: {bribe_count}")
    if bribe_count > 0:
        print(f"  ✓ Has data!")
else:
    print(f"Epoch {ts} not found")
    # List all epochs
    epochs = session.query(Epoch).order_by(Epoch.timestamp).all()
    print(f"\nAvailable epochs:")
    for e in epochs[-10:]:
        date = datetime.utcfromtimestamp(e.timestamp).strftime('%Y-%m-%d')
        v = session.query(Vote).filter_by(epoch_id=e.id).count()
        b = session.query(Bribe).filter_by(epoch_id=e.id).count()
        print(f"  {date} (ts={e.timestamp}): {v} votes, {b} bribes")

session.close()
