#!/usr/bin/env python3
"""
Analyze escrow performance with different allocation strategies:
1. Equal split across ALL gauges (naive)
2. Equal split across top N gauges by bribes (smart naive)
3. Optimal allocation (what we calculated)
"""

import logging
from datetime import datetime
from src.database import Database
from src.optimizer import VoteOptimizer
from src.price_feed import PriceFeed
from analysis.historical import HistoricalAnalyzer
from config import Config

logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger(__name__)

def calculate_actual_return(gauge_list: list, allocation: dict) -> float:
    """Calculate actual return for a given allocation."""
    total_return = 0.0
    
    for gauge_addr, your_votes in allocation.items():
        # Find the gauge data
        gauge = next((g for g in gauge_list if g["address"] == gauge_addr), None)
        if not gauge:
            continue
        
        # Calculate your share and return
        your_share = your_votes / (gauge["current_votes"] + your_votes)
        return_from_gauge = gauge["bribes_usd"] * your_share
        total_return += return_from_gauge
    
    return total_return

def main():
    db = Database(Config.DATABASE_PATH)
    price_feed = PriceFeed(Config.COINGECKO_API_KEY, db)
    analyzer = HistoricalAnalyzer(db, Config.YOUR_VOTING_POWER, price_feed)
    
    print("=" * 130)
    print("ESCROW PERFORMANCE ANALYSIS - STRATEGY COMPARISON")
    print(f"Your Address: {Config.YOUR_ADDRESS}")
    print(f"Voting Power: {Config.YOUR_VOTING_POWER:,}")
    print("=" * 130)
    print()
    
    # Get all epochs
    epochs = db.get_recent_epochs(count=100)
    epochs.reverse()  # Start from oldest
    
    print(f"{'Date':<12} {'Bribes':<12} {'All Gauges':<12} {'Top 5':<12} {'Top 10':<12} {'Optimal':<12} {'Best Strategy':<15}")
    print("-" * 130)
    
    totals = {
        "bribes": 0,
        "all_gauges": 0,
        "top_5": 0,
        "top_10": 0,
        "optimal": 0,
    }
    
    for epoch in epochs:
        try:
            # Get epoch data
            result = analyzer.analyze_epoch(epoch.timestamp)
            
            if not result or not result.get('total_bribes'):
                continue
            
            # Get gauge list for this epoch
            votes = db.get_votes_for_epoch(epoch.timestamp)
            bribes = db.get_bribes_for_epoch(epoch.timestamp)
            
            if not bribes:
                continue
            
            # Build gauge data
            gauge_data = {}
            for vote in votes:
                gauge_data[vote.gauge] = {
                    "address": vote.gauge,
                    "current_votes": vote.total_votes,
                    "bribes_usd": 0.0,
                }
            
            # Map bribes to gauges (simplified)
            bribe_to_gauge = {}
            for gauge in db.get_all_gauges():
                if gauge.internal_bribe:
                    bribe_to_gauge[gauge.internal_bribe.lower()] = gauge.address
                if gauge.external_bribe:
                    bribe_to_gauge[gauge.external_bribe.lower()] = gauge.address
                if gauge.address not in gauge_data:
                    gauge_data[gauge.address] = {
                        "address": gauge.address,
                        "current_votes": 0,
                        "bribes_usd": 0.0,
                    }
            
            # Get prices
            unique_tokens = list(set(bribe.reward_token for bribe in bribes))
            token_prices = price_feed.get_batch_prices_cached_only(unique_tokens)
            
            # Calculate bribe values
            for bribe in bribes:
                gauge_addr = bribe_to_gauge.get(bribe.bribe_contract.lower())
                if gauge_addr and gauge_addr in gauge_data:
                    price = token_prices.get(bribe.reward_token.lower(), 0.0)
                    usd_value = bribe.amount * price
                    gauge_data[gauge_addr]["bribes_usd"] += usd_value
            
            # Filter gauges with bribes
            gauge_list = [g for g in gauge_data.values() if g["bribes_usd"] > 0]
            
            if not gauge_list:
                continue
            
            # Strategy 1: Split across ALL gauges with bribes
            optimizer = VoteOptimizer(Config.YOUR_VOTING_POWER)
            votes_per_gauge = Config.YOUR_VOTING_POWER // len(gauge_list)
            all_gauges_allocation = {g["address"]: votes_per_gauge for g in gauge_list}
            all_gauges_return = calculate_actual_return(gauge_list, all_gauges_allocation)
            
            # Strategy 2: Split across top 5 gauges by bribes
            top_5_gauges = sorted(gauge_list, key=lambda x: x["bribes_usd"], reverse=True)[:5]
            votes_per_top5 = Config.YOUR_VOTING_POWER // len(top_5_gauges)
            top_5_allocation = {g["address"]: votes_per_top5 for g in top_5_gauges}
            top_5_return = calculate_actual_return(gauge_list, top_5_allocation)
            
            # Strategy 3: Split across top 10 gauges by bribes
            top_10_gauges = sorted(gauge_list, key=lambda x: x["bribes_usd"], reverse=True)[:10]
            votes_per_top10 = Config.YOUR_VOTING_POWER // len(top_10_gauges)
            top_10_allocation = {g["address"]: votes_per_top10 for g in top_10_gauges}
            top_10_return = calculate_actual_return(gauge_list, top_10_allocation)
            
            # Strategy 4: Optimal allocation
            optimal_return = result.get('optimal_return', 0)
            
            # Find best strategy
            strategies = {
                "All Gauges": all_gauges_return,
                "Top 5": top_5_return,
                "Top 10": top_10_return,
                "Optimal": optimal_return,
            }
            best_strategy = max(strategies, key=strategies.get)
            
            epoch_date = datetime.fromtimestamp(epoch.timestamp).strftime('%Y-%m-%d')
            epoch_bribes = result.get('total_bribes', 0)
            
            print(f"{epoch_date:<12} ${epoch_bribes:>10,.0f} ${all_gauges_return:>10,.2f} ${top_5_return:>10,.2f} ${top_10_return:>10,.2f} ${optimal_return:>10,.2f} {best_strategy:<15}")
            
            totals["bribes"] += epoch_bribes
            totals["all_gauges"] += all_gauges_return
            totals["top_5"] += top_5_return
            totals["top_10"] += top_10_return
            totals["optimal"] += optimal_return
            
        except Exception as e:
            logger.debug(f"Skipped epoch {epoch.timestamp}: {e}")
            continue
    
    print("-" * 130)
    print(f"{'TOTALS':<12} ${totals['bribes']:>10,.0f} ${totals['all_gauges']:>10,.2f} ${totals['top_5']:>10,.2f} ${totals['top_10']:>10,.2f} ${totals['optimal']:>10,.2f}")
    print()
    print("Strategy Performance:")
    print(f"  All Gauges (naive):     ${totals['all_gauges']:>10,.2f}  (baseline)")
    print(f"  Top 5 Focus:            ${totals['top_5']:>10,.2f}  ({(totals['top_5']/totals['all_gauges']-1)*100:+.1f}%)")
    print(f"  Top 10 Focus:           ${totals['top_10']:>10,.2f}  ({(totals['top_10']/totals['all_gauges']-1)*100:+.1f}%)")
    print(f"  Optimal:                ${totals['optimal']:>10,.2f}  ({(totals['optimal']/totals['all_gauges']-1)*100:+.1f}%)")
    print()
    print("=" * 130)

if __name__ == "__main__":
    main()
