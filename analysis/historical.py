"""
Historical analysis of past epochs to identify optimal strategies.
"""

import json
import logging
from datetime import datetime
from typing import Dict, List

from rich.console import Console
from rich.table import Table

from config import Config
from src.database import Database
from src.optimizer import VoteOptimizer
from src.price_feed import PriceFeed
from src.subgraph_client import SubgraphClient

logger = logging.getLogger(__name__)
console = Console()


class HistoricalAnalyzer:
    """Analyzes historical voting and bribe data."""

    def __init__(self, database: Database, voting_power: int, price_feed: PriceFeed = None):
        """
        Initialize historical analyzer.

        Args:
            database: Database instance
            voting_power: Your voting power for analysis
            price_feed: Optional PriceFeed instance for token price conversion
        """
        self.database = database
        # Ensure new tables (e.g., historical token prices) exist
        self.database.create_tables()
        self.optimizer = VoteOptimizer(voting_power)
        self.price_feed = price_feed or PriceFeed(Config.COINGECKO_API_KEY, database)
        self.subgraph_client = SubgraphClient(Config.SUBGRAPH_URL) if Config.SUBGRAPH_URL else None
        logger.info("Historical analyzer initialized")

    def _fetch_actual_votes(self, epoch: int, voter: str) -> Dict[str, float]:
        """
        Fetch actual per-gauge votes for a voter in a given epoch.
        
        Returns a dict: gauge_address -> normalized vote amount (wei / 1e18)
        """
        if not self.subgraph_client or not voter:
            return {}

        results = self.subgraph_client.fetch_all_paginated(
            self.subgraph_client.fetch_gauge_votes,
            epoch=epoch,
            voter=voter,
        )

        allocations: Dict[str, float] = {}
        for item in results:
            gauge_addr = item["gauge"]["address"].lower()
            # Subgraph returns weight in wei; convert to normalized amount
            weight_wei = float(item["weight"])
            weight_normalized = weight_wei / 1e18
            allocations[gauge_addr] = allocations.get(gauge_addr, 0) + weight_normalized

        return allocations

    def analyze_epoch(self, epoch: int, actual_voter: str = None) -> Dict[str, any]:
        """
        Analyze a single epoch and calculate optimal vs naive returns.

        Args:
            epoch: Epoch timestamp when claiming rewards (N+1 in the cycle)
                   Bribes from epoch N apply to rewards in epoch N+1

        Returns:
            Analysis results dictionary
        """
        logger.info(f"Analyzing epoch {epoch}")

        # Per contract clarification:
        # - Votes cast in epoch N determine gauge selections
        # - Bribes/fees placed in epoch N apply rewards in epoch N+1
        # - Claims for epoch N available in epoch N+1
        # So if analyzing returns in epoch N+1, look at bribes from epoch N
        prev_epoch = epoch - Config.EPOCH_DURATION
        votes = self.database.get_votes_for_epoch(prev_epoch)
        bribes = self.database.get_bribes_for_epoch(prev_epoch)
        
        logger.info(
            f"Found {len(votes)} votes for epoch {prev_epoch} and {len(bribes)} bribes for epoch {epoch}"
        )

        if not bribes:
            logger.warning(f"No bribes for epoch {epoch}")
            return None

        # Build gauge data - start with all gauges from votes
        gauge_data = {}
        for vote in votes:
            gauge_data[vote.gauge] = {
                "address": vote.gauge,
                "current_votes": vote.total_votes,
                "bribes_usd": 0.0,
                "internal_bribes_usd": 0.0,
                "external_bribes_usd": 0.0,
            }

        # Build bribe contract -> gauge address mapping (cache this lookup)
        logger.info(f"Building bribe contract to gauge mapping...")
        bribe_to_gauge = {}
        bribe_type = {}  # Track whether bribe contract is internal or external

        def _is_valid_bribe(addr: str) -> bool:
            # Some subgraphs may store zero address; skip those to avoid collisions.
            if not addr:
                return False
            addr_lower = addr.lower()
            return addr_lower != "0x0000000000000000000000000000000000000000"

        for gauge in self.database.get_all_gauges():
            if _is_valid_bribe(gauge.internal_bribe):
                addr_lower = gauge.internal_bribe.lower()
                bribe_to_gauge[addr_lower] = gauge.address
                bribe_type[addr_lower] = "internal"
            if _is_valid_bribe(gauge.external_bribe):
                addr_lower = gauge.external_bribe.lower()
                bribe_to_gauge[addr_lower] = gauge.address
                bribe_type[addr_lower] = "external"
            # Initialize all gauges in gauge_data if not already present
            if gauge.address not in gauge_data:
                gauge_data[gauge.address] = {
                    "address": gauge.address,
                    "current_votes": 0,
                    "bribes_usd": 0.0,
                    "internal_bribes_usd": 0.0,
                    "external_bribes_usd": 0.0,
                }
        
        logger.info(f"Mapped {len(bribe_to_gauge)} bribe contracts to gauges")

        # Get all unique reward tokens for batch price fetching
        unique_tokens = list(set(bribe.reward_token for bribe in bribes))
        logger.info(
            f"Fetching historical prices for {len(unique_tokens)} unique tokens at epoch {prev_epoch}..."
        )
        token_prices = self.price_feed.get_batch_prices_for_timestamp(
            unique_tokens, prev_epoch, granularity="hour"
        )
        
        # Track which tokens are missing prices
        missing_token_prices = set(bribe.reward_token.lower() for bribe in bribes 
                                   if bribe.reward_token.lower() not in token_prices)
        
        logger.info(f"Found cached prices for {len(token_prices)}/{len(unique_tokens)} tokens")
        if missing_token_prices:
            logger.warning(
                f"⚠️  Missing prices for {len(missing_token_prices)} tokens at epoch {prev_epoch}: "
                f"{list(missing_token_prices)[:5]}{'...' if len(missing_token_prices) > 5 else ''}"
            )

        # Map bribes to gauges and calculate USD values
        logger.info(f"Processing {len(bribes)} bribe events...")
        bribes_processed = 0
        for i, bribe in enumerate(bribes):
            # Find which gauge this bribe contract belongs to
            bribe_contract_lower = bribe.bribe_contract.lower()
            gauge_addr = bribe_to_gauge.get(bribe_contract_lower)
            
            if gauge_addr and gauge_addr in gauge_data:
                # Convert token amount to USD using fetched price
                token_addr = bribe.reward_token.lower()
                price = token_prices.get(token_addr, 0.0)
                
                # bribe.amount is already in token units (converted from wei in backfill)
                usd_value = bribe.amount * price
                gauge_data[gauge_addr]["bribes_usd"] += usd_value
                
                # Track internal vs external separately
                if bribe_type.get(bribe_contract_lower) == "internal":
                    gauge_data[gauge_addr]["internal_bribes_usd"] += usd_value
                else:
                    gauge_data[gauge_addr]["external_bribes_usd"] += usd_value
                
                bribes_processed += 1
            
            # Progress logging every 500 bribes
            if (i + 1) % 500 == 0:
                logger.info(f"Processed {i + 1}/{len(bribes)} bribes...")
        
        logger.info(f"Successfully mapped {bribes_processed}/{len(bribes)} bribes to gauges")

        gauge_list = list(gauge_data.values())
        
        # Filter out gauges with no bribes
        gauge_list = [g for g in gauge_list if g["bribes_usd"] > 0]
        logger.info(f"Found {len(gauge_list)} gauges with bribes (total: ${sum(g['bribes_usd'] for g in gauge_list):.2f})")
        
        # Debug: show top 5 gauges
        top_5 = sorted(gauge_list, key=lambda x: x["bribes_usd"], reverse=True)[:5]
        for i, g in enumerate(top_5):
            logger.info(f"  Top {i+1}: {g['address'][:10]}... - ${g['bribes_usd']:.2f} bribes, {g['current_votes']:,} votes")

        if not gauge_list:
            logger.warning(f"No gauges with bribes for epoch {epoch}")
            return None

        # Compare strategies
        comparison = self.optimizer.compare_strategies(gauge_list)

        actual_return = None
        if actual_voter:
            # Fetch actual votes from previous epoch (votes from prev_epoch apply to this epoch's bribes)
            actual_votes = self._fetch_actual_votes(prev_epoch, actual_voter)
            if actual_votes:
                total_actual = 0.0
                gauges_with_missing_prices = []
                
                # actual_votes are already normalized (wei / 1e18)
                # Just use them directly against database vote totals
                for gauge in gauge_list:
                    gauge_addr = gauge["address"].lower()
                    your_votes = actual_votes.get(gauge_addr)
                    if not your_votes:
                        continue
                    total_votes = gauge["current_votes"]
                    if total_votes <= 0:
                        continue
                    
                    # Check if this gauge has bribes with missing prices
                    gauge_bribes = [b for b in bribes if bribe_to_gauge.get(b.bribe_contract.lower()) == gauge_addr]
                    gauge_missing_tokens = set(b.reward_token.lower() for b in gauge_bribes 
                                               if b.reward_token.lower() in missing_token_prices)
                    if gauge_missing_tokens:
                        gauges_with_missing_prices.append((gauge_addr, gauge_missing_tokens))
                    
                    your_share = your_votes / total_votes
                    total_actual += gauge["bribes_usd"] * your_share

                actual_return = total_actual
                
                # Warn if actual return calculation used gauges with missing token prices
                if gauges_with_missing_prices:
                    readable_date = datetime.fromtimestamp(next_epoch).strftime("%Y-%m-%d")
                    logger.warning(
                        f"⚠️  Actual return missing prices for epoch {readable_date} ({next_epoch})"
                    )
                    for gauge_addr, tokens in gauges_with_missing_prices[:5]:
                        logger.warning(
                            f"     Gauge {gauge_addr[:10]}... missing tokens: {sorted(tokens)}"
                        )
                    if len(gauges_with_missing_prices) > 5:
                        logger.warning(
                            f"     ... and {len(gauges_with_missing_prices) - 5} more gauges"
                        )

        # Debug: Log optimal allocation details
        if comparison["optimal"]["allocation"]:
            logger.info(f"Optimal allocation breakdown:")
            for addr, votes in sorted(
                comparison["optimal"]["allocation"].items(),
                key=lambda x: x[1],
                reverse=True,
            )[:5]:
                gauge = next((g for g in gauge_list if g["address"] == addr), None)
                if gauge:
                    your_share = votes / (gauge["current_votes"] + votes)
                    return_on_this = gauge["bribes_usd"] * your_share
                    logger.info(
                        f"  {addr[:10]}... alloc={votes:,} votes, "
                        f"gauge_votes={gauge['current_votes']:,}, "
                        f"share={your_share:.2%}, return=${return_on_this:.2f}"
                    )

        # Save analysis
        self.database.save_analysis(
            epoch=epoch,
            optimal_return=comparison["optimal"]["return"],
            naive_return=comparison["naive"]["return"],
            opportunity_cost=comparison["improvement_vs_naive"],
            optimal_allocation=json.dumps(comparison["optimal"]["allocation"]),
        )

        return {
            "epoch": epoch,
            "total_bribes": sum(g["bribes_usd"] for g in gauge_list),
            "internal_bribes": sum(g["internal_bribes_usd"] for g in gauge_list),
            "external_bribes": sum(g["external_bribes_usd"] for g in gauge_list),
            "optimal_return": comparison["optimal"]["return"],
            "naive_return": comparison["naive"]["return"],
            "actual_return": actual_return,
            "opportunity_cost": comparison["improvement_vs_naive"],
            "improvement_pct": comparison["improvement_pct"],
        }

    def analyze_recent_epochs(self, count: int = 12) -> List[Dict[str, any]]:
        """
        Analyze the most recent epochs.

        Args:
            count: Number of epochs to analyze

        Returns:
            List of analysis results
        """
        epochs = self.database.get_recent_epochs(count)
        results = []

        for epoch in epochs:
            try:
                result = self.analyze_epoch(epoch.timestamp)
                if result:
                    results.append(result)
            except Exception as e:
                logger.error(f"Failed to analyze epoch {epoch.timestamp}: {e}")

        return results

    def display_summary(self, results: List[Dict[str, any]]) -> None:
        """
        Display historical analysis summary.

        Args:
            results: List of analysis results
        """
        if not results:
            console.print("[yellow]No historical data to display[/yellow]")
            return

        # Create table
        table = Table(title="Historical Analysis Summary", show_header=True)
        table.add_column("Epoch", style="cyan")
        table.add_column("Date", style="cyan")
        table.add_column("Protocol Fees", justify="right", style="blue")
        table.add_column("External Bribes", justify="right", style="magenta")
        table.add_column("Optimal Return", justify="right", style="green")
        table.add_column("Naive Return", justify="right", style="yellow")
        table.add_column("Opportunity Cost", justify="right", style="red")
        table.add_column("Improvement", justify="right", style="magenta")

        total_optimal = 0.0
        total_naive = 0.0
        total_protocol_fees = 0.0
        total_external_bribes = 0.0

        for result in sorted(results, key=lambda x: x["epoch"], reverse=True):
            # Convert epoch timestamp to readable date
            date_str = datetime.fromtimestamp(result["epoch"]).strftime("%Y-%m-%d")
            
            table.add_row(
                str(result["epoch"]),
                date_str,
                f"${result['total_bribes']:,.2f}",
                f"${result['external_bribes']:,.2f}",
                f"${result['optimal_return']:,.2f}",
                f"${result['naive_return']:,.2f}",
                f"${result['opportunity_cost']:,.2f}",
                f"+{result['improvement_pct']:.1f}%",
            )
            total_optimal += result["optimal_return"]
            total_naive += result["naive_return"]
            total_protocol_fees += result["total_bribes"]
            total_external_bribes += result["external_bribes"]

        console.print(table)

        # Summary statistics
        console.print("\n[bold]Summary Statistics:[/bold]")
        console.print(f"Total Epochs Analyzed: {len(results)}")
        console.print(f"Total Protocol Fees (Bribes): ${total_protocol_fees:,.2f}")
        console.print(f"Total External Bribes: ${total_external_bribes:,.2f}")
        console.print(f"Total Optimal Returns: ${total_optimal:,.2f}")
        console.print(f"Total Naive Returns: ${total_naive:,.2f}")
        console.print(f"Total Opportunity Cost: ${total_optimal - total_naive:,.2f}")

        if total_naive > 0:
            avg_improvement = (total_optimal - total_naive) / total_naive * 100
            console.print(f"Average Improvement: +{avg_improvement:.1f}%")
