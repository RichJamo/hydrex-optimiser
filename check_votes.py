#!/usr/bin/env python3
from src.database import Database, Vote

db = Database('data.db')
session = db.get_session()

# Your gauges for Jan 22 epoch
your_gauges = {
    '0x0a2918e8d6dc5f8d0a39d48c5be33c48a46b9c00': 117761.26,
    '0x18494f88d49eea03e0ee83fb2c17b0dbf2ff33e3': 117761.26,
    '0x1df220b4bee82063c2d4bfefe22ac4f44b4b8a40': 117761.26,
    '0x42b49967d38da569debb92c59e1f49e8ad79f61b': 117761.26,
    '0x6321d730afb14dd41f8ea33097c0ad8f7d87ae3a': 117761.26,
    '0x69d66e75f9b4f71fc30b6f4f2ad1f3e88cad8062': 117761.26,
    '0x89ef3f3ed11c5184beb4ae45c76e559ee99d6c8e': 117761.26,
    '0xcd7115cf80ca8b20b63a9056c7cb6f6bfe44b2c8': 117761.26,
    '0xd5a8c8f21bdeb4dd5a5c76b13d9c3a3c1c25e2e2': 117761.26,
    '0xfb5f8eee53bbd2f27d96bc1e3c2fd28e4e81ec91': 117761.26,
}

print('Checking Vote table for your gauges:')
print()
for gauge, your_votes in your_gauges.items():
    vote_record = session.query(Vote).filter_by(epoch=1769040000, gauge=gauge).first()
    if vote_record:
        share = (your_votes / vote_record.total_votes * 100) if vote_record.total_votes > 0 else 0
        print(f'✓ {gauge[:16]}... DB total: {vote_record.total_votes:>12,.2f} | Your share: {share:>6.2f}%')
    else:
        print(f'✗ {gauge[:16]}... MISSING - Using bribe submitter votes as proxy')
