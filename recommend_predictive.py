#!/usr/bin/env python3
"""
Predictive voting recommendation engine.
Analyzes historical patterns to predict which gauges will offer best returns NEXT week.
"""

import logging
from datetime import datetime
from collections import defaultdict

from src.database import Database
from src.price_feed import PriceFeed
from src.optimizer import VoteOptimizer
from config import Config

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)-8s] %(name)s: %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)

logger = logging.getLogger(__name__)

def analyze_historical_patterns(db, price_feed, num_epochs=10):
    """Analyze historical bribe patterns to predict next week."""
    logger.info(f"Analyzing {num_epochs} historical epochs to predict next week...")
    
    epochs = db.get_recent_epochs(num_epochs)
    logger.info(f"Found {len(epochs)} epochs to analyze")
    
    # Track per-gauge statistics
    gauge_stats = defaultdict(lambda: {
        'appearances': 0,
        'total_bribes_usd': 0,
        'total_votes': [],
        'bribe_history': [],
    })
    
    # Build bribe mapping once
    bribe_to_gauge = {}
    bribe_type = {}
    all_gauges = db.get_all_gauges()
    for gauge in all_gauges:
        if gauge.internal_bribe and gauge.internal_bribe.lower() != "0x0000000000000000000000000000000000000000":
            bribe_to_gauge[gauge.internal_bribe.lower()] = gauge.address
            bribe_type[gauge.internal_bribe.lower()] = "internal"
        if gauge.external_bribe and gauge.external_bribe.lower() != "0x0000000000000000000000000000000000000000":
            bribe_to_gauge[gauge.external_bribe.lower()] = gauge.address
            bribe_type[gauge.external_bribe.lower()] = "external"
    
    # Analyze each epoch
    for i, epoch in enumerate(epochs):
        logger.info(f"  Analyzing epoch {i+1}/{len(epochs)}: {datetime.fromtimestamp(epoch.timestamp).strftime('%Y-%m-%d')}")
        
        votes = db.get_votes_for_epoch(epoch.timestamp)
        bribes = db.get_bribes_for_epoch(epoch.timestamp)
        
        # Track votes per gauge
        votes_by_gauge = {v.gauge: v.total_votes for v in votes}
        
        # Get unique tokens and fetch prices
        unique_tokens = list(set(bribe.reward_token for bribe in bribes))
        token_prices = price_feed.get_batch_prices(unique_tokens)
        
        # Track bribes per gauge for this epoch
        epoch_bribes = defaultdict(lambda: {'internal': 0, 'external': 0, 'total': 0})
        
        for bribe in bribes:
            gauge_addr = bribe_to_gauge.get(bribe.bribe_contract.lower())
            if gauge_addr:
                price = token_prices.get(bribe.reward_token.lower(), 0.0)
                usd_value = bribe.amount * price
                
                b_type = bribe_type.get(bribe.bribe_contract.lower(), 'external')
                epoch_bribes[gauge_addr][b_type] += usd_value
                epoch_bribes[gauge_addr]['total'] += usd_value
        
        # Update gauge statistics
        for gauge_addr, bribe_data in epoch_bribes.items():
            gauge_stats[gauge_addr]['appearances'] += 1
            gauge_stats[gauge_addr]['total_bribes_usd'] += bribe_data['total']
            gauge_stats[gauge_addr]['bribe_history'].append(bribe_data['total'])
            if gauge_addr in votes_by_gauge:
                gauge_stats[gauge_addr]['total_votes'].append(votes_by_gauge[gauge_addr])
    
    # Calculate predictions
    predictions = []
    for gauge_addr, stats in gauge_stats.items():
        if stats['appearances'] >= 3:  # Must appear in at least 3 epochs
            avg_bribes = stats['total_bribes_usd'] / stats['appearances']
            avg_votes = sum(stats['total_votes']) / len(stats['total_votes']) if stats['total_votes'] else 0
            consistency = stats['appearances'] / len(epochs)  # % of epochs with bribes
            
            # Calculate variance
            bribe_values = stats['bribe_history']
            variance = sum((x - avg_bribes) ** 2 for x in bribe_values) / len(bribe_values)
            std_dev = variance ** 0.5
            
            predictions.append({
                'address': gauge_addr,
                'predicted_bribes_usd': avg_bribes,
                'predicted_votes': avg_votes,
                'consistency': consistency,
                'appearances': stats['appearances'],
                'std_dev': std_dev,
            })
    
    # Sort by predicted bribes
    predictions.sort(key=lambda x: x['predicted_bribes_usd'], reverse=True)
    
    logger.info(f"\nFound {len(predictions)} gauges with ≥3 appearances")
    logger.info(f"Top 10 predicted gauges for next week:")
    for i, pred in enumerate(predictions[:10]):
        logger.info(
            f"  {i+1}. {pred['address'][:10]}... - "
            f"${pred['predicted_bribes_usd']:,.2f} avg "
            f"({pred['consistency']:.0%} consistent, "
            f"±${pred['std_dev']:,.2f} volatility)"
        )
    
    return predictions

