"""
Utility functions for Hydrex Vote Optimizer.
"""

import logging
import time
from datetime import datetime, timezone
from functools import wraps
from typing import Any, Callable, Optional

from web3 import Web3


def setup_logging(log_level: str = "INFO", log_file: Optional[str] = None) -> None:
    """
    Setup logging configuration.

    Args:
        log_level: Logging level (DEBUG, INFO, WARNING, ERROR)
        log_file: Optional log file path
    """
    handlers = [logging.StreamHandler()]
    if log_file:
        handlers.append(logging.FileHandler(log_file))

    logging.basicConfig(
        level=getattr(logging, log_level.upper()),
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        handlers=handlers,
    )


def retry(max_attempts: int = 3, delay: float = 1.0, backoff: float = 2.0):
    """
    Retry decorator with exponential backoff.

    Args:
        max_attempts: Maximum number of retry attempts
        delay: Initial delay between retries in seconds
        backoff: Multiplier for delay after each retry

    Returns:
        Decorated function
    """

    def decorator(func: Callable) -> Callable:
        @wraps(func)
        def wrapper(*args, **kwargs) -> Any:
            current_delay = delay
            last_exception = None

            for attempt in range(max_attempts):
                try:
                    return func(*args, **kwargs)
                except Exception as e:
                    last_exception = e
                    if attempt < max_attempts - 1:
                        logging.warning(
                            f"{func.__name__} failed (attempt {attempt + 1}/{max_attempts}): {e}"
                        )
                        time.sleep(current_delay)
                        current_delay *= backoff
                    else:
                        logging.error(
                            f"{func.__name__} failed after {max_attempts} attempts: {e}"
                        )

            raise last_exception

        return wrapper

    return decorator


def format_token_amount(amount: int, decimals: int = 18) -> float:
    """
    Format token amount from wei to human-readable.

    Args:
        amount: Token amount in smallest unit
        decimals: Token decimals

    Returns:
        Human-readable amount
    """
    return amount / (10**decimals)


def format_usd(amount: float) -> str:
    """
    Format USD amount with proper separators.

    Args:
        amount: USD amount

    Returns:
        Formatted string (e.g., "$1,234.56")
    """
    return f"${amount:,.2f}"


def format_percentage(value: float) -> str:
    """
    Format percentage with one decimal place.

    Args:
        value: Percentage value (0-100)

    Returns:
        Formatted string (e.g., "45.0%")
    """
    return f"{value:.1f}%"


def epoch_to_datetime(timestamp: int) -> datetime:
    """
    Convert Unix timestamp to datetime.

    Args:
        timestamp: Unix timestamp

    Returns:
        Datetime object in UTC
    """
    return datetime.fromtimestamp(timestamp, tz=timezone.utc)


def datetime_to_epoch(dt: datetime) -> int:
    """
    Convert datetime to Unix timestamp.

    Args:
        dt: Datetime object

    Returns:
        Unix timestamp
    """
    return int(dt.timestamp())


def time_until(target_timestamp: int) -> str:
    """
    Format time until target timestamp.

    Args:
        target_timestamp: Target Unix timestamp

    Returns:
        Human-readable time until (e.g., "3 days, 5 hours")
    """
    now = time.time()
    diff = target_timestamp - now

    if diff <= 0:
        return "0 seconds"

    days = int(diff // 86400)
    hours = int((diff % 86400) // 3600)
    minutes = int((diff % 3600) // 60)

    parts = []
    if days > 0:
        parts.append(f"{days} day{'s' if days != 1 else ''}")
    if hours > 0:
        parts.append(f"{hours} hour{'s' if hours != 1 else ''}")
    if minutes > 0 and days == 0:
        parts.append(f"{minutes} minute{'s' if minutes != 1 else ''}")

    return ", ".join(parts)


def checksum_address(address: str) -> str:
    """
    Convert address to checksum format.

    Args:
        address: Ethereum address

    Returns:
        Checksummed address
    """
    return Web3.to_checksum_address(address)


def truncate_address(address: str, chars: int = 6) -> str:
    """
    Truncate Ethereum address for display.

    Args:
        address: Ethereum address
        chars: Number of characters to show on each end

    Returns:
        Truncated address (e.g., "0xabc...123")
    """
    if len(address) <= chars * 2 + 3:
        return address
    return f"{address[:chars]}...{address[-chars:]}"


def safe_division(numerator: float, denominator: float, default: float = 0.0) -> float:
    """
    Safely divide two numbers, returning default if denominator is zero.

    Args:
        numerator: Numerator
        denominator: Denominator
        default: Default value if division by zero

    Returns:
        Division result or default
    """
    if denominator == 0:
        return default
    return numerator / denominator
