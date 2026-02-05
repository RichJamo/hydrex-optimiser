"""
Analyze escrow account performance vs. optimal returns.

Shows for each epoch:
- What the optimal return COULD HAVE BEEN
- What a naive equal-split return would be
- The opportunity cost (difference between optimal and naive)

This helps identify if voting power is being allocated effectively.
"""

import logging
from datetime import datetime
from config import Config
from src.database import Database
from src.price_feed import PriceFeed
from analysis.historical import HistoricalAnalyzer

# Suppress verbose logging
logging.getLogger().setLevel(logging.WARNING)

def main():
    print("=" * 130)
    print("ESCROW PERFORMANCE: OPTIMAL vs NAIVE RETURNS")
    print(f"Your Address: {Config.YOUR_ADDRESS}")
    print(f"Voting Power: {Config.YOUR_VOTING_POWER:,} votes")
    print("=" * 130)
    print()
    
    db = Database(Config.DATABASE_PATH)
    price_feed = PriceFeed(Config.COINGECKO_API_KEY, db)
    analyzer = HistoricalAnalyzer(db, Config.YOUR_VOTING_POWER, price_feed)
    
    # Get all epochs
    epochs = db.get_recent_epochs(count=100)  # Get all available
    epochs.reverse()  # Start from oldest
    
    print(f"{'Epoch Date':<12} {'Total Bribes':<15} {'Optimal Return':<15} {'Naive Return':<15} {'Opportunity':<15} {'Efficiency %':<15}")
    print("-" * 130)
    
    total_bribes = 0
    total_optimal = 0
    total_naive = 0
    total_opportunities = 0
    epochs_analyzed = 0
    
    for epoch in epochs:
        try:
            # Analyze this epoch
            result = analyzer.analyze_epoch(epoch.timestamp)
            
            if not result or not result.get('total_bribes_usd'):
                continue
            
            epoch_bribes = result.get('total_bribes_usd', 0)
            optimal_return = result.get('optimal_return', 0)
            naive_return = result.get('naive_return', 0)
            opportunity = optimal_return - naive_return
            
            if naive_return > 0:
                efficiency = (naive_return / optimal_return * 100) if optimal_return > 0 else 0
            else:
                efficiency = 0
            
            epoch_date = datetime.fromtimestamp(epoch.timestamp).strftime('%Y-%m-%d')
            
            print(f"{epoch_date:<12} ${epoch_bribes:>13,.2f} ${optimal_return:>13,.2f} ${naive_return:>13,.2f} ${opportunity:>13,.2f} {efficiency:>13.1f}%")
            
            total_bribes += epoch_bribes
            total_optimal += optimal_return
            total_naive += naive_return
            total_opportunities += opportunity
            epochs_analyzed += 1
            
        except KeyboardInterrupt:
            break
        except Exception as e:
            # Skip epochs with errors
            continue
    
    if epochs_analyzed == 0:
        print("No epochs with complete data found.")
        return
    
    print("-" * 130)
    
    # Calculate totals
    if total_naive > 0:
        overall_efficiency = (total_naive / total_optimal * 100) if total_optimal > 0 else 0
    else:
        overall_efficiency = 0
    
    print(f"{'TOTAL':<12} ${total_bribes:>13,.2f} ${total_optimal:>13,.2f} ${total_naive:>13,.2f} ${total_opportunities:>13,.2f} {overall_efficiency:>13.1f}%")
    
    print()
    print("=" * 130)
    print("SUMMARY")
    print("=" * 130)
    print(f"Epochs analyzed: {epochs_analyzed}")
    print(f"Total bribes available: ${total_bribes:,.2f}")
    print(f"Total optimal returns: ${total_optimal:,.2f}")
    print(f"Total naive returns: ${total_naive:,.2f}")
    print(f"Total opportunity cost: ${total_opportunities:,.2f}")
    print()
    print(f"Average per epoch:")
    print(f"  Optimal return: ${total_optimal / epochs_analyzed:,.2f}")
    print(f"  Naive return: ${total_naive / epochs_analyzed:,.2f}")
    print(f"  Opportunity missed: ${total_opportunities / epochs_analyzed:,.2f}")
    print()
    print(f"Overall efficiency: {overall_efficiency:.1f}%")
    print(f"(100% = all votes allocated optimally, <100% = suboptimal allocation)")
    print("=" * 130)

if __name__ == "__main__":
    main()
