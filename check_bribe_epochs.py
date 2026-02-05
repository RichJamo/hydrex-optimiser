#!/usr/bin/env python3
import os
import sys
from datetime import datetime
from dotenv import load_dotenv

sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'src'))

from config import Config
from database import Database

load_dotenv()

db = Database('hydrex.db')
session = db.get_session()

# Get all epochs with bribes
from src.database import Epoch, Bribe
epochs_with_bribes = session.query(Epoch).join(Bribe).distinct().all()

print("Epochs with bribes:")
for epoch in sorted(epochs_with_bribes, key=lambda e: e.timestamp):
    date = datetime.utcfromtimestamp(epoch.timestamp).strftime('%Y-%m-%d')
    bribe_count = session.query(Bribe).filter_by(epoch_id=epoch.id).count()
    print(f"  {date} (ts={epoch.timestamp}): {bribe_count} bribes")

session.close()
