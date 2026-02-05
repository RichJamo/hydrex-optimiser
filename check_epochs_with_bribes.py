#!/usr/bin/env python3
import os
import sys
from datetime import datetime
from dotenv import load_dotenv

sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'src'))

from database import Database
from src.database import Epoch, Bribe

load_dotenv()

db = Database('hydrex.db')
session = db.get_session()

# Get all epochs with bribes, sorted by timestamp
epochs_with_bribes = (
    session.query(Epoch)
    .join(Bribe, Epoch.id == Bribe.epoch_id)
    .distinct()
    .order_by(Epoch.timestamp)
    .all()
)

print("Epochs with bribes:")
for epoch in epochs_with_bribes:
    date = datetime.utcfromtimestamp(epoch.timestamp).strftime('%Y-%m-%d')
    bribe_count = session.query(Bribe).filter_by(epoch_id=epoch.id).count()
    print(f"  {date} (ts={epoch.timestamp}): {bribe_count} bribes")

session.close()
