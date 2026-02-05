#!/usr/bin/env python3
import os
import sys
from collections import defaultdict
from datetime import datetime
from dotenv import load_dotenv

sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'src'))

from config import Config
from database import Database
from price_feed import PriceFeed
from subgraph_client import SubgraphClient
from src.database import Bribe, Gauge

load_dotenv()

escrow = "0x768a675B8542F23C428C6672738E380176E7635C"
bribe_epoch_ts = 1768435200

epoch_date = datetime.utcfromtimestamp(bribe_epoch_ts).strftime('%Y-%m-%d')
print(f"Aggregating claim tokens for epoch: {epoch_date} (ts={bribe_epoch_ts})")

# Subgraph actual votes to limit gauges
client = SubgraphClient()
actual_votes_data = client.fetch_all_paginated(
    client.fetch_gauge_votes,
    epoch=bribe_epoch_ts,
    voter=escrow,
)

your_gauges = {v["gauge"]["address"].lower() for v in actual_votes_data}
print(f"Your gauges in subgraph: {len(your_gauges)}")

# DB data
db = Database('data.db')
session = db.get_session()

bribes = session.query(Bribe).filter_by(epoch=bribe_epoch_ts).all()

# Build bribe->gauge mapping
gauges = session.query(Gauge).all()
bribe_to_gauge = {}
for gauge in gauges:
    if gauge.internal_bribe:
        bribe_to_gauge[gauge.internal_bribe.lower()] = ("internal", gauge.address.lower())
    if gauge.external_bribe:
        bribe_to_gauge[gauge.external_bribe.lower()] = ("external", gauge.address.lower())

# Prices
unique_tokens = list(set(bribe.reward_token for bribe in bribes))
token_prices = PriceFeed(Config.COINGECKO_API_KEY, db).get_batch_prices_for_timestamp(
    unique_tokens, bribe_epoch_ts, granularity="hour"
)

# Aggregate per token for your gauges only
agg = {
    "internal": defaultdict(lambda: {"amount": 0.0, "usd": 0.0}),
    "external": defaultdict(lambda: {"amount": 0.0, "usd": 0.0}),
}

for bribe in bribes:
    bribe_contract_lower = bribe.bribe_contract.lower()
    mapping = bribe_to_gauge.get(bribe_contract_lower)
    if not mapping:
        continue
    bribe_type, gauge_addr = mapping
    if gauge_addr not in your_gauges:
        continue

    token_addr = bribe.reward_token.lower()
    price = token_prices.get(token_addr, 0.0)
    usd_value = bribe.amount * price

    agg[bribe_type][token_addr]["amount"] += bribe.amount
    agg[bribe_type][token_addr]["usd"] += usd_value

# Print summary
print("\nToken totals for your gauges (internal + external):")
print(f"{'Token':<12} {'Internal Amt':<18} {'Internal USD':<14} {'External Amt':<18} {'External USD':<14} {'Total USD':<12}")
print("-" * 90)

all_tokens = set(agg["internal"].keys()) | set(agg["external"].keys())
for token in sorted(all_tokens):
    ia = agg["internal"][token]["amount"]
    iu = agg["internal"][token]["usd"]
    ea = agg["external"][token]["amount"]
    eu = agg["external"][token]["usd"]
    total = iu + eu
    print(f"{token[:10]}... {ia:<18.6f} ${iu:<13.2f} {ea:<18.6f} ${eu:<13.2f} ${total:<11.2f}")

session.close()
