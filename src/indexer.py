"""
Blockchain indexer for fetching Hydrex data from Base.
Supports both RPC and Subgraph data sources.
"""

import logging
from typing import Dict, List, Optional

from web3 import Web3
from web3.contract import Contract

from config import BRIBE_ABI, VOTER_ABI, Config
from src.utils import retry

logger = logging.getLogger(__name__)


class HydrexIndexer:
    """Indexes Hydrex VoterV5 contract data from Base blockchain."""

    def __init__(self, rpc_url: str, voter_address: str):
        """
        Initialize indexer with Web3 connection.

        Args:
            rpc_url: Base RPC endpoint
            voter_address: VoterV5 contract address
        """
        self.w3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": Config.RPC_TIMEOUT}))
        self.voter_address = Web3.to_checksum_address(voter_address)
        self.voter_contract: Contract = self.w3.eth.contract(
            address=self.voter_address, abi=VOTER_ABI
        )
        
        # Initialize subgraph client if configured
        self.subgraph_client = None
        if Config.SUBGRAPH_URL:
            try:
                from src.subgraph_client import SubgraphClient
                self.subgraph_client = SubgraphClient()
                logger.info(f"Subgraph client initialized: {Config.SUBGRAPH_URL}")
            except Exception as e:
                logger.warning(f"Failed to initialize subgraph client: {e}")
        
        logger.info(f"Indexer initialized for VoterV5: {self.voter_address}")
        logger.info(f"Data source: {'Subgraph + RPC' if self.subgraph_client else 'RPC only'}")

    @retry(max_attempts=3, delay=2.0)
    def get_latest_block(self) -> int:
        """
        Get latest block number.

        Returns:
            Latest block number
        """
        return self.w3.eth.block_number

    @retry(max_attempts=3, delay=2.0)
    def get_block_timestamp(self, block_number: int) -> int:
        """
        Get timestamp for a specific block.

        Args:
            block_number: Block number

        Returns:
            Unix timestamp
        """
        block = self.w3.eth.get_block(block_number)
        return int(block["timestamp"])

    @retry(max_attempts=3, delay=2.0)
    def get_gauge_info(self, gauge_address: str) -> Dict[str, any]:
        """
        Get gauge information from VoterV5 contract.

        Args:
            gauge_address: Gauge contract address

        Returns:
            Dictionary with gauge info: pool, internal_bribe, external_bribe, is_alive
        """
        gauge_address = Web3.to_checksum_address(gauge_address)

        try:
            pool = self.voter_contract.functions.poolForGauge(gauge_address).call()
            internal_bribe = self.voter_contract.functions.internal_bribes(
                gauge_address
            ).call()
            external_bribe = self.voter_contract.functions.external_bribes(
                gauge_address
            ).call()
            is_alive = self.voter_contract.functions.isAlive(gauge_address).call()

            return {
                "address": gauge_address,
                "pool": pool,
                "internal_bribe": internal_bribe,
                "external_bribe": external_bribe,
                "is_alive": is_alive,
            }
        except Exception as e:
            logger.error(f"Failed to get gauge info for {gauge_address}: {e}")
            raise

    @retry(max_attempts=3, delay=2.0)
    def get_gauge_weight(self, gauge_address: str) -> int:
        """
        Get current voting weight for a gauge.

        Args:
            gauge_address: Gauge contract address

        Returns:
            Total votes on gauge
        """
        gauge_address = Web3.to_checksum_address(gauge_address)
        try:
            return self.voter_contract.functions.weights(gauge_address).call()
        except Exception as e:
            logger.error(f"Failed to get gauge weight for {gauge_address}: {e}")
            return 0

    def fetch_gauge_created_events(
        self, from_block: int, to_block: Optional[int] = None, chunk_size: int = 1000
    ) -> List[Dict]:
        """
        Fetch GaugeCreated events from VoterV5 contract.
        Uses subgraph if available, otherwise falls back to RPC.

        Args:
            from_block: Starting block number
            to_block: Ending block number (latest if None)
            chunk_size: Number of blocks to query at once (RPC only)

        Returns:
            List of gauge created events
        """
        if to_block is None:
            to_block = self.get_latest_block()

        # Try subgraph first
        if self.subgraph_client:
            try:
                logger.info(f"Fetching GaugeCreated events from subgraph (blocks {from_block}-{to_block})")
                gauges = self.subgraph_client.fetch_all_paginated(
                    self.subgraph_client.fetch_gauges,
                    block_gte=from_block,
                    block_lte=to_block
                )
                
                # Only use subgraph results if we got data
                if gauges:
                    # Convert subgraph format to expected format
                    results = []
                    for g in gauges:
                        results.append({
                            "gauge": g["address"],
                            "creator": g["creator"],
                            "internal_bribe": g["internalBribe"],
                            "external_bribe": g["externalBribe"],
                            "pool": g.get("pool", g["address"]),  # Use gauge address as fallback
                            "block_number": int(g["blockNumber"]),
                            "block_timestamp": int(g["blockTimestamp"]),
                            "transaction_hash": g["transactionHash"],
                        })
                    
                    logger.info(f"Fetched {len(results)} GaugeCreated events from subgraph")
                    return results
                else:
                    logger.info("Subgraph returned no data, falling back to RPC")
                
            except Exception as e:
                logger.warning(f"Subgraph query failed, falling back to RPC: {e}")
        
        # Fallback to RPC
        logger.info(f"Fetching GaugeCreated events via RPC from {from_block} to {to_block}")

        results = []
        current_from = from_block

        while current_from <= to_block:
            current_to = min(current_from + chunk_size - 1, to_block)
            
            try:
                logger.debug(f"Querying blocks {current_from} to {current_to}")
                events = self.voter_contract.events.GaugeCreated.get_logs(
                    fromBlock=current_from, toBlock=current_to
                )

                for event in events:
                    results.append(
                        {
                            "gauge": event["args"]["gauge"],
                            "creator": event["args"]["creator"],
                            "internal_bribe": event["args"]["internal_bribe"],
                            "external_bribe": event["args"]["external_bribe"],
                            "block_number": event["blockNumber"],
                            "tx_hash": event["transactionHash"].hex(),
                        }
                    )

            except Exception as e:
                logger.error(f"Failed to fetch events for blocks {current_from}-{current_to}: {e}")

            current_from = current_to + 1

        logger.info(f"Found {len(results)} GaugeCreated events")
        return results

    def fetch_voted_events(
        self, from_block: int, to_block: Optional[int] = None, chunk_size: int = 1000
    ) -> List[Dict]:
        """
        Fetch Voted events from VoterV5 contract.
        Uses subgraph if available, otherwise falls back to RPC.

        Args:
            from_block: Starting block number
            to_block: Ending block number (latest if None)
            chunk_size: Number of blocks to query at once (RPC only)

        Returns:
            List of voted events
        """
        if to_block is None:
            to_block = self.get_latest_block()

        # Try subgraph first
        if self.subgraph_client:
            try:
                logger.info(f"Fetching Voted events from subgraph (blocks {from_block}-{to_block})")
                votes = self.subgraph_client.fetch_all_paginated(
                    self.subgraph_client.fetch_votes,
                    block_gte=from_block,
                    block_lte=to_block
                )
                
                # Only use subgraph results if we got data
                if votes:
                    # Convert subgraph format to expected format
                    results = []
                    for v in votes:
                        results.append({
                            "voter": v["voter"],
                            "weight": int(v["weight"]),
                            "block_number": int(v["blockNumber"]),
                            "block_timestamp": int(v["blockTimestamp"]),
                            "tx_hash": v["transactionHash"],
                        })
                    
                    logger.info(f"Fetched {len(results)} Voted events from subgraph")
                    return results
                else:
                    logger.info("Subgraph returned no vote data, falling back to RPC")
                
            except Exception as e:
                logger.warning(f"Subgraph vote query failed, falling back to RPC: {e}")
                return results
                
            except Exception as e:
                logger.warning(f"Subgraph query failed, falling back to RPC: {e}")
        
        # Fallback to RPC
        logger.info(f"Fetching Voted events via RPC from {from_block} to {to_block}")


        logger.info(f"Fetching Voted events from {from_block} to {to_block}")

        results = []
        current_from = from_block

        while current_from <= to_block:
            current_to = min(current_from + chunk_size - 1, to_block)
            
            try:
                logger.debug(f"Querying blocks {current_from} to {current_to}")
                events = self.voter_contract.events.Voted.get_logs(
                    fromBlock=current_from, toBlock=current_to
                )

                for event in events:
                    results.append(
                        {
                            "voter": event["args"]["voter"],
                            "pool": event["args"]["pool"],
                            "weight": event["args"]["weight"],
                            "block_number": event["blockNumber"],
                            "tx_hash": event["transactionHash"].hex(),
                        }
                    )

            except Exception as e:
                logger.error(f"Failed to fetch events for blocks {current_from}-{current_to}: {e}")

            current_from = current_to + 1

        logger.info(f"Found {len(results)} Voted events")
        return results

    def fetch_notify_reward_events(
        self, bribe_address: str, from_block: int, to_block: Optional[int] = None, chunk_size: int = 1000
    ) -> List[Dict]:
        """
        Fetch NotifyReward events from a Bribe contract in chunks.

        Args:
            bribe_address: Bribe contract address
            from_block: Starting block number
            to_block: Ending block number (latest if None)
            chunk_size: Number of blocks to query at once

        Returns:
            List of notify reward events
        """
        if to_block is None:
            to_block = self.get_latest_block()

        bribe_address = Web3.to_checksum_address(bribe_address)
        bribe_contract = self.w3.eth.contract(address=bribe_address, abi=BRIBE_ABI)

        logger.debug(
            f"Fetching NotifyReward events for {bribe_address} from {from_block} to {to_block}"
        )

        results = []
        current_from = from_block

        while current_from <= to_block:
            current_to = min(current_from + chunk_size - 1, to_block)
            
            try:
                events = bribe_contract.events.NotifyReward.get_logs(
                    fromBlock=current_from, toBlock=current_to
                )

                for event in events:
                    block = self.w3.eth.get_block(event["blockNumber"])
                    results.append(
                        {
                            "from": event["args"]["from"],
                            "reward": event["args"]["reward"],
                            "amount": event["args"]["amount"],
                            "block_number": event["blockNumber"],
                            "timestamp": int(block["timestamp"]),
                            "tx_hash": event["transactionHash"].hex(),
                        }
                    )

            except Exception as e:
                logger.error(
                    f"Failed to fetch NotifyReward events for {bribe_address} blocks {current_from}-{current_to}: {e}"
                )

            current_from = current_to + 1

        logger.debug(f"Found {len(results)} NotifyReward events for {bribe_address}")
        return results
