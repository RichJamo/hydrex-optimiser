#!/usr/bin/env python3
import sys
sys.path.insert(0, 'src')

from database import Database
from src.database import Epoch
from datetime import datetime

db = Database('hydrex.db')
session = db.get_session()

epochs = session.query(Epoch).order_by(Epoch.timestamp.desc()).limit(20).all()

print("Latest 20 epochs in database:")
print(f"{'Date':<12} {'Timestamp':<15}")
print("-" * 30)
for epoch in epochs:
    date = datetime.utcfromtimestamp(epoch.timestamp).strftime('%Y-%m-%d')
    print(f"{date:<12} {epoch.timestamp:<15}")

session.close()
