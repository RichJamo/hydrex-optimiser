#!/usr/bin/env python3
"""Debug script to analyze a single epoch with detailed output."""

import logging
from src.database import Database
from src.price_feed import PriceFeed
from analysis.historical import HistoricalAnalyzer
from config import Config

# Set up logging to show all details
logging.basicConfig(
    level=logging.INFO,
    format='%(name)s - %(levelname)s - %(message)s'
)

# Initialize
db = Database(Config.DATABASE_PATH)
price_feed = PriceFeed(Config.COINGECKO_API_KEY, db)
analyzer = HistoricalAnalyzer(db, Config.YOUR_VOTING_POWER, price_feed)

# Analyze the spike: 1762387200 (2025-11-06) with optimal return of $1146.55
epoch = 1762387200
print(f"\n\n{'='*80}")
print(f"ANALYZING EPOCH {epoch} (2025-11-06)")
print(f"{'='*80}\n")

result = analyzer.analyze_epoch(epoch)

if result:
    print(f"\n{'='*80}")
    print(f"RESULTS:")
    print(f"  Total Protocol Fees: ${result['total_bribes']:,.2f}")
    print(f"  Internal Bribes: ${result['total_bribes'] - result['external_bribes']:,.2f}")
    print(f"  External Bribes: ${result['external_bribes']:,.2f}")
    print(f"  Optimal Return: ${result['optimal_return']:,.2f}")
    print(f"  Naive Return: ${result['naive_return']:,.2f}")
    print(f"  Improvement: {result['improvement_pct']:.1f}%")
    print(f"{'='*80}\n")
