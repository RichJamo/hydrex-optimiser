#!/usr/bin/env python3
import os
import sys
from datetime import datetime
from dotenv import load_dotenv

sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'src'))

from database import Database
from database import Bribe

load_dotenv()

db = Database('data.db')
session = db.get_session()

# Jan 22 epoch (where you voted on Jan 28)
target_epoch = 1769040000

bribes = session.query(Bribe).filter(Bribe.epoch == target_epoch).all()

print(f"Bribes in epoch {datetime.utcfromtimestamp(target_epoch).strftime('%Y-%m-%d')} (ts={target_epoch}):")
print(f"Total bribe events: {len(bribes)}")
print()

if bribes:
    # Group by bribe contract
    by_contract = {}
    for bribe in bribes:
        if bribe.bribe_contract not in by_contract:
            by_contract[bribe.bribe_contract] = []
        by_contract[bribe.bribe_contract].append(bribe)
    
    print(f"Number of unique bribe contracts: {len(by_contract)}")
    print()
    print("Sample bribe events:")
    for i, (contract, events) in enumerate(list(by_contract.items())[:5]):
        print(f"  {contract[:10]}... - {len(events)} events")
else:
    print("✗ NO bribes found in this epoch!")

session.close()
