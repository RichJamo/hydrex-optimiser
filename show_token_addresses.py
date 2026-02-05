#!/usr/bin/env python3
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'src'))

from database import Database, Bribe

db = Database('data.db')
session = db.get_session()

epoch = 1769040000

# Get unique tokens
bribes = session.query(Bribe).filter(Bribe.epoch == epoch).all()
unique_tokens = list(set(b.reward_token for b in bribes))

print(f'Sample of tokens without prices (full addresses):')
print()
for token in unique_tokens[:5]:
    print(token)
    bribes_with_token = [b for b in bribes if b.reward_token.lower() == token.lower()]
    print(f'  → {len(bribes_with_token)} bribe events')
    print()

session.close()
