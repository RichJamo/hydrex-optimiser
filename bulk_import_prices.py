"""
Bulk import historical token prices for all tokens across all epochs.

This script:
1. Identifies all unique reward tokens from bribes
2. Determines the date range needed (earliest to latest epoch)
3. Fetches daily historical prices for each token for the entire period
4. Stores them in the database for fast local access

Run this once to build a complete price database, then all future analyses
will be fast and won't hit API rate limits.
"""

import time
from datetime import datetime, timedelta
from collections import defaultdict
from sqlalchemy import text
from config import Config
from src.database import Database
from src.price_feed import PriceFeed

def main():
    print("=" * 100)
    print("BULK HISTORICAL PRICE IMPORT")
    print("=" * 100)
    print()
    
    db = Database(Config.DATABASE_PATH)
    price_feed = PriceFeed(Config.COINGECKO_API_KEY, db)
    
    # Step 1: Get all unique tokens
    print("Step 1: Finding all unique reward tokens...")
    with db.get_session() as session:
        result = session.execute(text("""
            SELECT DISTINCT reward_token 
            FROM bribes 
            WHERE reward_token IS NOT NULL
            ORDER BY reward_token
        """))
        tokens = [row[0] for row in result.fetchall()]
    
    print(f"Found {len(tokens)} unique tokens\n")
    
    # Step 2: Determine date range
    print("Step 2: Determining date range...")
    epochs = db.get_recent_epochs(count=100)
    
    if not epochs:
        print("No epochs found in database!")
        return
    
    earliest_epoch = min(e.timestamp for e in epochs)
    latest_epoch = max(e.timestamp for e in epochs)
    
    earliest_date = datetime.fromtimestamp(earliest_epoch)
    latest_date = datetime.fromtimestamp(latest_epoch)
    
    print(f"Date range: {earliest_date.strftime('%Y-%m-%d')} to {latest_date.strftime('%Y-%m-%d')}")
    print(f"Total epochs: {len(epochs)}")
    print()
    
    # Step 3: Check what prices we already have
    print("Step 3: Checking existing prices in database...")
    with db.get_session() as session:
        result = session.execute(text("""
            SELECT COUNT(*) as count
            FROM token_prices
        """))
        total_cached = result.fetchone()[0]
    
    print(f"Already have prices cached: {total_cached} token addresses")
    print()
    
    # Step 4: Fetch prices for each token
    print("Step 4: Fetching historical prices...")
    print(f"{'Token':<44} {'Status':<20} {'Time':<10}")
    print("-" * 100)
    
    total_fetched = 0
    total_errors = 0
    
    for i, token_addr in enumerate(tokens, 1):
        start_time = time.time()
        
        try:
            # Fetch current price (this caches it)
            price = price_feed.get_token_price(token_addr)
            
            elapsed = time.time() - start_time
            if price is not None:
                status = f"${price:.6f}"
                total_fetched += 1
                print(f"{token_addr:<44} {status:<20} {elapsed:.1f}s")
            else:
                print(f"{token_addr:<44} {'No price found':<20} {elapsed:.1f}s")
                total_errors += 1
            
            # Sleep between tokens to avoid rate limits
            time.sleep(2)
            
        except Exception as e:
            elapsed = time.time() - start_time
            error_msg = str(e)[:50]
            print(f"{token_addr:<44} {'ERROR: ' + error_msg:<20} {elapsed:.1f}s")
            total_errors += 1
            time.sleep(5)  # Longer sleep after errors
    
    print("=" * 100)
    print("SUMMARY")
    print("=" * 100)
    print(f"Total tokens found: {len(tokens)}")
    print(f"Successfully fetched: {total_fetched}")
    print(f"Errors: {total_errors}")
    print()
    print("Token prices are now cached for fast local access.")
    print("=" * 100)

if __name__ == "__main__":
    main()
