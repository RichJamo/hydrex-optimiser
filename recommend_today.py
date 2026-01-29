#!/usr/bin/env python3
"""Generate voting recommendation based on latest epoch data."""

import logging
from datetime import datetime
from src.database import Database
from src.price_feed import PriceFeed
from src.optimizer import VoteOptimizer
from config import Config

# Set up logging
logging.basicConfig(
    level=logging.INFO,
    format='%(name)s - %(levelname)s - %(message)s'
)

logger = logging.getLogger(__name__)

# Initialize
db = Database(Config.DATABASE_PATH)
price_feed = PriceFeed(Config.COINGECKO_API_KEY, db)
optimizer = VoteOptimizer(Config.YOUR_VOTING_POWER)

# Get most recent epoch
latest_epoch = db.get_recent_epochs(1)[0]
logger.info(f"Latest epoch in DB: {latest_epoch.timestamp} ({datetime.fromtimestamp(latest_epoch.timestamp).strftime('%Y-%m-%d')})")

# Fetch votes and bribes for latest epoch
votes = db.get_votes_for_epoch(latest_epoch.timestamp)
bribes = db.get_bribes_for_epoch(latest_epoch.timestamp)

logger.info(f"Found {len(votes)} votes and {len(bribes)} bribes")

# Build gauge data
gauge_data = {}
for vote in votes:
    gauge_data[vote.gauge] = {
        "address": vote.gauge,
        "current_votes": vote.total_votes,
        "bribes_usd": 0.0,
        "internal_bribes_usd": 0.0,
        "external_bribes_usd": 0.0,
    }

# Build bribe mapping
bribe_to_gauge = {}
bribe_type = {}
for gauge in db.get_all_gauges():
    if gauge.internal_bribe and gauge.internal_bribe.lower() != "0x0000000000000000000000000000000000000000":
        bribe_to_gauge[gauge.internal_bribe.lower()] = gauge.address
        bribe_type[gauge.internal_bribe.lower()] = "internal"
    if gauge.external_bribe and gauge.external_bribe.lower() != "0x0000000000000000000000000000000000000000":
        bribe_to_gauge[gauge.external_bribe.lower()] = gauge.address
        bribe_type[gauge.external_bribe.lower()] = "external"
    if gauge.address not in gauge_data:
        gauge_data[gauge.address] = {
            "address": gauge.address,
            "current_votes": 0,
            "bribes_usd": 0.0,
            "internal_bribes_usd": 0.0,
            "external_bribes_usd": 0.0,
        }

# Fetch prices and map bribes
unique_tokens = list(set(bribe.reward_token for bribe in bribes))
logger.info(f"Fetching prices for {len(unique_tokens)} tokens...")
token_prices = price_feed.get_batch_prices(unique_tokens)

for bribe in bribes:
    gauge_addr = bribe_to_gauge.get(bribe.bribe_contract.lower())
    if gauge_addr and gauge_addr in gauge_data:
        token_addr = bribe.reward_token.lower()
        price = token_prices.get(token_addr, 0.0)
        usd_value = bribe.amount * price
        
        gauge_data[gauge_addr]["bribes_usd"] += usd_value
        if bribe_type.get(bribe.bribe_contract.lower()) == "internal":
            gauge_data[gauge_addr]["internal_bribes_usd"] += usd_value
        else:
            gauge_data[gauge_addr]["external_bribes_usd"] += usd_value

# Filter and optimize
gauge_list = [g for g in gauge_data.values() if g["bribes_usd"] > 0]
gauge_list = sorted(gauge_list, key=lambda x: x["bribes_usd"], reverse=True)

if gauge_list:
    comparison = optimizer.compare_strategies(gauge_list)
    
    print(f"\n{'='*80}")
    print(f"VOTING RECOMMENDATION FOR UPCOMING EPOCH")
    print(f"Based on latest available data: {datetime.fromtimestamp(latest_epoch.timestamp).strftime('%Y-%m-%d')}")
    print(f"{'='*80}\n")
    
    print(f"Expected Return: ${comparison['optimal']['return']:,.2f}")
    print(f"(vs ~${comparison['naive']['return']:,.2f} with naive equal-split strategy)\n")
    
    print(f"RECOMMENDED VOTE ALLOCATION (Top Gauges):")
    print(f"{'Gauge Address':<12} {'Your Votes':<15} {'Your Share':<12} {'Total Bribes':<15} {'Expected $':<15}")
    print(f"{'-'*70}")
    
    sorted_alloc = sorted(comparison["optimal"]["allocation"].items(), key=lambda x: x[1], reverse=True)
    total_allocated = 0
    for addr, votes_allocated in sorted_alloc:
        gauge = next((g for g in gauge_list if g["address"] == addr), None)
        if gauge:
            share = votes_allocated / (gauge["current_votes"] + votes_allocated)
            expected = gauge["bribes_usd"] * share
            total_allocated += votes_allocated
            
            # Format address (shorter for display)
            short_addr = f"{addr[:6]}...{addr[-4:]}"
            print(f"{short_addr:<12} {votes_allocated:>13,} {share:>10.2%} ${gauge['bribes_usd']:>13,.2f} ${expected:>13,.2f}")
    
    print(f"{'-'*70}")
    print(f"{'TOTAL ALLOCATED':<12} {total_allocated:>13,}")
    print(f"\n{'='*80}\n")
else:
    logger.error("No gauges with bribes found")

