#!/usr/bin/env python3
"""
Smart voting recommendation engine.
Considers historical performance + current epoch data to recommend optimal votes.
Can be run up until just before epoch flip for real-time recommendations.
"""

import logging
from datetime import datetime
import sys

from src.database import Database
from src.price_feed import PriceFeed
from src.optimizer import VoteOptimizer
from config import Config

# Set up logging with timestamps and level indicators
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s [%(levelname)-8s] %(name)s: %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)

logger = logging.getLogger(__name__)

def analyze_historical_performance(db, num_epochs=10):
    """Analyze which gauges have been most profitable historically."""
    logger.info(f"Analyzing historical performance over {num_epochs} epochs...")
    
    epochs = db.get_recent_epochs(num_epochs)
    logger.info(f"Found {len(epochs)} recent epochs to analyze")
    
    gauge_performance = {}  # gauge_addr -> {'total_bribes': X, 'epochs': Y, 'avg_bribes': Z}
    
    for epoch in epochs:
        logger.debug(f"  Processing epoch {epoch.timestamp}")
        votes = db.get_votes_for_epoch(epoch.timestamp)
        bribes = db.get_bribes_for_epoch(epoch.timestamp)
        
        # Map bribes to gauges
        bribe_to_gauge = {}
        for gauge in db.get_all_gauges():
            if gauge.internal_bribe and gauge.internal_bribe.lower() != "0x0000000000000000000000000000000000000000":
                bribe_to_gauge[gauge.internal_bribe.lower()] = gauge.address
            if gauge.external_bribe and gauge.external_bribe.lower() != "0x0000000000000000000000000000000000000000":
                bribe_to_gauge[gauge.external_bribe.lower()] = gauge.address
        
        # Count bribes per gauge
        for bribe in bribes:
            gauge_addr = bribe_to_gauge.get(bribe.bribe_contract.lower())
            if gauge_addr:
                if gauge_addr not in gauge_performance:
                    gauge_performance[gauge_addr] = {'total_bribes': 0, 'epochs': 0}
                gauge_performance[gauge_addr]['total_bribes'] += bribe.amount
                gauge_performance[gauge_addr]['epochs'] += 1
    
    # Calculate averages and sort
    for addr in gauge_performance:
        gauge_performance[addr]['avg_bribes'] = gauge_performance[addr]['total_bribes'] / gauge_performance[addr]['epochs']
    
    top_performers = sorted(gauge_performance.items(), key=lambda x: x[1]['total_bribes'], reverse=True)[:10]
    logger.info(f"Top 10 historically profitable gauges:")
    for i, (addr, perf) in enumerate(top_performers):
        logger.info(f"  {i+1}. {addr[:10]}... - ${perf['total_bribes']:,.0f} total across {perf['epochs']} epochs (avg: ${perf['avg_bribes']:,.0f})")
    
    return gauge_performance

def fetch_current_epoch_data(db, price_feed, epoch_timestamp):
    """Fetch and process current epoch data."""
    logger.info(f"Fetching data for epoch {epoch_timestamp} ({datetime.fromtimestamp(epoch_timestamp).strftime('%Y-%m-%d')})...")
    
    # Get votes
    logger.debug("Fetching votes...")
    votes = db.get_votes_for_epoch(epoch_timestamp)
    logger.info(f"  Fetched {len(votes)} vote records")
    
    # Get bribes
    logger.debug("Fetching bribes...")
    bribes = db.get_bribes_for_epoch(epoch_timestamp)
    logger.info(f"  Fetched {len(bribes)} bribe events")
    
    if not votes or not bribes:
        logger.error(f"No data found for epoch {epoch_timestamp}")
        return None
    
    # Build gauge data structure
    logger.debug("Building gauge data structure...")
    gauge_data = {}
    for vote in votes:
        gauge_data[vote.gauge] = {
            "address": vote.gauge,
            "current_votes": vote.total_votes,
            "bribes_usd": 0.0,
            "internal_bribes_usd": 0.0,
            "external_bribes_usd": 0.0,
        }
    
    # Load all gauges and build bribe mappings
    logger.debug("Loading gauge bribe contracts...")
    all_gauges = db.get_all_gauges()
    logger.info(f"  Loaded {len(all_gauges)} gauges from database")
    
    bribe_to_gauge = {}
    bribe_type = {}
    for gauge in all_gauges:
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
    
    logger.info(f"  Mapped {len(bribe_to_gauge)} bribe contracts to gauges")
    
    # Fetch token prices
    logger.debug("Fetching unique tokens from bribes...")
    unique_tokens = list(set(bribe.reward_token for bribe in bribes))
    logger.info(f"Fetching prices for {len(unique_tokens)} unique tokens...")
    token_prices = price_feed.get_batch_prices(unique_tokens)
    logger.info(f"  Got prices for {len(token_prices)} tokens")
    
    # Map bribes to gauges and convert to USD
    logger.debug("Converting bribes to USD and mapping to gauges...")
    bribes_mapped = 0
    for i, bribe in enumerate(bribes):
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
            bribes_mapped += 1
        
        if (i + 1) % 500 == 0:
            logger.debug(f"  Processed {i + 1}/{len(bribes)} bribes...")
    
    logger.info(f"Successfully mapped {bribes_mapped}/{len(bribes)} bribes to gauges")
    
    return gauge_data

