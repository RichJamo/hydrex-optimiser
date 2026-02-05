#!/usr/bin/env python3
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'src'))

from database import Database
from database import Bribe

db = Database('data.db')
session = db.get_session()

epoch = 1769040000
bribes = session.query(Bribe).filter(Bribe.epoch == epoch).all()

print(f'Bribes in epoch {datetime.utcfromtimestamp(epoch).strftime("%Y-%m-%d")} (ts={epoch}):')
print(f'Total: {len(bribes)}')

if bribes:
    print(f'Sample (first 5):')
    for b in bribes[:5]:
        print(f'  {b.bribe_contract[:10]}... {b.reward_token[:10]}... amount={b.amount}')
else:
    print('NO bribes in database for this epoch')

session.close()
