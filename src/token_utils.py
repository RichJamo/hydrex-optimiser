#!/usr/bin/env python3
"""
Token utilities for fetching and caching token metadata (decimals, symbols)
"""
import json
import os
import time
from typing import Optional

from web3 import Web3

from config import Config

ERC20_ABI = [
    {
        "constant": True,
        "inputs": [],
        "name": "decimals",
        "outputs": [{"name": "", "type": "uint8"}],
        "type": "function",
    },
    {
        "constant": True,
        "inputs": [],
        "name": "symbol",
        "outputs": [{"name": "", "type": "string"}],
        "type": "function",
    },
]

w3 = Web3(Web3.HTTPProvider(Config.RPC_URL, request_kwargs={"timeout": Config.RPC_TIMEOUT}))

# Cache file paths
DECIMALS_CACHE_PATH = os.path.join(os.path.dirname(__file__), "token_decimals.json")
SYMBOLS_CACHE_PATH = os.path.join(os.path.dirname(__file__), "token_symbols.json")

def load_decimals_cache():
    """Load cached token decimals."""
    if os.path.exists(DECIMALS_CACHE_PATH):
        try:
            with open(DECIMALS_CACHE_PATH, "r") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}

def load_symbols_cache():
    """Load cached token symbols."""
    if os.path.exists(SYMBOLS_CACHE_PATH):
        try:
            with open(SYMBOLS_CACHE_PATH, "r") as f:
                cache = json.load(f)
                # Only load actual symbols, not fallback values (which contain "...")
                return {k: v for k, v in cache.items() if "..." not in v}
        except Exception:
            return {}
    return {}

def save_decimals_cache(cache):
    """Save token decimals cache."""
    try:
        with open(DECIMALS_CACHE_PATH, "w") as f:
            json.dump(cache, f, indent=2)
    except Exception as e:
        print(f"Warning: Could not save decimals cache: {e}")

def save_symbols_cache(cache):
    """Save token symbols cache."""
    try:
        with open(SYMBOLS_CACHE_PATH, "w") as f:
            json.dump(cache, f, indent=2)
    except Exception as e:
        print(f"Warning: Could not save symbols cache: {e}")

def get_token_decimals(
    token_address: str,
    cache: dict = None,
    database: Optional[object] = None,
) -> int:
    """
    Get token decimals with retry logic for rate limiting.
    Returns 18 as default if unable to fetch.
    """
    if cache is None:
        cache = load_decimals_cache()
    
    token_address = token_address.lower()
    
    # Check DB cache first
    if database is not None:
        record = database.get_token_metadata(token_address)
        if record and record.decimals is not None:
            return record.decimals

    # Check file cache next
    if token_address in cache:
        return cache[token_address]
    
    try:
        contract = w3.eth.contract(
            address=Web3.to_checksum_address(token_address), 
            abi=ERC20_ABI
        )
        
        max_retries = 3
        for attempt in range(max_retries):
            try:
                decimals = contract.functions.decimals().call()
                cache[token_address] = decimals
                save_decimals_cache(cache)
                if database is not None:
                    database.save_token_metadata(token_address, decimals=decimals)
                # Shorter delay to reduce script duration while still respecting rate limits
                time.sleep(0.05)  
                return decimals
            except Exception as e:
                if "429" in str(e) and attempt < max_retries - 1:
                    # Rate limited, wait and retry with exponential backoff
                    time.sleep(1 * (attempt + 1))
                    continue
                raise
    except Exception as e:
        # Default to 18 decimals if we can't fetch
        print(f"Warning: Could not fetch decimals for {token_address}: {str(e)[:80]}")
        cache[token_address] = 18
        save_decimals_cache(cache)
        if database is not None:
            database.save_token_metadata(token_address, decimals=18)
        return 18

def prefetch_token_metadata(database, bribes: list):
    """
    Pre-fetch decimals and symbols for all tokens in bribes.
    Only fetches for tokens not already in DB cache.
    This prevents repeated RPC calls on subsequent script runs.
    """
    if database is None:
        return
    
    # Get all unique token addresses from bribes
    unique_tokens = set()
    for bribe in bribes:
        if bribe.get('token_address'):
            unique_tokens.add(bribe['token_address'].lower())
    
    print(f"[DEBUG] Pre-fetching metadata for {len(unique_tokens)} unique tokens...", flush=True)
    
    decimals_cache = load_decimals_cache()
    symbols_cache = load_symbols_cache()
    
    # Fetch missing tokens
    missing_count = 0
    for token_addr in unique_tokens:
        # Check if already in DB or cache
        db_record = database.get_token_metadata(token_addr)
        if db_record and db_record.decimals is not None and db_record.symbol is not None:
            continue  # Already have full metadata
        
        # Not in DB, try to fetch and cache
        missing_count += 1
        try:
            # Fetch decimals if missing
            if not db_record or db_record.decimals is None:
                decimals = get_token_decimals(token_addr, cache=decimals_cache, database=database)
            
            # Fetch symbol if missing
            if not db_record or db_record.symbol is None:
                symbol = get_token_symbol(token_addr, cache=symbols_cache, database=database)
        except Exception as e:
            print(f"[DEBUG] Error pre-fetching metadata for {token_addr}: {e}", flush=True)
    
    print(f"[DEBUG] Pre-fetch complete: {len(unique_tokens) - missing_count} from cache, {missing_count} fetched", flush=True)

def get_token_symbol(
    token_address: str,
    cache: dict = None,
    database: Optional[object] = None,
) -> str:
    """
    Get token symbol with retry logic for rate limiting.
    Returns shortened address if unable to fetch.
    """
    if cache is None:
        cache = load_symbols_cache()
    
    token_address_lower = token_address.lower()
    
    # Check DB cache first
    if database is not None:
        record = database.get_token_metadata(token_address)
        if record and record.symbol:
            return record.symbol

    # Check file cache next
    if token_address_lower in cache:
        return cache[token_address_lower]
    
    try:
        contract = w3.eth.contract(
            address=Web3.to_checksum_address(token_address), 
            abi=ERC20_ABI
        )
        
        max_retries = 3
        for attempt in range(max_retries):
            try:
                symbol = contract.functions.symbol().call()
                if isinstance(symbol, bytes):
                    symbol = symbol.decode("utf-8").rstrip("\x00")
                if not symbol or not isinstance(symbol, str):
                    raise ValueError("Empty or invalid symbol")
                cache[token_address_lower] = symbol
                save_symbols_cache(cache)
                if database is not None:
                    database.save_token_metadata(token_address, symbol=symbol)
                time.sleep(0.1)  # Rate limit
                return symbol
            except Exception as e:
                if "429" in str(e) and attempt < max_retries - 1:
                    # Rate limited, wait and retry with exponential backoff
                    time.sleep(1 * (attempt + 1))
                    continue
                raise
    except Exception as e:
        # Fall back to shortened address
        fallback = f"{token_address[:6]}...{token_address[-4:]}"
        cache[token_address_lower] = fallback
        save_symbols_cache(cache)
        if database is not None:
            database.save_token_metadata(token_address, symbol=fallback)
        print(f"Warning: Could not fetch symbol for {token_address}: {str(e)[:80]}")
        return fallback
