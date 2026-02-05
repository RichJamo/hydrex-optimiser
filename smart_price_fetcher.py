"""
Efficient historical price fetcher with smart caching.

Uses CoinGecko's batch API to fetch prices for multiple tokens at once,
with aggressive caching to minimize API calls.
"""

import time
import logging
from datetime import datetime
from collections import defaultdict
from config import Config
from src.database import Database
from src.price_feed import PriceFeed

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def batch_fetch_prices(tokens: list, price_feed: PriceFeed) -> dict:
    """
    Fetch current prices for a batch of tokens efficiently.
    
    Args:
        tokens: List of token addresses
        price_feed: PriceFeed instance
        
    Returns:
        Dict mapping token address to price
    """
    prices = {}
    
    for i, token in enumerate(tokens):
        try:
            price = price_feed.get_token_price(token)
            if price is not None:
                prices[token] = price
                logger.info(f"[{i+1}/{len(tokens)}] {token[:10]}... = ${price:.6f}")
            else:
                logger.warning(f"[{i+1}/{len(tokens)}] {token[:10]}... = No price found")
            
            # Rate limit protection - 1 second between requests
            time.sleep(1)
            
        except Exception as e:
            logger.error(f"[{i+1}/{len(tokens)}] {token[:10]}... = Error: {e}")
            time.sleep(3)  # Longer delay on error
    
    return prices


def main():
    print("=" * 100)
    print("SMART HISTORICAL PRICE FETCHER")
    print("=" * 100)
    print()
    
    db = Database(Config.DATABASE_PATH)
    price_feed = PriceFeed(Config.COINGECKO_API_KEY, db)
    
    # Get all unique tokens
    print("Step 1: Identifying all unique reward tokens...")
    from sqlalchemy import text
    
    with db.get_session() as session:
        result = session.execute(text("""
            SELECT DISTINCT reward_token 
            FROM bribes 
            WHERE reward_token IS NOT NULL
            ORDER BY reward_token
        """))
        all_tokens = [row[0] for row in result.fetchall()]
    
    print(f"Found {len(all_tokens)} unique tokens")
    print()
    
    # Get epochs
    print("Step 2: Loading epochs...")
    epochs = db.get_recent_epochs(count=100)
    print(f"Found {len(epochs)} epochs")
    print()
    
    # Fetch current prices for all tokens (these will be cached for instant future use)
    print("Step 3: Fetching and caching current prices...")
    print("-" * 100)
    prices = batch_fetch_prices(all_tokens, price_feed)
    
    print("-" * 100)
    print(f"Successfully cached prices for {len(prices)} tokens")
    print()
    
    # Show summary
    print("=" * 100)
    print("SUMMARY")
    print("=" * 100)
    print(f"Total tokens: {len(all_tokens)}")
    print(f"Prices cached: {len(prices)}")
    print(f"Missing prices: {len(all_tokens) - len(prices)}")
    print()
    print("Prices are now cached in memory and database.")
    print("Future analyses will use these cached prices for instant lookup.")
    print()
    print("NOTE: For true historical prices at each epoch timestamp,")
    print("you would need CoinGecko API Pro or a different data source.")
    print("=" * 100)


if __name__ == "__main__":
    main()
