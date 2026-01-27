"""
Bribe tracker for indexing NotifyReward events from Bribe contracts.
"""

import logging
from typing import Dict, List

from config import Config
from src.database import Database
from src.indexer import HydrexIndexer
from src.price_feed import PriceFeed

logger = logging.getLogger(__name__)


class BribeTracker:
    """Tracks bribe deposits for gauges across epochs."""

    def __init__(self, indexer: HydrexIndexer, database: Database, price_feed: PriceFeed):
        """
        Initialize bribe tracker.

        Args:
            indexer: Blockchain indexer
            database: Database instance
            price_feed: Price feed for token valuations
        """
        self.indexer = indexer
        self.database = database
        self.price_feed = price_feed
        logger.info("Bribe tracker initialized")

    def _get_epoch_from_timestamp(self, timestamp: int) -> int:
        """
        Calculate epoch start timestamp from any timestamp.

        Args:
            timestamp: Unix timestamp

        Returns:
            Epoch start timestamp (Wednesday 00:00 UTC)
        """
        # Calculate days since Unix epoch
        days_since_epoch = timestamp // 86400
        # Wednesday is day 3 (0=Thursday in Unix epoch)
        days_to_wednesday = (days_since_epoch - 3) % 7
        epoch_start_day = days_since_epoch - days_to_wednesday
        return epoch_start_day * 86400

    def index_bribes_for_gauge(
        self, gauge_address: str, from_block: int, to_block: int
    ) -> None:
        """
        Index bribe deposits for a specific gauge.

        Args:
            gauge_address: Gauge contract address
            from_block: Starting block number
            to_block: Ending block number
        """
        gauge = self.database.get_gauge(gauge_address)
        if not gauge:
            logger.warning(f"Gauge {gauge_address} not found in database")
            return

        # Index internal bribes
        if gauge.internal_bribe:
            logger.info(
                f"Indexing internal bribes for {gauge_address} from block {from_block} to {to_block}"
            )
            self._index_bribe_contract(
                gauge_address,
                gauge.internal_bribe,
                "internal",
                from_block,
                to_block,
            )

        # Index external bribes
        if gauge.external_bribe:
            logger.info(
                f"Indexing external bribes for {gauge_address} from block {from_block} to {to_block}"
            )
            self._index_bribe_contract(
                gauge_address,
                gauge.external_bribe,
                "external",
                from_block,
                to_block,
            )

    def _index_bribe_contract(
        self,
        gauge_address: str,
        bribe_address: str,
        bribe_type: str,
        from_block: int,
        to_block: int,
    ) -> None:
        """
        Index NotifyReward events from a bribe contract.

        Args:
            gauge_address: Gauge address
            bribe_address: Bribe contract address
            bribe_type: 'internal' or 'external'
            from_block: Starting block
            to_block: Ending block
        """
        events = self.indexer.fetch_notify_reward_events(
            bribe_address, from_block, to_block
        )

        for event in events:
            # Determine which epoch this bribe belongs to
            epoch = self._get_epoch_from_timestamp(event["timestamp"])

            # Get token price and calculate USD value
            token_address = event["reward"]
            amount = event["amount"]

            # Assume 18 decimals (adjust if needed)
            usd_value = self.price_feed.calculate_bribe_value(
                token_address, amount, decimals=18
            )

            # Store in database
            self.database.save_bribe(
                epoch=epoch,
                gauge=gauge_address,
                bribe_type=bribe_type,
                token=token_address,
                amount=str(amount),
                usd_value=usd_value,
                timestamp=event["timestamp"],
            )

            logger.debug(
                f"Indexed {bribe_type} bribe: {gauge_address}, "
                f"epoch {epoch}, ${usd_value:.2f}"
            )

    def get_total_bribes_for_epoch(self, epoch: int) -> float:
        """
        Calculate total bribes in USD for an epoch.

        Args:
            epoch: Epoch timestamp

        Returns:
            Total bribe value in USD
        """
        bribes = self.database.get_bribes_for_epoch(epoch)
        return sum(bribe.usd_value for bribe in bribes)

    def get_bribes_by_gauge(self, epoch: int) -> Dict[str, float]:
        """
        Get total bribes per gauge for an epoch.

        Args:
            epoch: Epoch timestamp

        Returns:
            Dictionary mapping gauge address to total bribe USD value
        """
        bribes = self.database.get_bribes_for_epoch(epoch)
        gauge_bribes = {}

        for bribe in bribes:
            if bribe.gauge not in gauge_bribes:
                gauge_bribes[bribe.gauge] = 0.0
            gauge_bribes[bribe.gauge] += bribe.usd_value

        return gauge_bribes

    def index_all_gauges(self, from_block: int, to_block: int) -> None:
        """
        Index bribes for all gauges in the database.

        Args:
            from_block: Starting block number
            to_block: Ending block number
        """
        # Try to use subgraph for much faster bribe indexing
        if self.indexer.subgraph_client:
            logger.info(f"Using subgraph to fetch all bribes from blocks {from_block}-{to_block}")
            try:
                self._index_bribes_from_subgraph(from_block, to_block)
                return
            except Exception as e:
                logger.warning(f"Subgraph bribe fetching failed, falling back to RPC: {e}")
        
        # Fallback to RPC (slow)
        gauges = self.database.get_all_gauges(alive_only=True)
        logger.info(f"Indexing bribes via RPC for {len(gauges)} gauges (this may be slow)")

        for i, gauge in enumerate(gauges, 1):
            logger.info(f"Processing gauge {i}/{len(gauges)}: {gauge.address}")
            try:
                self.index_bribes_for_gauge(gauge.address, from_block, to_block)
            except Exception as e:
                logger.error(f"Failed to index bribes for {gauge.address}: {e}")
                continue
    
    def _index_bribes_from_subgraph(self, from_block: int, to_block: int) -> None:
        """
        Fetch all bribes from subgraph (much faster than RPC).
        
        Args:
            from_block: Starting block number
            to_block: Ending block number
        """
        # Fetch all bribes in the block range
        bribes = self.indexer.subgraph_client.fetch_all_paginated(
            self.indexer.subgraph_client.fetch_bribes,
            block_gte=from_block,
            block_lte=to_block
        )
        
        logger.info(f"Fetched {len(bribes)} bribes from subgraph")
        
        # Get gauge mappings (bribe contract -> gauge)
        gauges = self.database.get_all_gauges()
        bribe_to_gauge = {}
        for gauge in gauges:
            if gauge.internal_bribe:
                bribe_to_gauge[gauge.internal_bribe.lower()] = (gauge.address, 'internal')
            if gauge.external_bribe:
                bribe_to_gauge[gauge.external_bribe.lower()] = (gauge.address, 'external')
        
        # Process each bribe
        for bribe_event in bribes:
            bribe_contract = bribe_event['bribeContract'].lower()
            
            # Find which gauge this bribe belongs to
            if bribe_contract not in bribe_to_gauge:
                logger.debug(f"Bribe contract {bribe_contract} not found in gauge mappings")
                continue
            
            gauge_address, bribe_type = bribe_to_gauge[bribe_contract]
            
            # Determine epoch from timestamp
            timestamp = int(bribe_event['blockTimestamp'])
            epoch = self._get_epoch_from_timestamp(timestamp)
            
            # Get token price and calculate USD value
            token_address = bribe_event['rewardToken']
            amount = int(bribe_event['amount'])
            
            usd_value = self.price_feed.calculate_bribe_value(
                token_address, amount, decimals=18
            )
            
            # Store in database
            self.database.save_bribe(
                epoch=epoch,
                gauge=gauge_address,
                bribe_type=bribe_type,
                token=token_address,
                amount=str(amount),
                usd_value=usd_value,
                timestamp=timestamp,
            )
            
            logger.debug(
                f"Indexed {bribe_type} bribe: {gauge_address}, "
                f"epoch {epoch}, ${usd_value:.2f}"
            )