def generate_recommendation(gauge_data, optimizer):
    """Generate voting recommendation from gauge data."""
    logger.info("Generating recommendation...")
    
    # Filter gauges with bribes
    gauge_list = [g for g in gauge_data.values() if g["bribes_usd"] > 0]
    gauge_list = sorted(gauge_list, key=lambda x: x["bribes_usd"], reverse=True)
    
    logger.info(f"Found {len(gauge_list)} gauges with bribes")
    logger.info(f"Top 5 gauges by bribes:")
    for i, g in enumerate(gauge_list[:5]):
        logger.info(f"  {i+1}. {g['address'][:10]}... - ${g['bribes_usd']:,.2f}")
    
    if not gauge_list:
        logger.error("No gauges with bribes found")
        return None
    
    # Run optimization
    logger.info("Running optimization algorithm...")
    comparison = optimizer.compare_strategies(gauge_list)
    
    return {
        "gauge_list": gauge_list,
        "comparison": comparison,
    }

def main():
    logger.info("="*80)
    logger.info("SMART VOTING RECOMMENDATION ENGINE")
    logger.info("="*80)
    
    # Initialize
    logger.info("Initializing database and tools...")
    db = Database(Config.DATABASE_PATH)
    price_feed = PriceFeed(Config.COINGECKO_API_KEY, db)
    optimizer = VoteOptimizer(Config.YOUR_VOTING_POWER)
    logger.info(f"  Your voting power: {Config.YOUR_VOTING_POWER:,}")
    
    # Analyze historical performance
    logger.info("\n" + "="*80)
    historical = analyze_historical_performance(db, num_epochs=10)
    
    # Get current epoch
    logger.info("\n" + "="*80)
    latest_epoch = db.get_recent_epochs(1)[0]
    logger.info(f"CURRENT EPOCH: {latest_epoch.timestamp} ({datetime.fromtimestamp(latest_epoch.timestamp).strftime('%Y-%m-%d')})")
    
    # Fetch current epoch data
    logger.info("="*80)
    gauge_data = fetch_current_epoch_data(db, price_feed, latest_epoch.timestamp)
    
    if not gauge_data:
        logger.error("Failed to fetch current epoch data")
        sys.exit(1)
    
    # Generate recommendation
    logger.info("\n" + "="*80)
    result = generate_recommendation(gauge_data, optimizer)
    
    if not result:
        logger.error("Failed to generate recommendation")
        sys.exit(1)
    
    # Display recommendation
    comparison = result["comparison"]
    gauge_list = result["gauge_list"]
    
    print(f"\n\n{'='*80}")
    print(f"VOTING RECOMMENDATION")
    print(f"{'='*80}\n")
    
    total_bribes = sum(g["bribes_usd"] for g in gauge_list)
    print(f"Total Bribes Available: ${total_bribes:,.2f}")
    print(f"Your Voting Power: {Config.YOUR_VOTING_POWER:,} votes\n")
    
    print(f"STRATEGY COMPARISON:")
    print(f"  Optimal Strategy Return:  ${comparison['optimal']['return']:,.2f}")
    print(f"  Naive Equal-Split Return: ${comparison['naive']['return']:,.2f}")
    print(f"  Expected Improvement:     {comparison['improvement_pct']:.1f}%\n")
    
    print(f"RECOMMENDED VOTE ALLOCATION:")
    print(f"{'Gauge Address':<14} {'Your Votes':<15} {'Your Share':<12} {'Total Bribes':<15} {'Expected $':<15}")
    print(f"{'-'*71}")
    
    sorted_alloc = sorted(comparison["optimal"]["allocation"].items(), key=lambda x: x[1], reverse=True)
    total_allocated = 0
    for addr, votes_allocated in sorted_alloc:
        gauge = next((g for g in gauge_list if g["address"] == addr), None)
        if gauge:
            share = votes_allocated / (gauge["current_votes"] + votes_allocated)
            expected = gauge["bribes_usd"] * share
            total_allocated += votes_allocated
            
            short_addr = f"{addr[:6]}...{addr[-4:]}"
            print(f"{short_addr:<14} {votes_allocated:>13,} {share:>10.2%} ${gauge['bribes_usd']:>13,.2f} ${expected:>13,.2f}")
    
    print(f"{'-'*71}")
    print(f"{'TOTAL':<14} {total_allocated:>13,}")
    print(f"\n{'='*80}\n")
    
    logger.info(f"\nRecommendation complete. Total allocated: {total_allocated:,} votes")

if __name__ == "__main__":
    main()
