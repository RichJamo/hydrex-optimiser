"""
Vote recommendation generator for current epoch.
"""

import json
import logging
import sqlite3
from typing import Dict, Optional

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from config import Config
from src.database import Database
from src.indexer import HydrexIndexer
from src.optimizer import VoteOptimizer, expected_return_usd
from src.utils import format_percentage, format_usd, time_until

logger = logging.getLogger(__name__)
console = Console()


def _competition_multiplier(votes: float) -> float:
    """Conservative p75 boundary/T-1 vote ratio by vote tier.

    Calibrated from 2,440 (epoch, gauge) observations across 31 epochs
    (Apr 2025 – Apr 2026).  All tiers ≥100k show ratio=1.0 in practice
    — Hydrex gauge votes are sticky and don’t build up near the boundary.
    The <100k tier reflects rebalancing noise in small pools (p75=1.21).
    Override HIGH_COMPETITION_VOTES_THRESHOLD/HIGH_COMPETITION_VOTE_CAP_RATIO
    in .env rather than tuning this function for competition dampening.
    """
    if votes < 100_000:
        return 1.20
    return 1.0


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

        # Pre-compute historical ROI/1k per gauge from the last 7 executed epochs.
        # This is passed to the optimizer so it can apply the ROI floor without a
        # separate DB round-trip inside the hot path.
        hist_roi = self._historical_roi_per_1k(n_epochs=7)

        # Build gauge data with current votes and bribes
        gauge_data = []
        for gauge in gauges:
            # Get current votes from blockchain (live weight)
            votes = self.indexer.get_gauge_weight(gauge.address)

            # Apply competition multiplier to current_votes so the optimizer
            # sees a conservatively adjusted competition level.
            adjusted_votes = votes * _competition_multiplier(votes)

            # Get bribes for this epoch
            bribes = self.database.get_bribes_for_gauge(epoch, gauge.address)
            total_bribes = sum(b.usd_value for b in bribes)

            if total_bribes > 0:  # Only include gauges with bribes
                gauge_data.append(
                    {
                        "address": gauge.address,
                        "pool": gauge.pool,
                        "current_votes": adjusted_votes,
                        "bribes_usd": total_bribes,
                        "historical_roi_per_1k": hist_roi.get(
                            gauge.address.lower(), 999.0
                        ),
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

    def _historical_roi_per_1k(self, n_epochs: int = 7) -> Dict[str, float]:
        """Return average realized ROI per 1k votes per gauge address (lowercase).

        Uses the last *n_epochs* epochs that have executed allocations and
        matching boundary vote data.  Gauges with no history are absent from
        the result dict; the optimizer treats absent entries as passing the
        floor (sentinel 999 in gauge_data).
        """
        try:
            from src.optimizer import expected_return_usd as _eru
            live_path = Config.DATABASE_PATH
            pre_path  = "data/db/preboundary_dev.db"

            live = sqlite3.connect(live_path)
            pre  = sqlite3.connect(pre_path)

            # Epochs with successful executed allocations
            runs = live.execute("""
                SELECT ea.epoch, avr.id
                FROM executed_allocations ea
                JOIN auto_vote_runs avr
                    ON ea.strategy_tag = 'auto_voter_run_' || avr.id
                    AND avr.status = 'tx_success'
                GROUP BY ea.epoch
                ORDER BY ea.epoch DESC
                LIMIT ?
            """, (n_epochs,)).fetchall()

            roi_sum:   Dict[str, float] = {}
            roi_count: Dict[str, int]   = {}

            for epoch, run_id in runs:
                strategy_tag = f"auto_voter_run_{run_id}"

                exec_alloc = {
                    r[0]: int(r[1])
                    for r in live.execute(
                        "SELECT lower(gauge_address), executed_votes "
                        "FROM executed_allocations "
                        "WHERE epoch=? AND strategy_tag=? AND executed_votes > 0",
                        (epoch, strategy_tag),
                    ).fetchall()
                }
                bndry_votes = {
                    r[0]: float(r[1] or 0)
                    for r in live.execute(
                        "SELECT lower(gauge_address), votes_raw "
                        "FROM boundary_gauge_values WHERE epoch=? AND active_only=1",
                        (epoch,),
                    ).fetchall()
                }
                bribe_usd = {
                    r[0]: float(r[1] or 0)
                    for r in pre.execute(
                        "SELECT lower(gauge_address), rewards_now_usd "
                        "FROM preboundary_snapshots "
                        "WHERE epoch=? AND decision_window='T-1'",
                        (epoch,),
                    ).fetchall()
                }

                for gauge, our_v in exec_alloc.items():
                    total_v  = bndry_votes.get(gauge, 0.0)
                    others_v = max(0.0, total_v - our_v)
                    rew      = bribe_usd.get(gauge, 0.0)
                    realized = _eru(rew, others_v, float(our_v))
                    roi_1k   = (realized / our_v * 1_000) if our_v > 0 else 0.0
                    roi_sum[gauge]   = roi_sum.get(gauge, 0.0) + roi_1k
                    roi_count[gauge] = roi_count.get(gauge, 0) + 1

            live.close()
            pre.close()

            return {g: roi_sum[g] / roi_count[g] for g in roi_sum}
        except Exception as exc:
            logger.warning("Could not compute historical ROI/1k: %s", exc)
            return {}

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
