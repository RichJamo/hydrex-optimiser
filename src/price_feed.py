"""
Token price feed using Hydrex routing (Kyber-backed) and CoinGecko fallback.
Includes caching to avoid rate limits.
"""

import logging
import time
from typing import Dict, Optional

import requests
from web3 import Web3

from config import Config
from config.settings import (
    HYDREX_ROUTING_API_URL,
    HYDREX_ROUTING_ORIGIN,
    HYDREX_ROUTING_SLIPPAGE_BPS,
    HYDREX_ROUTING_SOURCE,
    MY_ESCROW_ADDRESS,
    USDC_ADDRESS,
)
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

    # Special token addresses
    HYDX_ADDRESS = "0x00000e7efa313f4e11bfff432471ed9423ac6b30"  # HYDX token
    OHYDX_ADDRESS = "0xa1136031150e50b015b41f1ca6b2e99e49d8cb78"  # oHYDX option token
    HYDX_FALLBACK_PRICE = 0.06  # $0.06 if CoinGecko unavailable
    OHYDX_DISCOUNT = 0.7  # oHYDX = HYDX * 0.7 (30% discount)

    # Option tokens or non-spot assets that shouldn't be priced via CoinGecko
    OPTION_TOKEN_PRICE_OVERRIDES = {
        # oHYDX handled dynamically in get_token_price
    }

    ERC20_ABI = [
        {
            "constant": True,
            "inputs": [],
            "name": "decimals",
            "outputs": [{"name": "", "type": "uint8"}],
            "type": "function",
        }
    ]

    CHAIN_ID_BASE = 8453
    USDC_DECIMALS = 6

    def __init__(self, api_key: Optional[str] = None, database=None):
        """
        Initialize price feed with CoinGecko API.

        Args:
            api_key: Optional CoinGecko API key for higher rate limits
            database: Optional database for persistent price caching
        """
        self.api_key = api_key
        self.is_demo_key = bool(api_key and str(api_key).startswith("CG-"))
        self.cg_base_url = (
            "https://api.coingecko.com/api/v3"
            if self.is_demo_key or not self.api_key
            else "https://pro-api.coingecko.com/api/v3"
        )
        self.database = database
        self.cache: Dict[str, tuple[float, float]] = {}  # token -> (price, timestamp)
        self.decimals_cache: Dict[str, int] = {}
        self.cache_ttl = Config.PRICE_CACHE_TTL
        self.routing_api_url = HYDREX_ROUTING_API_URL.rstrip("/")
        self.routing_source = HYDREX_ROUTING_SOURCE.strip()
        self.routing_origin = HYDREX_ROUTING_ORIGIN.strip()
        self.routing_slippage_bps = int(HYDREX_ROUTING_SLIPPAGE_BPS)
        self.routing_taker = (MY_ESCROW_ADDRESS or "").strip()
        self._w3 = None
        analytics_url = Config.ANALYTICS_SUBGRAPH_URL or Config.SUBGRAPH_URL
        self.subgraph_client = SubgraphClient(analytics_url) if analytics_url else None
        self.historical_cache: Dict[tuple[str, int, str], float] = {}
        logger.info("Price feed initialized")

    def _get_token_decimals(self, token_address: str) -> int:
        token_address = token_address.lower()

        if token_address == USDC_ADDRESS.lower():
            return self.USDC_DECIMALS

        if token_address in self.decimals_cache:
            return self.decimals_cache[token_address]

        if self.database is not None:
            try:
                record = self.database.get_token_metadata(token_address)
                if record and record.decimals is not None:
                    decimals = int(record.decimals)
                    self.decimals_cache[token_address] = decimals
                    return decimals
            except Exception:
                pass

        try:
            if self._w3 is None and Config.RPC_URL:
                self._w3 = Web3(
                    Web3.HTTPProvider(
                        Config.RPC_URL,
                        request_kwargs={"timeout": int(Config.RPC_TIMEOUT)},
                    )
                )

            if self._w3 is not None:
                contract = self._w3.eth.contract(
                    address=Web3.to_checksum_address(token_address),
                    abi=self.ERC20_ABI,
                )
                decimals = int(contract.functions.decimals().call())
                self.decimals_cache[token_address] = decimals
                if self.database is not None:
                    try:
                        self.database.save_token_metadata(token_address, decimals=decimals)
                    except Exception:
                        pass
                return decimals
        except Exception as e:
            logger.debug(f"Could not fetch decimals for {token_address}: {e}")

        # Safe default for unknown ERC20s
        self.decimals_cache[token_address] = 18
        return 18

    def _fetch_prices_via_hydrex_routing(self, token_addresses: list[str]) -> Dict[str, float]:
        addresses = [a.lower() for a in token_addresses if a]
        if not addresses:
            return {}

        taker = self.routing_taker
        if not (taker.startswith("0x") and len(taker) == 42):
            logger.debug("Hydrex routing price fetch skipped: MY_ESCROW_ADDRESS is not configured")
            return {}

        swaps = []
        decimals_by_token: Dict[str, int] = {}
        for address in addresses:
            if address == USDC_ADDRESS.lower():
                continue
            decimals = self._get_token_decimals(address)
            decimals_by_token[address] = decimals
            swaps.append(
                {
                    "fromTokenAddress": address,
                    "toTokenAddress": USDC_ADDRESS.lower(),
                    "amount": str(10**decimals),
                }
            )

        if not swaps:
            return {USDC_ADDRESS.lower(): 1.0} if USDC_ADDRESS.lower() in addresses else {}

        url = f"{self.routing_api_url}/quote/multi"
        origin = self.routing_origin.rstrip("/")
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Accept-Language": "en-US,en;q=0.9",
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            ),
            "Origin": origin,
            "Referer": f"{origin}/",
        }

        out: Dict[str, float] = {}

        def _request_multi(swaps_payload: list[Dict[str, str]]) -> dict:
            payload: Dict[str, object] = {
                "taker": taker,
                "chainId": str(self.CHAIN_ID_BASE),
                "slippage": str(self.routing_slippage_bps),
                "origin": self.routing_origin,
                "swaps": swaps_payload,
            }
            if self.routing_source:
                payload["source"] = self.routing_source

            response = requests.post(url, json=payload, headers=headers, timeout=30)
            response.raise_for_status()
            return response.json()

        def _extract_prices(data: dict) -> Dict[str, float]:
            prices: Dict[str, float] = {}
            for leg in data.get("swaps", []) or []:
                try:
                    from_token = str(leg.get("fromTokenAddress", "")).lower()
                    if not from_token:
                        continue

                    amount_in = int(str(leg.get("amountIn", "0")))
                    amount_out = int(str(leg.get("amountOut", "0")))
                    if amount_in <= 0 or amount_out <= 0:
                        continue

                    from_decimals = decimals_by_token.get(from_token, 18)
                    token_units = amount_in / float(10**from_decimals)
                    usdc_units = amount_out / float(10**self.USDC_DECIMALS)
                    if token_units <= 0:
                        continue

                    prices[from_token] = usdc_units / token_units
                except Exception:
                    continue
            return prices

        chunk_size = 20
        for start in range(0, len(swaps), chunk_size):
            chunk = swaps[start : start + chunk_size]
            try:
                out.update(_extract_prices(_request_multi(chunk)))
                continue
            except Exception as e:
                if len(chunk) > 1:
                    logger.warning(
                        "Hydrex routing batch price request failed for %s tokens, retrying individually: %s",
                        len(chunk),
                        e,
                    )
                else:
                    logger.warning(f"Hydrex routing price request failed: {e}")

            # Retry each token separately so one bad token does not poison the entire chunk.
            for single_swap in chunk:
                try:
                    out.update(_extract_prices(_request_multi([single_swap])))
                except Exception:
                    continue

        if USDC_ADDRESS.lower() in addresses:
            out[USDC_ADDRESS.lower()] = 1.0

        return out

    def _coingecko_get(self, path: str, params: dict) -> Optional[dict]:
        url = f"{self.cg_base_url}{path}"
        headers = {}
        if self.api_key:
            if self.is_demo_key:
                headers["x-cg-demo-api-key"] = str(self.api_key)
            else:
                headers["x-cg-pro-api-key"] = str(self.api_key)

        retries = 4
        backoff_seconds = 1.5
        for attempt in range(retries):
            resp = None
            try:
                resp = requests.get(url, params=params, headers=headers, timeout=20)
                if resp.status_code == 429 and attempt < retries - 1:
                    wait = backoff_seconds * (2 ** attempt)
                    logger.warning(f"CoinGecko throttled (429). Retrying in {wait:.1f}s")
                    time.sleep(wait)
                    continue

                resp.raise_for_status()
                return resp.json()
            except Exception as e:
                err_text = ""
                try:
                    err_text = (resp.text if resp is not None else "")[:400]
                except Exception:
                    pass
                if attempt < retries - 1:
                    wait = backoff_seconds * (2 ** attempt)
                    logger.warning(f"CoinGecko request failed (retrying in {wait:.1f}s): {e} {err_text}")
                    time.sleep(wait)
                    continue
                logger.warning(f"CoinGecko request failed: {e} {err_text}")
                return None
        return None

    def fetch_batch_prices_by_address(self, token_addresses: list[str]) -> Dict[str, float]:
        """
        Fetch prices for multiple Base token addresses in one API call.
        Returns mapping of lowercased token address -> usd price.
        """
        addresses = [a.lower() for a in token_addresses if a]
        if not addresses:
            return {}

        # Primary source: Hydrex routing API (Kyber-backed)
        out: Dict[str, float] = self._fetch_prices_via_hydrex_routing(addresses)
        missing = [a for a in addresses if a not in out]
        if not missing:
            return out

        # Demo key currently allows only 1 contract address/request.
        # Fall back to sequential single-address requests to keep behavior stable.
        if self.is_demo_key and len(missing) > 1:
            for address in missing:
                data_single = self._coingecko_get(
                    "/simple/token_price/base",
                    {"contract_addresses": address, "vs_currencies": "usd"},
                )
                if not data_single:
                    continue
                payload = data_single.get(address)
                if payload and payload.get("usd") is not None:
                    try:
                        out[address] = float(payload["usd"])
                    except Exception:
                        continue
            return out

        data = self._coingecko_get(
            "/simple/token_price/base",
            {"contract_addresses": ",".join(missing), "vs_currencies": "usd"},
        )
        if not data:
            return out

        for addr, payload in data.items():
            try:
                if payload and "usd" in payload and payload["usd"] is not None:
                    out[str(addr).lower()] = float(payload["usd"])
            except Exception:
                continue
        return out

    def get_token_price(self, token_address: str) -> Optional[float]:
        """
        Get current USD price for a token.

        Args:
            token_address: Token contract address

        Returns:
            USD price per token, or None if not found
        """
        token_address = token_address.lower()

        # Handle oHYDX specially: price = HYDX * 0.7 (30% discount)
        if token_address == self.OHYDX_ADDRESS.lower():
            hydx_price = self.get_token_price(self.HYDX_ADDRESS)
            if hydx_price is None:
                # Use fallback price if HYDX not found
                hydx_price = self.HYDX_FALLBACK_PRICE
            ohydx_price = hydx_price * self.OHYDX_DISCOUNT
            self.cache[token_address] = (ohydx_price, time.time())
            logger.debug(f"Calculated oHYDX price from HYDX: ${ohydx_price:.4f} (HYDX: ${hydx_price:.4f})")
            return ohydx_price

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

        # Try Hydrex routing API first (Kyber-backed route quote)
        routing_prices = self._fetch_prices_via_hydrex_routing([token_address])
        routing_price = routing_prices.get(token_address)
        if routing_price is not None:
            self.cache[token_address] = (routing_price, time.time())
            if self.database:
                try:
                    self.database.save_token_price(token_address, routing_price)
                except Exception as db_error:
                    logger.debug(f"Could not save routing price to DB (locked): {db_error}")
            logger.debug(f"Fetched routing price for {token_address}: ${routing_price}")
            return routing_price

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
                # Save to database for persistence (ignore lock errors)
                if self.database:
                    try:
                        self.database.save_token_price(token_address, price)
                    except Exception as db_error:
                        # Ignore database lock errors - price is still cached in memory
                        logger.debug(f"Could not save price to DB (locked): {db_error}")
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
            data = self._coingecko_get(
                "/simple/price",
                {"ids": token_id, "vs_currencies": "usd"},
            )
            if not data:
                return None
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
            data = self._coingecko_get(
                "/simple/token_price/base",
                {"contract_addresses": token_address, "vs_currencies": "usd"},
            )
            if not data:
                return None
            token_address = token_address.lower()
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
