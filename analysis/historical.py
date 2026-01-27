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
        self.optimizer = VoteOptimizer(voting_power)
        self.price_feed = price_feed or PriceFeed(Config.COINGECKO_API_KEY, database)
        logger.info("Historical analyzer initialized")

    def analyze_epoch(self, epoch: int) -> Dict[str, any]:
        """
        Analyze a single epoch and calculate optimal vs naive returns.

        Args:
            epoch: Epoch timestamp

        Returns:
            Analysis results dictionary
        """
        logger.info(f"Analyzing epoch {epoch}")

        # Get votes and bribes for this epoch
        votes = self.database.get_votes_for_epoch(epoch)
        bribes = self.database.get_bribes_for_epoch(epoch)
        
        logger.info(f"Found {len(votes)} votes and {len(bribes)} bribes for epoch {epoch}")

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
            }

        # Build bribe contract -> gauge address mapping (cache this lookup)
        logger.info(f"Building bribe contract to gauge mapping...")
        bribe_to_gauge = {}
        for gauge in self.database.get_all_gauges():
            bribe_to_gauge[gauge.internal_bribe.lower()] = gauge.address
            bribe_to_gauge[gauge.external_bribe.lower()] = gauge.address
            # Initialize all gauges in gauge_data if not already present
            if gauge.address not in gauge_data:
                gauge_data[gauge.address] = {
                    "address": gauge.address,
                    "current_votes": 0,
                    "bribes_usd": 0.0,
                }
        
        logger.info(f"Mapped {len(bribe_to_gauge)} bribe contracts to gauges")

        # Get all unique reward tokens for batch price fetching
        unique_tokens = list(set(bribe.reward_token for bribe in bribes))
        logger.info(f"Fetching prices for {len(unique_tokens)} unique tokens...")
        token_prices = self.price_feed.get_batch_prices(unique_tokens)
        
        logger.info(f"Fetched prices for {len(token_prices)}/{len(unique_tokens)} tokens")

        # Map bribes to gauges and calculate USD values
        logger.info(f"Processing {len(bribes)} bribe events...")
        bribes_processed = 0
        for i, bribe in enumerate(bribes):
            # Find which gauge this bribe contract belongs to
            gauge_addr = bribe_to_gauge.get(bribe.bribe_contract.lower())
            
            if gauge_addr and gauge_addr in gauge_data:
                # Convert token amount to USD using fetched price
                token_addr = bribe.reward_token.lower()
                price = token_prices.get(token_addr, 0.0)
                
                # bribe.amount is already in token units (converted from wei in backfill)
                usd_value = bribe.amount * price
                gauge_data[gauge_addr]["bribes_usd"] += usd_value
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
            "optimal_return": comparison["optimal"]["return"],
            "naive_return": comparison["naive"]["return"],
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
        table.add_column("Optimal Return", justify="right", style="green")
        table.add_column("Naive Return", justify="right", style="yellow")
        table.add_column("Opportunity Cost", justify="right", style="red")
        table.add_column("Improvement", justify="right", style="magenta")

        total_optimal = 0.0
        total_naive = 0.0
        total_protocol_fees = 0.0

        for result in sorted(results, key=lambda x: x["epoch"], reverse=True):
            # Convert epoch timestamp to readable date
            date_str = datetime.fromtimestamp(result["epoch"]).strftime("%Y-%m-%d")
            
            table.add_row(
                str(result["epoch"]),
                date_str,
                f"${result['total_bribes']:,.2f}",
                f"${result['optimal_return']:,.2f}",
                f"${result['naive_return']:,.2f}",
                f"${result['opportunity_cost']:,.2f}",
                f"+{result['improvement_pct']:.1f}%",
            )
            total_optimal += result["optimal_return"]
            total_naive += result["naive_return"]
            total_protocol_fees += result["total_bribes"]

        console.print(table)

        # Summary statistics
        console.print("\n[bold]Summary Statistics:[/bold]")
        console.print(f"Total Epochs Analyzed: {len(results)}")
        console.print(f"Total Protocol Fees (Bribes): ${total_protocol_fees:,.2f}")
        console.print(f"Total Optimal Returns: ${total_optimal:,.2f}")
        console.print(f"Total Naive Returns: ${total_naive:,.2f}")
        console.print(f"Total Opportunity Cost: ${total_optimal - total_naive:,.2f}")

        if total_naive > 0:
            avg_improvement = (total_optimal - total_naive) / total_naive * 100
            console.print(f"Average Improvement: +{avg_improvement:.1f}%")
