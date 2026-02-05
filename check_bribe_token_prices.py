#!/usr/bin/env python3
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'src'))

from database import Database, Bribe
from price_feed import PriceFeed

db = Database('data.db')
session = db.get_session()
price_feed = PriceFeed()

epoch = 1769040000

# Get bribes for your gauges
your_gauges = {
    "0x0a2918e8034737576fc9877c741f628876dcf491",
    "0x18494f88d79c3b11f7dc4c5c0a8d7725e86d4c73",
    "0x1df220b45408a11729302ec84a1443d98beccc57",
    "0x42b49967d38da5c4070336ce1cca91a802a11e8c",
    "0x6321d730b14af86e4cf81c8a01179e2c7f7a1dad",
    "0x69d66e75e6f748f784b45efd7e246b6fcf917ce7",
    "0x89ef3f3ed11c51948db6abdacba52464e0d89ccc",
    "0xcd7115cf78277bc17977fbb2177db37851f8f742",
    "0xd5a8c8f2235751136772f6436d1b87f00d603e2b",
    "0xfb5f8eee4354b65adb9fcd5454531006c39c0f71",
}

# Get all bribes
bribes = session.query(Bribe).filter(Bribe.epoch == epoch).all()

# Get prices for this epoch
unique_tokens = list(set(b.reward_token for b in bribes))
token_prices = price_feed.get_batch_prices_for_timestamp(unique_tokens, epoch)

print(f'Total unique tokens in bribes: {len(unique_tokens)}')
print(f'Tokens with prices: {len([t for t in unique_tokens if token_prices.get(t.lower(), 0) > 0])}')
print(f'Tokens WITHOUT prices: {len([t for t in unique_tokens if token_prices.get(t.lower(), 0) == 0])}')
print()

# Find which tokens are in YOUR gauges' bribes
your_gauge_tokens = {}
for bribe in bribes:
    # Check if this bribe's contract belongs to your gauges
    # We need to reverse lookup bribe_contract -> gauge
    # For now, just get tokens from your gauges' bribes
    pass

# Simpler: just look at tokens in bribes and their prices
missing_price_tokens = []
for token in unique_tokens:
    price = token_prices.get(token.lower(), 0)
    if price == 0:
        missing_price_tokens.append(token)

print(f'Sample tokens WITHOUT prices (first 10):')
for token in missing_price_tokens[:10]:
    bribes_with_token = [b for b in bribes if b.reward_token.lower() == token.lower()]
    count = len(bribes_with_token)
    sample_amount = bribes_with_token[0].amount if bribes_with_token else 0
    print(f'  {token[:10]}... - {count} bribe events, sample amount: {sample_amount}')

session.close()
