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
