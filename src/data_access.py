"""
High-level data access layer for analysis scripts.

Provides convenient query methods that combine data from multiple tables
and return structured results needed by analysis scripts.

This layer sits on top of src/database.py and abstracts away SQL/ORM details.
"""

from typing import List, Dict, Optional, Tuple, Any
import sqlite3
from dataclasses import dataclass
from datetime import datetime

from src.database import Database, Gauge, Bribe, Vote, TokenMetadata


@dataclass
class BribeDetails:
    """Detailed bribe/reward record with token info."""
    epoch: int
    bribe_contract: str
    gauge_address: str
    pool_name: Optional[str]
    bribe_type: str  # "internal" or "external"
    token_address: str
    token_symbol: Optional[str]
    token_decimals: Optional[int]
    amount_wei: str
    amount_human: Optional[float]
    timestamp: int


@dataclass
class EpochBribesSummary:
    """Summary of all bribes for an epoch."""
    epoch: int
    total_bribes_amount: float  # Total in human-readable units (with conversion applied)
    bribe_count: int
    unique_tokens: int
    bribes: List[BribeDetails]


class DataAccess:
    """High-level data access for analysis scripts."""

    def __init__(self, db: Database):
        """
        Initialize data access layer.
        
        Args:
            db: Initialized Database instance
        """
        self.db = db

    def get_bribes_for_epoch_detailed(
        self,
        epoch: int,
        pool_addresses: Optional[List[str]] = None,
    ) -> EpochBribesSummary:
        """
        Get all bribes for an epoch with full details (token symbols, decimals, etc).
        
        Args:
            epoch: Epoch timestamp (WEEK-aligned)
            pool_addresses: Optional list of pool addresses to filter by
        
        Returns:
            EpochBribesSummary with all bribe details
        """
        bribes = self.db.get_bribes_for_epoch(epoch)
        
        # Build gauge map
        all_gauges = self.db.get_all_gauges(alive_only=False)
        gauge_by_bribe: Dict[str, Tuple[str, str, str]] = {}  # bribe_contract -> (gauge, pool, bribe_type)
        
        for gauge in all_gauges:
            if gauge.internal_bribe:
                gauge_by_bribe[gauge.internal_bribe.lower()] = (gauge.address, gauge.pool or "", "internal")
            if gauge.external_bribe:
                gauge_by_bribe[gauge.external_bribe.lower()] = (gauge.address, gauge.pool or "", "external")
        
        # Build token metadata map
        token_metadata: Dict[str, TokenMetadata] = {}
        
        bribe_details = []
        total_amount = 0.0
        token_set = set()
        
        for bribe in bribes:
            bribe_l = bribe.bribe_contract.lower()
            token_l = bribe.reward_token.lower()
            
            # Get gauge/pool info
            gauge_info = gauge_by_bribe.get(bribe_l)
            if gauge_info:
                gauge_addr, pool_name, bribe_type = gauge_info
            else:
                gauge_addr, pool_name, bribe_type = "", "", "unknown"
            
            # Filter by pool if specified
            if pool_addresses and pool_name and pool_name.lower() not in [p.lower() for p in pool_addresses]:
                continue
            
            # Get token metadata (with caching)
            if token_l not in token_metadata:
                token_metadata[token_l] = self.db.get_token_metadata(token_l)
            
            meta = token_metadata[token_l]
            token_symbol = meta.symbol if meta else None
            token_decimals = meta.decimals if meta else None
            
            # Convert amount to human-readable
            amount_human = None
            if bribe.amount is not None:
                amount_human = bribe.amount
            elif bribe.amount_wei is not None and token_decimals is not None:
                try:
                    amount_human = int(bribe.amount_wei) / (10 ** token_decimals)
                except (ValueError, TypeError):
                    pass
            
            if amount_human is not None:
                total_amount += amount_human
            
            token_set.add(token_l)
            
            detail = BribeDetails(
                epoch=epoch,
                bribe_contract=bribe.bribe_contract,
                gauge_address=gauge_addr,
                pool_name=pool_name,
                bribe_type=bribe_type,
                token_address=bribe.reward_token,
                token_symbol=token_symbol,
                token_decimals=token_decimals,
                amount_wei=bribe.amount_wei or "",
                amount_human=amount_human,
                timestamp=bribe.timestamp,
            )
            bribe_details.append(detail)
        
        return EpochBribesSummary(
            epoch=epoch,
            total_bribes_amount=total_amount,
            bribe_count=len(bribe_details),
            unique_tokens=len(token_set),
            bribes=bribe_details,
        )

    def get_bribes_by_pool_and_type(
        self,
        epoch: int,
        pool_name: str,
    ) -> Dict[str, List[BribeDetails]]:
        """
        Get bribes for a specific epoch and pool, grouped by type (internal/external).
        
        Args:
            epoch: Epoch timestamp
            pool_name: Pool name to query
        
        Returns:
            Dict mapping "internal"|"external" to list of BribeDetails
        """
        summary = self.get_bribes_for_epoch_detailed(epoch, pool_addresses=[pool_name])
        
        grouped: Dict[str, List[BribeDetails]] = {"internal": [], "external": []}
        for detail in summary.bribes:
            bribe_type = detail.bribe_type or "external"
            if bribe_type not in grouped:
                grouped[bribe_type] = []
            grouped[bribe_type].append(detail)
        
        return grouped

    def get_all_pools_in_epoch(self, epoch: int) -> List[str]:
        """Get all unique pool names that had bribes in an epoch."""
        summary = self.get_bribes_for_epoch_detailed(epoch)
        pools = set()
        for detail in summary.bribes:
            if detail.pool_name:
                pools.add(detail.pool_name)
        return sorted(list(pools))

    def save_bribe_with_metadata(
        self,
        epoch: int,
        bribe_contract: str,
        reward_token: str,
        amount_wei: str,
        token_symbol: Optional[str] = None,
        token_decimals: Optional[int] = None,
        amount_human: Optional[float] = None,
    ) -> None:
        """
        Save bribe and ensure token metadata is cached.
        
        Args:
            epoch: Epoch timestamp
            bribe_contract: Bribe contract address
            reward_token: Reward token address
            amount_wei: Amount in wei (smallest unit)
            token_symbol: Token symbol (optional)
            token_decimals: Token decimals (optional)
            amount_human: Amount in human units (optional)
        """
        # Save token metadata if provided
        if token_symbol is not None or token_decimals is not None:
            self.db.save_token_metadata(reward_token, token_symbol, token_decimals)
        
        # Save bribe
        self.db.save_bribe(
            epoch=epoch,
            bribe_contract=bribe_contract,
            reward_token=reward_token,
            amount_wei=amount_wei,
            timestamp=int(datetime.utcnow().timestamp()),
            amount=amount_human,
        )

    def raw_query(self, sql: str, params: Optional[List] = None) -> List[Tuple]:
        """
        Execute raw SQL query (for special cases not covered by high-level API).
        
        Args:
            sql: SQL query string
            params: Optional query parameters
        
        Returns:
            List of result tuples
        """
        with self.db.engine.connect() as conn:
            result = conn.execute(sql, params or [])
            return result.fetchall()