def main():
    logger.info("="*80)
    logger.info("PREDICTIVE VOTING RECOMMENDATION ENGINE")
    logger.info("Based on historical patterns to predict NEXT WEEK's returns")
    logger.info("="*80)
    
    # Initialize
    logger.info("\nInitializing...")
    db = Database(Config.DATABASE_PATH)
    price_feed = PriceFeed(Config.COINGECKO_API_KEY, db)
    optimizer = VoteOptimizer(Config.YOUR_VOTING_POWER)
    logger.info(f"  Your voting power: {Config.YOUR_VOTING_POWER:,}")
    
    # Get current epoch
    latest_epoch = db.get_recent_epochs(1)[0]
    next_epoch_timestamp = latest_epoch.timestamp + 604800
    logger.info(f"\nCurrent epoch: {datetime.fromtimestamp(latest_epoch.timestamp).strftime('%Y-%m-%d')}")
    logger.info(f"NEXT epoch (what you're voting for): {datetime.fromtimestamp(next_epoch_timestamp).strftime('%Y-%m-%d')}")
    
    # Analyze historical patterns
    logger.info("\n" + "="*80)
    predictions = analyze_historical_patterns(db, price_feed, num_epochs=10)
    
    if not predictions:
        logger.error("No predictions available")
        return
    
    # Build gauge data for optimizer using predictions
    logger.info("\n" + "="*80)
    logger.info("Building predicted gauge data for optimization...")
    
    # Fetch pool addresses for all gauges
    gauge_pool_map = {}
    for pred in predictions:
        pool = db.execute_query(
            "SELECT pool FROM gauges WHERE address = ?",
            (pred['address'],)
        )
        if pool and len(pool) > 0:
            gauge_pool_map[pred['address']] = pool[0][0]
    
    gauge_list = []
    for pred in predictions:
        gauge_list.append({
            'address': pred['address'],
            'pool': gauge_pool_map.get(pred['address'], pred['address']),
            'current_votes': pred['predicted_votes'],  # Use historical average
            'bribes_usd': pred['predicted_bribes_usd'],
            'consistency': pred['consistency'],
            'std_dev': pred['std_dev'],
        })
    
    logger.info(f"Running optimization on {len(gauge_list)} predicted gauges...")
    comparison = optimizer.compare_strategies(gauge_list)
    
    # Display recommendation
    print(f"\n\n{'='*80}")
    print(f"PREDICTIVE VOTING RECOMMENDATION FOR NEXT WEEK")
    print(f"Vote tonight → Earn rewards next week (Jan 29 - Feb 5)")
    print(f"{'='*80}\n")
    
    total_predicted_bribes = sum(g['bribes_usd'] for g in gauge_list)
    print(f"Predicted Total Bribes Available: ${total_predicted_bribes:,.2f}")
    print(f"(Based on 10-epoch historical average)")
    print(f"Your Voting Power: {Config.YOUR_VOTING_POWER:,} votes\n")
    
    print(f"EXPECTED RETURNS:")
    print(f"  Optimal Strategy:         ${comparison['optimal']['return']:,.2f}")
    print(f"  Naive Equal-Split:        ${comparison['naive']['return']:,.2f}")
    print(f"  Expected Improvement:     {comparison['improvement_pct']:.1f}%\n")
    
    print(f"RECOMMENDED VOTE ALLOCATION (Top Gauges):")
    print(f"{'Gauge Address':<44} {'Pool Address':<44} {'Votes':<13} {'Share':<10} {'Pred. Bribes':<14} {'Expected $':<13}")
    print(f"{'-'*160}")
    
    sorted_alloc = sorted(comparison["optimal"]["allocation"].items(), key=lambda x: x[1], reverse=True)
    total_allocated = 0
    
    for addr, votes_allocated in sorted_alloc:
        gauge = next((g for g in gauge_list if g["address"] == addr), None)
        if gauge:
            share = votes_allocated / (gauge["current_votes"] + votes_allocated) if gauge["current_votes"] > 0 else 1.0
            expected = gauge["bribes_usd"] * share
            total_allocated += votes_allocated
            
            pool_addr = gauge.get('pool', addr)
            
            print(f"{addr:<44} {pool_addr:<44} {votes_allocated:>11,} {share:>8.2%} ${gauge['bribes_usd']:>12,.2f} ${expected:>11,.2f}")
    
    print(f"{'-'*160}")
    print(f"{'TOTAL':<88} {total_allocated:>11,}")
    print(f"\n{'='*160}")
    
    print(f"\nNOTE: Predictions based on 10-epoch average. Actual bribes may vary.")
    print(f"High consistency = appears most weeks. Low consistency = sporadic.\n")

if __name__ == "__main__":
    main()
