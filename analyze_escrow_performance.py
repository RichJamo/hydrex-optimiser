"""
Analyze escrow account performance vs. optimal returns.

This compares:
1. Optimal returns (if votes were allocated perfectly)
2. Naive equal-split returns (baseline)
3. Opportunity cost (what was left on the table)

Note: Requires cached token prices for accurate USD calculations.
Run smart_price_fetcher.py first to build the price cache.
"""

import logging
from datetime import datetime
from config import Config
from src.database import Database
from src.price_feed import PriceFeed
from analysis.historical import HistoricalAnalyzer

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def main():
    db = Database(Config.DATABASE_PATH)
    price_feed = PriceFeed(Config.COINGECKO_API_KEY, db)
    analyzer = HistoricalAnalyzer(db, Config.YOUR_VOTING_POWER, price_feed)
    
    print("=" * 120)
    print("ESCROW ACCOUNT PERFORMANCE ANALYSIS")
    print(f"Your Address: {Config.YOUR_ADDRESS}")
    print("=" * 120)
    print()
    
    # Get all epochs
    epochs = db.get_recent_epochs(count=100)
    epochs.reverse()  # Start from oldest
    
    print(f"{'Date':<12} {'Total Bribes':<14} {'Actual Return':<14} {'Naive Return':<14} {'Optimal Return':<14} {'Opp. Cost':<14} {'Efficiency':<12}")
    print("-" * 120)
    
    total_bribes = 0
    total_actual = 0
    total_naive = 0
    total_optimal = 0
    total_opportunities_missed = 0
    epochs_with_actual = 0
    epochs_analyzed = 0
    
    for epoch in epochs:
        try:
            # Analyze this epoch
            result = analyzer.analyze_epoch(epoch.timestamp, actual_voter=Config.YOUR_ADDRESS)
            
            if not result or not result.get('total_bribes'):
                continue
            
            epoch_bribes = result.get('total_bribes', 0)
            actual_return = result.get('actual_return', None)
            optimal_return = result.get('optimal_return', 0)
            naive_return = result.get('naive_return', 0)
            opportunity = optimal_return - naive_return
            
            if optimal_return > 0:
                efficiency = (naive_return / optimal_return) * 100
            else:
                efficiency = 0
            
            epoch_date = datetime.fromtimestamp(epoch.timestamp).strftime('%Y-%m-%d')

            actual_display = f"${actual_return:>12,.2f}" if actual_return is not None else f"{'N/A':>12}"
            print(f"{epoch_date:<12} ${epoch_bribes:>12,.2f} {actual_display} ${naive_return:>12,.2f} ${optimal_return:>12,.2f} ${opportunity:>12,.2f} {efficiency:>10.1f}%")
            
            total_bribes += epoch_bribes
            if actual_return is not None:
                total_actual += actual_return
                epochs_with_actual += 1
            total_naive += naive_return
            total_optimal += optimal_return
            total_opportunities_missed += opportunity
            epochs_analyzed += 1
            
        except Exception as e:
            logger.debug(f"Skipped epoch {epoch.timestamp}: {e}")
            continue
    
    print("-" * 120)
    
    if epochs_analyzed > 0:
        avg_efficiency = (total_naive / total_optimal * 100) if total_optimal > 0 else 0
        actual_total_display = f"${total_actual:>12,.2f}" if epochs_with_actual > 0 else f"{'N/A':>12}"
        print(f"{'TOTALS':<12} ${total_bribes:>12,.2f} {actual_total_display} ${total_naive:>12,.2f} ${total_optimal:>12,.2f} ${total_opportunities_missed:>12,.2f} {avg_efficiency:>10.1f}%")
        print()
        print(f"Epochs analyzed: {epochs_analyzed}")
        if epochs_with_actual > 0:
            print(f"Epochs with actual votes: {epochs_with_actual}")
        print(f"Average bribes per epoch: ${total_bribes / epochs_analyzed:,.2f}")
        if epochs_with_actual > 0:
            print(f"Average actual return per epoch: ${total_actual / epochs_with_actual:,.2f}")
        print(f"Average naive return per epoch: ${total_naive / epochs_analyzed:,.2f}")
        print(f"Average optimal return per epoch: ${total_optimal / epochs_analyzed:,.2f}")
        print(f"Average opportunity missed per epoch: ${total_opportunities_missed / epochs_analyzed:,.2f}")
        print()
        print(f"Overall efficiency: {avg_efficiency:.1f}%")
        if avg_efficiency < 100:
            print(f"Potential improvement: ${total_opportunities_missed:,.2f} ({(100-avg_efficiency):.1f}% more)")
    
    print()
    print("=" * 120)
    print("\nNOTE: Actual returns are computed from on-chain gauge vote events (subgraph-backed).")
    print("Optimal/naive returns are counterfactual comparisons based on total gauge votes.")
    print("=" * 120)


if __name__ == "__main__":
    main()
