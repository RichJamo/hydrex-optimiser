"""
Token price feed using CoinGecko API.
Includes caching to avoid rate limits.
"""

import logging
import time
from typing import Dict, Optional

import requests
from pycoingecko import CoinGeckoAPI

from config import Config
from src.subgraph_client import SubgraphClient

logger = logging.getLogger(__name__)


class PriceFeed:
    """Fetches and caches token prices from CoinGecko."""

    # Common token address to CoinGecko ID mapping (Base blockchain)
    TOKEN_ID_MAP = {
        "0x4200000000000000000000000000000000000006": "ethereum",  # WETH on Base
        "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913": "usd-coin",  # USDC on Base
        "0x50c5725949a6f0c72e6c4a641f24049a917db0cb": "dai",  # DAI on Base
        "0x2ae3f1ec7f1f5012cfeab0185bfc7aa3cf0dec22": "coinbase-wrapped-btc",  # cbBTC on Base
        # Add more Base tokens as needed
    }

    # Option tokens or non-spot assets that shouldn't be priced via CoinGecko
    OPTION_TOKEN_PRICE_OVERRIDES = {
        "0xa1136031150e50b015b41f1ca6b2e99e49d8cb78": 0.0,  # oHYDX option token
    }

    def __init__(self, api_key: Optional[str] = None, database=None):
        """
        Initialize price feed with CoinGecko API.

        Args:
            api_key: Optional CoinGecko API key for higher rate limits
            database: Optional database for persistent price caching
        """
        self.api = CoinGeckoAPI(api_key=api_key) if api_key else CoinGeckoAPI()
        self.database = database
        self.cache: Dict[str, tuple[float, float]] = {}  # token -> (price, timestamp)
        self.cache_ttl = Config.PRICE_CACHE_TTL
        analytics_url = Config.ANALYTICS_SUBGRAPH_URL or Config.SUBGRAPH_URL
        self.subgraph_client = SubgraphClient(analytics_url) if analytics_url else None
        self.historical_cache: Dict[tuple[str, int, str], float] = {}
        logger.info("Price feed initialized")

    def get_token_price(self, token_address: str) -> Optional[float]:
        """
        Get current USD price for a token.

        Args:
            token_address: Token contract address

        Returns:
            USD price per token, or None if not found
        """
        token_address = token_address.lower()

        if token_address in self.OPTION_TOKEN_PRICE_OVERRIDES:
            price = self.OPTION_TOKEN_PRICE_OVERRIDES[token_address]
            self.cache[token_address] = (price, time.time())
            return price

        # Check memory cache first
        if token_address in self.cache:
            price, cached_at = self.cache[token_address]
            if time.time() - cached_at < self.cache_ttl:
                logger.debug(f"Memory cache hit for {token_address}: ${price}")
                return price

        # Check database cache (persistent, 1 hour TTL)
        if self.database:
            db_price = self.database.get_token_price(token_address, max_age_seconds=3600)
            if db_price is not None:
                logger.debug(f"DB cache hit for {token_address}: ${db_price}")
                self.cache[token_address] = (db_price, time.time())
                return db_price

        # Fetch from API
        try:
            # Try using known token ID mapping first
            if token_address in self.TOKEN_ID_MAP:
                token_id = self.TOKEN_ID_MAP[token_address]
                price = self._fetch_price_by_id(token_id)
            else:
                # Try fetching by contract address on Base
                price = self._fetch_price_by_address(token_address)

            if price is not None:
                self.cache[token_address] = (price, time.time())
                # Save to database for persistence
                if self.database:
                    self.database.save_token_price(token_address, price)
                logger.debug(f"Fetched price for {token_address}: ${price}")
                return price

        except Exception as e:
            logger.error(f"Failed to fetch price for {token_address}: {e}")

        return None

    def _fetch_price_by_id(self, token_id: str) -> Optional[float]:
        """
        Fetch price by CoinGecko token ID.

        Args:
            token_id: CoinGecko token ID

        Returns:
            USD price or None
        """
        try:
            data = self.api.get_price(ids=token_id, vs_currencies="usd")
            if token_id in data and "usd" in data[token_id]:
                return float(data[token_id]["usd"])
        except Exception as e:
            logger.warning(f"Failed to fetch price by ID {token_id}: {e}")

        return None

    def _fetch_price_by_address(self, token_address: str) -> Optional[float]:
        """
        Fetch price by contract address on Base.

        Args:
            token_address: Token contract address

        Returns:
            USD price or None
        """
        try:
            # CoinGecko uses 'base' as the platform ID
            data = self.api.get_token_price(
                id="base", contract_addresses=token_address, vs_currencies="usd"
            )
            if token_address in data and "usd" in data[token_address]:
                return float(data[token_address]["usd"])
        except Exception as e:
            logger.warning(f"Failed to fetch price by address {token_address}: {e}")

        return None

    def get_batch_prices_cached_only(self, token_addresses: list[str]) -> Dict[str, float]:
        """
        Get prices for multiple tokens from cache only (no API calls).

        Args:
            token_addresses: List of token addresses

        Returns:
            Dictionary mapping address to price (only cached prices)
        """
        prices = {}

        for address in token_addresses:
            address_lower = address.lower()
            if address_lower in self.OPTION_TOKEN_PRICE_OVERRIDES:
                prices[address_lower] = self.OPTION_TOKEN_PRICE_OVERRIDES[address_lower]

        # Check memory cache first
        for address in token_addresses:
            address_lower = address.lower()
            if address_lower in self.cache:
                price, cached_at = self.cache[address_lower]
                if time.time() - cached_at < self.cache_ttl:
                    prices[address_lower] = price
                    continue

        # Check database cache for remaining addresses
        uncached_in_memory = [addr.lower() for addr in token_addresses if addr.lower() not in prices]
        if self.database and uncached_in_memory:
            db_prices = self.database.get_batch_token_prices(uncached_in_memory, max_age_seconds=86400)  # 24 hour TTL
            for addr, price in db_prices.items():
                prices[addr] = price
                self.cache[addr] = (price, time.time())
        
        logger.info(f"Cached prices found: {len(prices)}/{len(token_addresses)}")
        return prices

    def get_batch_prices_for_timestamp(
        self,
        token_addresses: list[str],
        timestamp: int,
        granularity: str = "hour",
    ) -> Dict[str, float]:
        """
        Get historical prices for multiple tokens at a specific timestamp using the subgraph.

        Args:
            token_addresses: List of token addresses
            timestamp: Unix timestamp
            granularity: "hour" or "day"

        Returns:
            Dictionary mapping address to priceUSD
        """
        if not token_addresses:
            return {}

        if granularity not in {"hour", "day"}:
            raise ValueError("granularity must be 'hour' or 'day'")

        if granularity == "hour":
            period_start = timestamp - (timestamp % 3600)
        else:
            period_start = timestamp - (timestamp % 86400)

        prices: Dict[str, float] = {}
        for address in token_addresses:
            address_lower = address.lower()
            if address_lower in self.OPTION_TOKEN_PRICE_OVERRIDES:
                price = self.OPTION_TOKEN_PRICE_OVERRIDES[address_lower]
                prices[address_lower] = price
                self.historical_cache[(address_lower, period_start, granularity)] = price
        # Check historical cache first
        for address in token_addresses:
            key = (address.lower(), period_start, granularity)
            if key in self.historical_cache:
                prices[address.lower()] = self.historical_cache[key]

        missing = [addr.lower() for addr in token_addresses if addr.lower() not in prices]

        # Check database cache for historical prices (try requested granularity first, then alternate)
        if missing and self.database:
            db_prices = self.database.get_historical_token_prices(
                missing, period_start, granularity
            )
            if db_prices:
                logger.info(f"Found {len(db_prices)} prices in DB cache ({granularity} granularity)")
                for addr, price in db_prices.items():
                    prices[addr] = price
                    self.historical_cache[(addr, period_start, granularity)] = price
            missing = [addr for addr in missing if addr not in prices]
            
            # If still missing and we tried "day", also try "hour" granularity in database
            if missing and granularity == "day":
                db_prices_hour = self.database.get_historical_token_prices(
                    missing, period_start, "hour"
                )
                if db_prices_hour:
                    logger.info(f"Found {len(db_prices_hour)} prices in DB cache (hour granularity fallback)")
                    for addr, price in db_prices_hour.items():
                        prices[addr] = price
                        self.historical_cache[(addr, period_start, granularity)] = price
                missing = [addr for addr in missing if addr not in prices]

        if missing and self.subgraph_client:
            try:
                if granularity == "hour":
                    data = self.subgraph_client.fetch_all_paginated(
                        self.subgraph_client.fetch_token_hour_data,
                        token_addresses=missing,
                        period_start_unix=period_start,
                    )
                else:
                    data = self.subgraph_client.fetch_all_paginated(
                        self.subgraph_client.fetch_token_day_data,
                        token_addresses=missing,
                        date_unix=period_start,
                    )

                to_persist = []
                for item in data:
                    token_id = item["token"]["id"].lower()
                    price = float(item.get("priceUSD") or 0.0)
                    prices[token_id] = price
                    self.historical_cache[(token_id, period_start, granularity)] = price
                    to_persist.append((token_id, period_start, granularity, price))

                if to_persist and self.database:
                    self.database.save_historical_token_prices(to_persist)
            except Exception as e:
                # Don't retry subgraph on errors; skip to fallbacks
                logger.debug(f"Skipping subgraph fetch (error: {str(e)[:60]}). Will use fallbacks.")

        # Fallback 1: Try forward-fill from previous week (604800 seconds = 1 week)
        still_missing = [addr.lower() for addr in token_addresses if addr.lower() not in prices]
        if still_missing:
            prev_period_start = period_start - 604800  # Go back 1 week
            # Try database cache first (no subgraph call since subgraph is broken)
            if self.database:
                # Try requested granularity first
                db_prev_prices = self.database.get_historical_token_prices(
                    still_missing, prev_period_start, granularity
                )
                if db_prev_prices:
                    for addr, price in db_prev_prices.items():
                        if price > 0:
                            prices[addr] = price
                            self.historical_cache[(addr, period_start, granularity)] = price
                            logger.info(f"Using forward-fill price for {addr[:10]}: ${price} (from 1 week prior, database cached)")
                
                # If still missing and we tried "day", also try "hour" granularity
                still_missing_after = [addr for addr in still_missing if addr not in prices]
                if still_missing_after and granularity == "day":
                    db_prev_prices_hour = self.database.get_historical_token_prices(
                        still_missing_after, prev_period_start, "hour"
                    )
                    if db_prev_prices_hour:
                        for addr, price in db_prev_prices_hour.items():
                            if price > 0:
                                prices[addr] = price
                                self.historical_cache[(addr, period_start, granularity)] = price
                                logger.info(f"Using forward-fill price for {addr[:10]}: ${price} (from 1 week prior, hour granularity)")

            still_missing = [addr for addr in still_missing if addr not in prices]

        # Fallback 2: Try cached current prices for anything still missing
        if still_missing:
            fallback = self.get_batch_prices_cached_only(still_missing)
            prices.update(fallback)

        return prices

    def get_batch_prices(self, token_addresses: list[str]) -> Dict[str, float]:
        """
        Get prices for multiple tokens efficiently.

        Args:
            token_addresses: List of token addresses

        Returns:
            Dictionary mapping address to price
        """
        prices = {}
        uncached_addresses = []

        # Check memory cache first
        for address in token_addresses:
            address_lower = address.lower()
            if address_lower in self.OPTION_TOKEN_PRICE_OVERRIDES:
                price = self.OPTION_TOKEN_PRICE_OVERRIDES[address_lower]
                prices[address_lower] = price
                self.cache[address_lower] = (price, time.time())
                continue
            if address_lower in self.cache:
                price, cached_at = self.cache[address_lower]
                if time.time() - cached_at < self.cache_ttl:
                    prices[address_lower] = price
                    continue
            uncached_addresses.append(address_lower)

        # Check database cache for uncached addresses
        if self.database and uncached_addresses:
            db_prices = self.database.get_batch_token_prices(uncached_addresses, max_age_seconds=3600)
            for addr, price in db_prices.items():
                prices[addr] = price
                self.cache[addr] = (price, time.time())
            # Remove addresses found in DB from uncached list
            uncached_addresses = [addr for addr in uncached_addresses if addr not in db_prices]
        
        logger.info(f"Cached: {len(prices)}, Need to fetch: {len(uncached_addresses)}")

        # Batch fetch uncached prices
        if uncached_addresses:
            try:
                # Group by known IDs and unknown addresses
                known_ids = [
                    self.TOKEN_ID_MAP[addr]
                    for addr in uncached_addresses
                    if addr in self.TOKEN_ID_MAP
                ]
                unknown_addresses = [
                    addr for addr in uncached_addresses if addr not in self.TOKEN_ID_MAP
                ]

                # Fetch known tokens by ID
                if known_ids:
                    data = self.api.get_price(ids=",".join(known_ids), vs_currencies="usd")
                    for addr in uncached_addresses:
                        if addr in self.TOKEN_ID_MAP:
                            token_id = self.TOKEN_ID_MAP[addr]
                            if token_id in data and "usd" in data[token_id]:
                                price = float(data[token_id]["usd"])
                                prices[addr] = price
                                self.cache[addr] = (price, time.time())
                                # Save to database
                                if self.database:
                                    self.database.save_token_price(addr, price)

                # Fetch unknown tokens by address (rate limit friendly)
                for addr in unknown_addresses:
                    price = self._fetch_price_by_address(addr)
                    if price is not None:
                        prices[addr] = price
                        self.cache[addr] = (price, time.time())
                        # Save to database
                        if self.database:
                            self.database.save_token_price(addr, price)
                    time.sleep(1)  # Avoid rate limits

            except Exception as e:
                logger.error(f"Failed to batch fetch prices: {e}")

        return prices

    def calculate_bribe_value(
        self, token_address: str, amount: int, decimals: int = 18
    ) -> float:
        """
        Calculate USD value of a bribe amount.

        Args:
            token_address: Token contract address
            amount: Token amount in smallest unit
            decimals: Token decimals

        Returns:
            USD value
        """
        price = self.get_token_price(token_address)
        if price is None:
            logger.warning(f"No price found for {token_address}, using $0")
            return 0.0

        token_amount = amount / (10**decimals)
        return token_amount * price

    def clear_cache(self) -> None:
        """Clear the price cache."""
        self.cache.clear()
        logger.info("Price cache cleared")
