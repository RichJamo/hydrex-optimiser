"""
Goldsky Subgraph client for fetching Hydrex data.
Uses GraphQL queries instead of RPC calls for better performance.
"""

import logging
from typing import Optional

import requests

from config import Config

logger = logging.getLogger(__name__)


class SubgraphClient:
    """Client for querying Hydrex Goldsky subgraph."""

    def __init__(self, subgraph_url: Optional[str] = None):
        """
        Initialize subgraph client.

        Args:
            subgraph_url: Goldsky subgraph endpoint URL
        """
        self.url = subgraph_url or Config.SUBGRAPH_URL
        if not self.url:
            raise ValueError("SUBGRAPH_URL not configured")

    def query(self, query: str, variables: Optional[dict] = None) -> dict:
        """
        Execute a GraphQL query.

        Args:
            query: GraphQL query string
            variables: Query variables

        Returns:
            Query response data

        Raises:
            Exception: If query fails
        """
        try:
            response = requests.post(
                self.url,
                json={"query": query, "variables": variables or {}},
                timeout=30,
            )
            response.raise_for_status()
            result = response.json()

            if "errors" in result:
                raise Exception(f"GraphQL errors: {result['errors']}")

            return result.get("data", {})

        except Exception as e:
            logger.error(f"Subgraph query failed: {e}")
            raise

    def fetch_gauges(
        self, 
        block_gte: Optional[int] = None,
        block_lte: Optional[int] = None,
        first: int = 1000,
        skip: int = 0
    ) -> list[dict]:
        """
        Fetch gauge creation events.

        Args:
            block_gte: Minimum block number
            block_lte: Maximum block number
            first: Number of results to fetch
            skip: Number of results to skip

        Returns:
            List of gauge dictionaries
        """
        # Build where clause dynamically
        where_conditions = []
        if block_gte is not None:
            where_conditions.append(f"blockNumber_gte: $blockGte")
        if block_lte is not None:
            where_conditions.append(f"blockNumber_lte: $blockLte")
        
        where_clause = f"where: {{ {', '.join(where_conditions)} }}" if where_conditions else ""
        
        query = f"""
        query GetGauges($first: Int!, $skip: Int!{', $blockGte: BigInt' if block_gte is not None else ''}{', $blockLte: BigInt' if block_lte is not None else ''}) {{
          gauges(
            first: $first
            skip: $skip
            {where_clause}
            orderBy: blockNumber
            orderDirection: asc
          ) {{
            id
            address
            pool
            creator
            internalBribe
            externalBribe
            isAlive
            blockNumber
            blockTimestamp
            transactionHash
          }}
        }}
        """

        variables = {
            "first": first,
            "skip": skip,
        }
        if block_gte is not None:
            variables["blockGte"] = str(block_gte)
        if block_lte is not None:
            variables["blockLte"] = str(block_lte)

        result = self.query(query, variables)
        return result.get("gauges", [])

    def fetch_votes(
        self,
        voter: Optional[str] = None,
        block_gte: Optional[int] = None,
        block_lte: Optional[int] = None,
        first: int = 1000,
        skip: int = 0
    ) -> list[dict]:
        """
        Fetch voting events.

        Args:
            voter: Filter by voter address
            block_gte: Minimum block number
            block_lte: Maximum block number
            first: Number of results to fetch
            skip: Number of results to skip

        Returns:
            List of vote dictionaries
        """
        # Build where clause dynamically
        where_conditions = []
        if voter is not None:
            where_conditions.append(f"voter: $voter")
        if block_gte is not None:
            where_conditions.append(f"blockNumber_gte: $blockGte")
        if block_lte is not None:
            where_conditions.append(f"blockNumber_lte: $blockLte")
        
        where_clause = f"where: {{ {', '.join(where_conditions)} }}" if where_conditions else ""
        
        query = f"""
        query GetVotes($first: Int!, $skip: Int!{', $voter: Bytes' if voter is not None else ''}{', $blockGte: BigInt' if block_gte is not None else ''}{', $blockLte: BigInt' if block_lte is not None else ''}) {{
          votes(
            first: $first
            skip: $skip
            {where_clause}
            orderBy: blockNumber
            orderDirection: asc
          ) {{
            id
            voter
            weight
            blockNumber
            blockTimestamp
            transactionHash
          }}
        }}
        """

        variables = {
            "first": first,
            "skip": skip,
        }
        if voter is not None:
            variables["voter"] = voter.lower()
        if block_gte is not None:
            variables["blockGte"] = str(block_gte)
        if block_lte is not None:
            variables["blockLte"] = str(block_lte)

        result = self.query(query, variables)
        return result.get("votes", [])

    def fetch_bribes(
        self,
        epoch: Optional[int] = None,
        bribe_contract: Optional[str] = None,
        block_gte: Optional[int] = None,
        block_lte: Optional[int] = None,
        first: int = 1000,
        skip: int = 0
    ) -> list[dict]:
        """
        Fetch bribe reward events (RewardAdded from internal/external bribe contracts).

        Args:
            epoch: Filter by epoch timestamp
            bribe_contract: Filter by bribe contract address
            block_gte: Minimum block number
            block_lte: Maximum block number
            first: Number of results to fetch
            skip: Number of results to skip

        Returns:
            List of bribe dictionaries with epoch, bribeContract, rewardToken, amount
        """
        # Build where clause dynamically
        where_conditions = []
        if epoch is not None:
            where_conditions.append(f"epoch: $epoch")
        if bribe_contract is not None:
            where_conditions.append(f"bribeContract: $bribeContract")
        if block_gte is not None:
            where_conditions.append(f"blockNumber_gte: $blockGte")
        if block_lte is not None:
            where_conditions.append(f"blockNumber_lte: $blockLte")
        
        where_clause = f"where: {{ {', '.join(where_conditions)} }}" if where_conditions else ""
        
        query = f"""
        query GetBribes($first: Int!, $skip: Int!{', $epoch: BigInt' if epoch is not None else ''}{', $bribeContract: Bytes' if bribe_contract is not None else ''}{', $blockGte: BigInt' if block_gte is not None else ''}{', $blockLte: BigInt' if block_lte is not None else ''}) {{
          bribes(
            first: $first
            skip: $skip
            {where_clause}
            orderBy: blockNumber
            orderDirection: asc
          ) {{
            id
            epoch
            bribeContract
            rewardToken
            amount
            blockNumber
            blockTimestamp
            transactionHash
          }}
        }}
        """

        variables = {
            "first": first,
            "skip": skip,
        }
        if epoch is not None:
            variables["epoch"] = str(epoch)
        if bribe_contract is not None:
            variables["bribeContract"] = bribe_contract.lower()
        if block_gte is not None:
            variables["blockGte"] = str(block_gte)
        if block_lte is not None:
            variables["blockLte"] = str(block_lte)

        result = self.query(query, variables)
        return result.get("bribes", [])

    def fetch_gauge_votes(
        self,
        epoch: Optional[int] = None,
        gauge: Optional[str] = None,
        voter: Optional[str] = None,
        block_gte: Optional[int] = None,
        block_lte: Optional[int] = None,
        first: int = 1000,
        skip: int = 0
    ) -> list[dict]:
        """
        Fetch per-gauge voting data (requires GaugeVote entity in subgraph).

        Args:
            epoch: Filter by epoch timestamp
            gauge: Filter by gauge address
            voter: Filter by voter address
            block_gte: Minimum block number
            block_lte: Maximum block number
            first: Number of results to fetch
            skip: Number of results to skip

        Returns:
            List of gauge vote dictionaries
        """
        # Build where clause dynamically
        where_conditions = []
        if epoch is not None:
            where_conditions.append(f"epoch: $epoch")
        if gauge is not None:
            where_conditions.append(f"gauge: $gauge")
        if voter is not None:
            where_conditions.append(f"voter: $voter")
        if block_gte is not None:
            where_conditions.append(f"blockNumber_gte: $blockGte")
        if block_lte is not None:
            where_conditions.append(f"blockNumber_lte: $blockLte")
        
        where_clause = f"where: {{ {', '.join(where_conditions)} }}" if where_conditions else ""
        
        query = f"""
        query GetGaugeVotes($first: Int!, $skip: Int!{', $epoch: BigInt' if epoch is not None else ''}{', $gauge: String' if gauge is not None else ''}{', $voter: Bytes' if voter is not None else ''}{', $blockGte: BigInt' if block_gte is not None else ''}{', $blockLte: BigInt' if block_lte is not None else ''}) {{
          gaugeVotes(
            first: $first
            skip: $skip
            {where_clause}
            orderBy: blockNumber
            orderDirection: asc
          ) {{
            id
            epoch
            gauge {{
              id
              address
              pool
            }}
            voter
            weight
            blockNumber
            blockTimestamp
            transactionHash
          }}
        }}
        """

        variables = {
            "first": first,
            "skip": skip,
        }
        if epoch is not None:
            variables["epoch"] = str(epoch)
        if gauge is not None:
            variables["gauge"] = gauge.lower()
        if voter is not None:
            variables["voter"] = voter.lower()
        if block_gte is not None:
            variables["blockGte"] = str(block_gte)
        if block_lte is not None:
            variables["blockLte"] = str(block_lte)

        result = self.query(query, variables)
        return result.get("gaugeVotes", [])

    def fetch_all_paginated(
        self, 
        fetch_func,
        page_size: int = 1000,
        **kwargs
    ) -> list[dict]:
        """
        Fetch all results with automatic pagination.

        Args:
            fetch_func: Function to call for each page
            page_size: Results per page
            **kwargs: Arguments to pass to fetch_func

        Returns:
            All results combined
        """
        all_results = []
        skip = 0

        while True:
            results = fetch_func(first=page_size, skip=skip, **kwargs)
            
            if not results:
                break

            all_results.extend(results)
            skip += page_size

            # Stop if we got fewer results than requested (last page)
            if len(results) < page_size:
                break

            logger.info(f"Fetched {len(all_results)} results so far...")

        return all_results

    def fetch_token_hour_data(
        self,
        token_addresses: list[str],
        period_start_unix: int,
        first: int = 1000,
        skip: int = 0,
    ) -> list[dict]:
        """
        Fetch hourly token price data for a set of tokens at a specific hour.

        Args:
            token_addresses: List of token addresses
            period_start_unix: Hour start unix timestamp
            first: Number of results to fetch
            skip: Number of results to skip

        Returns:
            List of tokenHourData dictionaries
        """
        query = """
        query GetTokenHourData($first: Int!, $skip: Int!, $tokens: [String!], $periodStart: Int!) {
          tokenHourDatas(
            first: $first
            skip: $skip
            where: { token_in: $tokens, periodStartUnix: $periodStart }
          ) {
            token {
              id
            }
            periodStartUnix
            priceUSD
            open
            high
            low
            close
          }
        }
        """

        variables = {
            "first": first,
            "skip": skip,
            "tokens": [addr.lower() for addr in token_addresses],
            "periodStart": period_start_unix,
        }

        result = self.query(query, variables)
        return result.get("tokenHourDatas", [])

    def fetch_token_day_data(
        self,
        token_addresses: list[str],
        date_unix: int,
        first: int = 1000,
        skip: int = 0,
    ) -> list[dict]:
        """
        Fetch daily token price data for a set of tokens at a specific day.

        Args:
            token_addresses: List of token addresses
            date_unix: Day start unix timestamp
            first: Number of results to fetch
            skip: Number of results to skip

        Returns:
            List of tokenDayData dictionaries
        """
        query = """
        query GetTokenDayData($first: Int!, $skip: Int!, $tokens: [String!], $date: Int!) {
          tokenDayDatas(
            first: $first
            skip: $skip
            where: { token_in: $tokens, date: $date }
          ) {
            token {
              id
            }
            date
            priceUSD
            open
            high
            low
            close
          }
        }
        """

        variables = {
            "first": first,
            "skip": skip,
            "tokens": [addr.lower() for addr in token_addresses],
            "date": date_unix,
        }

        result = self.query(query, variables)
        return result.get("tokenDayDatas", [])
