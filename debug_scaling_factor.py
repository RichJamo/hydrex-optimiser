#!/usr/bin/env python3
"""
Debug script to determine the actual scaling factor from known payout.
User received $1800 for the epoch starting 2026-01-22 (claimed 2026-01-29).
"""

import os
import sys
from datetime import datetime
from dotenv import load_dotenv

# Add src to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'src'))

from config import Config
from database import Database
from price_feed import PriceFeed
from analysis.historical import HistoricalAnalyzer

load_dotenv()

def debug_epoch_2026_01_22():
    """Analyze the epoch you claimed on 2026-01-29.
    
    Timeline (per contract):
    - Epoch 2026-01-15 to 2026-01-22: You CAST VOTES here (apply to next epoch)
    - Epoch 2026-01-22 to 2026-01-29: Bribes/Fees EARNED here (determined by your previous votes)
    - Epoch 2026-01-29 onwards: You CAN CLAIM the bribes/fees from epoch 2026-01-22
    """
    
    # Initialize
    db = Database('hydrex.db')
    price_feed = PriceFeed(Config.COINGECKO_API_KEY, db)
    escrow = "0x768a675B8542F23C428C6672738E380176E7635C"
    voting_power = 1530896
    
    analyzer = HistoricalAnalyzer(db, voting_power, price_feed)
    
    # Epoch where bribes/fees are paid (and you claim from)
    bribe_epoch_ts = 1768435200  # 2026-01-22
    
    bribe_date = datetime.utcfromtimestamp(bribe_epoch_ts).strftime('%Y-%m-%d')
    
    print(f"Bribe/Fee epoch: {bribe_date} (ts={bribe_epoch_ts})")
    print(f"Claim date: 2026-01-29 or later")
    print(f"User actual received: $1,800.00")
    print()
    
    # analyze_epoch() now expects the epoch where bribes are paid
    # It will internally look for votes from the previous epoch
    result = analyzer.analyze_epoch(bribe_epoch_ts, escrow)
    
    if result:
        print(f"Calculated actual return: ${result['actual_return']:,.2f}")
        print(f"Calculated naive return: ${result['naive_return']:,.2f}")
        print(f"Calculated optimal return: ${result['optimal_return']:,.2f}")
        print()
        
        actual = result['actual_return']
        claimed = 1800
        
        if actual > 0:
            ratio = claimed / actual
            print(f"Scaling factor needed: {ratio:.2f}x")
            print(f"(Calculated is {ratio:.2f}x too {('high' if ratio > 1 else 'low')})")
        else:
            print("Could not calculate actual return (missing votes?)")
    else:
        print("No analysis result")

if __name__ == '__main__':
    debug_epoch_2026_01_22()
