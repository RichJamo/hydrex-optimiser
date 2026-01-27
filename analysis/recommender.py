"""
Vote recommendation generator for current epoch.
"""

import json
import logging
from typing import Dict, Optional

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from config import Config
from src.database import Database
from src.indexer import HydrexIndexer
from src.optimizer import VoteOptimizer
from src.utils import format_percentage, format_usd, time_until

logger = logging.getLogger(__name__)
console = Console()


class VoteRecommender:
    """Generates vote recommendations for current epoch."""

    def __init__(
        self, indexer: HydrexIndexer, database: Database, voting_power: int
    ):
        """
        Initialize vote recommender.

        Args:
            indexer: Blockchain indexer
            database: Database instance
            voting_power: Your voting power
        """
        self.indexer = indexer
        self.database = database
        self.optimizer = VoteOptimizer(voting_power)
        logger.info("Vote recommender initialized")

    def get_current_epoch(self) -> int:
        """Get current epoch timestamp."""
        return Config.get_current_epoch_timestamp()

    def generate_recommendation(self) -> Dict[str, any]:
        """
        Generate optimal vote allocation recommendation for current epoch.

        Returns:
            Dictionary with recommendation details
        """
        epoch = self.get_current_epoch()
        logger.info(f"Generating recommendation for epoch {epoch}")

        # Get all gauges
        gauges = self.database.get_all_gauges(alive_only=True)

        # Build gauge data with current votes and bribes
        gauge_data = []
        for gauge in gauges:
            # Get current votes from blockchain
            votes = self.indexer.get_gauge_weight(gauge.address)

            # Get bribes for this epoch
            bribes = self.database.get_bribes_for_gauge(epoch, gauge.address)
            total_bribes = sum(b.usd_value for b in bribes)

            if total_bribes > 0:  # Only include gauges with bribes
                gauge_data.append(
                    {
                        "address": gauge.address,
                        "pool": gauge.pool,
                        "current_votes": votes,
                        "bribes_usd": total_bribes,
                    }
                )

        if not gauge_data:
            logger.warning("No gauges with bribes found")
            return None

        # Get optimal allocation
        optimal_allocation = self.optimizer.quadratic_optimization(gauge_data)
        total_return = self.optimizer.calculate_total_return(
            gauge_data, optimal_allocation
        )

        # Get naive allocation for comparison
        comparison = self.optimizer.compare_strategies(gauge_data)

        # Build detailed recommendation
        recommendations = []
        for address, votes in optimal_allocation.items():
            gauge_info = next(g for g in gauge_data if g["address"] == address)
            expected_return = self.optimizer.calculate_expected_return(
                gauge_info["current_votes"], votes, gauge_info["bribes_usd"]
            )

            recommendations.append(
                {
                    "address": address,
                    "pool": gauge_info["pool"],
                    "votes": votes,
                    "percentage": (votes / self.optimizer.voting_power) * 100,
                    "expected_return": expected_return,
                    "current_votes": gauge_info["current_votes"],
                    "total_bribes": gauge_info["bribes_usd"],
                }
            )

        # Sort by expected return
        recommendations.sort(key=lambda x: x["expected_return"], reverse=True)

        return {
            "epoch": epoch,
            "recommendations": recommendations,
            "total_expected_return": total_return,
            "naive_return": comparison["naive"]["return"],
            "opportunity_cost": comparison["improvement_vs_naive"],
            "improvement_pct": comparison["improvement_pct"],
        }

    def display_recommendation(
        self, recommendation: Dict[str, any], format: str = "rich"
    ) -> Optional[str]:
        """
        Display vote recommendation.

        Args:
            recommendation: Recommendation dictionary
            format: Output format ('rich' or 'json')

        Returns:
            JSON string if format='json', None otherwise
        """
        if format == "json":
            return json.dumps(recommendation, indent=2)

        # Rich console display
        epoch = recommendation["epoch"]
        epoch_end = epoch + Config.EPOCH_DURATION

        # Title
        console.print("\n" + "━" * 60)
        console.print(
            f"  [bold cyan]OPTIMAL VOTE ALLOCATION[/bold cyan] - Epoch {epoch}"
        )
        console.print("━" * 60 + "\n")

        # Create recommendations table
        table = Table(show_header=True, header_style="bold magenta")
        table.add_column("Pool", style="cyan", width=30)
        table.add_column("Votes", justify="right", style="yellow")
        table.add_column("%", justify="right", style="blue")
        table.add_column("Expected Return", justify="right", style="green")

        for rec in recommendation["recommendations"]:
            table.add_row(
                rec["pool"][:28] + "..." if len(rec["pool"]) > 30 else rec["pool"],
                f"{rec['votes']:,}",
                format_percentage(rec["percentage"]),
                format_usd(rec["expected_return"]),
            )

        console.print(table)

        # Summary
        console.print("\n" + "━" * 60)
        console.print(
            f"[bold]Total Expected Return:[/bold] {format_usd(recommendation['total_expected_return'])}"
        )
        console.print(
            f"[bold]Opportunity Cost vs Naive:[/bold] {format_usd(recommendation['opportunity_cost'])} "
            f"(+{format_percentage(recommendation['improvement_pct'])})"
        )
        console.print("━" * 60)

        # Voting window warning
        if Config.is_in_safe_voting_window():
            console.print(
                f"\n✅ [green]Safe to vote now[/green] (Epoch ends in {time_until(epoch_end)})"
            )
        else:
            console.print(
                f"\n⚠️ [yellow]Outside safe voting window[/yellow] (Wait until Saturday 18:00 UTC)"
            )

        console.print()
