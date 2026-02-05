"""
Configuration module for Hydrex Vote Optimizer.
Loads environment variables and defines constants.
"""

import os
from datetime import datetime, timezone
from typing import Optional

from dotenv import load_dotenv

# Load environment variables
load_dotenv()


class Config:
    """Application configuration."""

    # RPC Configuration
    RPC_URL: str = os.getenv("RPC_URL", "https://rpc.linea.build")
    RPC_TIMEOUT: int = int(os.getenv("RPC_TIMEOUT", "30"))

    # Subgraph Configuration
    SUBGRAPH_URL: Optional[str] = os.getenv("SUBGRAPH_URL")
    ANALYTICS_SUBGRAPH_URL: Optional[str] = os.getenv("ANALYTICS_SUBGRAPH_URL")

    # Contract Addresses
    VOTER_ADDRESS: str = os.getenv("VOTER_ADDRESS", "")

    # User Configuration
    YOUR_ADDRESS: str = os.getenv("YOUR_ADDRESS", "")
    YOUR_VOTING_POWER: int = int(os.getenv("YOUR_VOTING_POWER", "1000000"))

    # API Keys
    COINGECKO_API_KEY: Optional[str] = os.getenv("COINGECKO_API_KEY")

    # Database
    DATABASE_PATH: str = os.getenv("DATABASE_PATH", "data.db")

    # Logging
    LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")
    LOG_FILE: str = os.getenv("LOG_FILE", "hydrex_optimizer.log")

    # Cache Settings
    PRICE_CACHE_TTL: int = int(os.getenv("PRICE_CACHE_TTL", "300"))

    # Optimization Settings
    MIN_VOTE_ALLOCATION: int = int(os.getenv("MIN_VOTE_ALLOCATION", "1000"))
    MAX_GAUGES_TO_VOTE: int = int(os.getenv("MAX_GAUGES_TO_VOTE", "10"))

    # Epoch Configuration
    EPOCH_DURATION: int = 604800  # 7 days in seconds
    EPOCH_START_DAY: int = 2  # Wednesday (0=Monday, 6=Sunday)
    EPOCH_START_HOUR: int = 0  # 00:00 UTC
    EPOCH_START_MINUTE: int = 0

    # Voting Window
    SAFE_VOTING_START_DAY: int = 5  # Saturday
    SAFE_VOTING_START_HOUR: int = 18  # 18:00 UTC
    SAFE_VOTING_END_DAY: int = 1  # Tuesday
    SAFE_VOTING_END_HOUR: int = 20  # 20:00 UTC

    # Validation
    @classmethod
    def validate(cls) -> list[str]:
        """
        Validate configuration and return list of errors.

        Returns:
            List of error messages, empty if valid
        """
        errors = []

        if not cls.VOTER_ADDRESS:
            errors.append("VOTER_ADDRESS not set in .env")

        if not cls.YOUR_ADDRESS:
            errors.append("YOUR_ADDRESS not set in .env")

        if cls.YOUR_VOTING_POWER <= 0:
            errors.append("YOUR_VOTING_POWER must be positive")

        if not cls.RPC_URL:
            errors.append("RPC_URL not set in .env")

        return errors

    @classmethod
    def get_current_epoch_timestamp(cls) -> int:
        """
        Calculate the current epoch start timestamp.

        Returns:
            Unix timestamp of current epoch start (Wednesday 00:00 UTC)
        """
        now = datetime.now(timezone.utc)
        days_since_wednesday = (now.weekday() - cls.EPOCH_START_DAY) % 7
        epoch_start = now.replace(
            hour=cls.EPOCH_START_HOUR,
            minute=cls.EPOCH_START_MINUTE,
            second=0,
            microsecond=0,
        )
        epoch_start = epoch_start.replace(
            day=now.day - days_since_wednesday
        )
        return int(epoch_start.timestamp())

    @classmethod
    def is_in_safe_voting_window(cls) -> bool:
        """
        Check if current time is in the safe voting window.

        Returns:
            True if in safe voting window (Saturday 18:00 - Tuesday 20:00 UTC)
        """
        now = datetime.now(timezone.utc)
        weekday = now.weekday()
        hour = now.hour

        # Saturday 18:00 - Sunday 23:59
        if weekday == cls.SAFE_VOTING_START_DAY and hour >= cls.SAFE_VOTING_START_HOUR:
            return True
        # Sunday - Monday (all day)
        if weekday in [6, 0]:
            return True
        # Tuesday 00:00 - 20:00
        if weekday == cls.SAFE_VOTING_END_DAY and hour <= cls.SAFE_VOTING_END_HOUR:
            return True

        return False


# Minimal VoterV5 ABI
VOTER_ABI = [
    {
        "anonymous": False,
        "inputs": [
            {"indexed": True, "internalType": "address", "name": "voter", "type": "address"},
            {"indexed": False, "internalType": "uint256", "name": "weight", "type": "uint256"},
        ],
        "name": "Voted",
        "type": "event",
    },
    {
        "anonymous": False,
        "inputs": [
            {"indexed": True, "internalType": "address", "name": "gauge", "type": "address"},
            {"indexed": False, "internalType": "address", "name": "creator", "type": "address"},
            {"indexed": False, "internalType": "address", "name": "internal_bribe", "type": "address"},
            {"indexed": True, "internalType": "address", "name": "external_bribe", "type": "address"},
            {"indexed": True, "internalType": "address", "name": "pool", "type": "address"},
        ],
        "name": "GaugeCreated",
        "type": "event",
    },
    {
        "inputs": [{"name": "gauge", "type": "address"}],
        "name": "weights",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [{"name": "gauge", "type": "address"}],
        "name": "poolForGauge",
        "outputs": [{"name": "", "type": "address"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [{"name": "gauge", "type": "address"}],
        "name": "internal_bribes",
        "outputs": [{"name": "", "type": "address"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [{"name": "gauge", "type": "address"}],
        "name": "external_bribes",
        "outputs": [{"name": "", "type": "address"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [{"name": "gauge", "type": "address"}],
        "name": "isAlive",
        "outputs": [{"name": "", "type": "bool"}],
        "stateMutability": "view",
        "type": "function",
    },
]

# Minimal Bribe Contract ABI
BRIBE_ABI = [
    {
        "anonymous": False,
        "inputs": [
            {"indexed": True, "name": "rewardToken", "type": "address"},
            {"indexed": False, "name": "amount", "type": "uint256"},
            {"indexed": False, "name": "startTimestamp", "type": "uint256"},
        ],
        "name": "RewardAdded",
        "type": "event",
    },
]
