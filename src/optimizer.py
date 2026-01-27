"""
Vote allocation optimizer using greedy and quadratic optimization algorithms.
"""

import logging
from typing import Dict, List, Tuple

import numpy as np
from scipy.optimize import minimize

from config import Config

logger = logging.getLogger(__name__)


class VoteOptimizer:
    """Optimizes vote allocation to maximize expected bribe returns."""

    def __init__(self, voting_power: int):
        """
        Initialize optimizer.

        Args:
            voting_power: Your total voting power
        """
        self.voting_power = voting_power
        logger.info(f"Vote optimizer initialized with power: {voting_power}")

    def calculate_expected_return(
        self, gauge_votes: int, your_votes: int, total_bribes_usd: float
    ) -> float:
        """
        Calculate expected return for a vote allocation.

        Args:
            gauge_votes: Current votes on the gauge
            your_votes: Your votes to allocate
            total_bribes_usd: Total bribes on gauge in USD

        Returns:
            Expected return in USD
        """
        if your_votes == 0:
            return 0.0

        total_votes = gauge_votes + your_votes
        if total_votes == 0:
            return 0.0

        your_share = your_votes / total_votes
        return total_bribes_usd * your_share

    def greedy_allocation(
        self, gauge_data: List[Dict[str, any]]
    ) -> Dict[str, int]:
        """
        Greedy allocation: sort by bribes/vote ratio and allocate accordingly.

        Args:
            gauge_data: List of dicts with 'address', 'current_votes', 'bribes_usd'

        Returns:
            Dictionary mapping gauge address to vote amount
        """
        if not gauge_data:
            return {}

        # Calculate bribe per vote ratio
        for gauge in gauge_data:
            current_votes = gauge["current_votes"]
            bribes = gauge["bribes_usd"]

            # Avoid division by zero
            if current_votes == 0:
                gauge["ratio"] = float("inf") if bribes > 0 else 0
            else:
                gauge["ratio"] = bribes / current_votes

        # Sort by ratio descending
        sorted_gauges = sorted(gauge_data, key=lambda x: x["ratio"], reverse=True)

        # Allocate votes to top gauges
        allocation = {}
        remaining_power = self.voting_power
        max_gauges = min(len(sorted_gauges), Config.MAX_GAUGES_TO_VOTE)

        for i, gauge in enumerate(sorted_gauges[:max_gauges]):
            if remaining_power <= 0:
                break

            # Allocate proportionally (simple equal split for greedy)
            votes = remaining_power // (max_gauges - i)
            votes = max(votes, Config.MIN_VOTE_ALLOCATION)

            if votes > remaining_power:
                votes = remaining_power

            allocation[gauge["address"]] = votes
            remaining_power -= votes

        logger.info(f"Greedy allocation: {len(allocation)} gauges")
        return allocation

    def quadratic_optimization(
        self, gauge_data: List[Dict[str, any]]
    ) -> Dict[str, int]:
        """
        Quadratic optimization to maximize total expected return.

        Args:
            gauge_data: List of dicts with 'address', 'current_votes', 'bribes_usd'

        Returns:
            Dictionary mapping gauge address to vote amount
        """
        if not gauge_data:
            return {}

        n = len(gauge_data)

        # Filter out gauges with no bribes
        filtered_gauges = [g for g in gauge_data if g["bribes_usd"] > 0]
        if not filtered_gauges:
            logger.warning("No gauges with bribes to optimize")
            return {}

        # Limit to top gauges by bribes
        filtered_gauges = sorted(
            filtered_gauges, key=lambda x: x["bribes_usd"], reverse=True
        )[: Config.MAX_GAUGES_TO_VOTE]

        n = len(filtered_gauges)
        current_votes = np.array([g["current_votes"] for g in filtered_gauges])
        bribes = np.array([g["bribes_usd"] for g in filtered_gauges])

        # Objective function: maximize sum of (your_votes[i] / (current_votes[i] + your_votes[i])) * bribes[i]
        # Scipy minimizes, so we negate
        def objective(x):
            total_votes = current_votes + x
            shares = np.divide(x, total_votes, where=total_votes != 0)
            return -np.sum(shares * bribes)

        # Constraints: sum of votes = voting_power
        constraints = [{"type": "eq", "fun": lambda x: np.sum(x) - self.voting_power}]

        # Bounds: 0 <= votes[i] <= voting_power
        bounds = [(0, self.voting_power) for _ in range(n)]

        # Initial guess: equal distribution
        x0 = np.full(n, self.voting_power / n)

        # Optimize
        try:
            result = minimize(
                objective,
                x0,
                method="SLSQP",
                bounds=bounds,
                constraints=constraints,
                options={"maxiter": 1000},
            )

            if not result.success:
                logger.warning(f"Optimization did not converge: {result.message}")

            # Round to integers and adjust for rounding errors
            allocation_array = np.round(result.x).astype(int)

            # Adjust for rounding errors
            diff = self.voting_power - np.sum(allocation_array)
            if diff != 0:
                # Add/subtract from largest allocation
                max_idx = np.argmax(allocation_array)
                allocation_array[max_idx] += diff

            # Create allocation dictionary
            allocation = {}
            for i, gauge in enumerate(filtered_gauges):
                votes = int(allocation_array[i])
                if votes >= Config.MIN_VOTE_ALLOCATION:
                    allocation[gauge["address"]] = votes

            logger.info(f"Quadratic optimization: {len(allocation)} gauges")
            return allocation

        except Exception as e:
            logger.error(f"Optimization failed: {e}")
            # Fallback to greedy
            return self.greedy_allocation(gauge_data)

    def calculate_total_return(
        self, gauge_data: List[Dict[str, any]], allocation: Dict[str, int]
    ) -> float:
        """
        Calculate total expected return for an allocation.

        Args:
            gauge_data: List of gauge data
            allocation: Vote allocation

        Returns:
            Total expected return in USD
        """
        total_return = 0.0

        for gauge in gauge_data:
            address = gauge["address"]
            if address in allocation:
                your_votes = allocation[address]
                expected = self.calculate_expected_return(
                    gauge["current_votes"], your_votes, gauge["bribes_usd"]
                )
                total_return += expected

        return total_return

    def compare_strategies(
        self, gauge_data: List[Dict[str, any]]
    ) -> Dict[str, any]:
        """
        Compare different allocation strategies.

        Args:
            gauge_data: List of gauge data

        Returns:
            Dictionary with comparison results
        """
        # Greedy allocation
        greedy_alloc = self.greedy_allocation(gauge_data)
        greedy_return = self.calculate_total_return(gauge_data, greedy_alloc)

        # Quadratic optimization
        optimal_alloc = self.quadratic_optimization(gauge_data)
        optimal_return = self.calculate_total_return(gauge_data, optimal_alloc)

        # Naive allocation (equal weight to top 5 by bribes)
        top_5 = sorted(gauge_data, key=lambda x: x["bribes_usd"], reverse=True)[:5]
        naive_alloc = {g["address"]: self.voting_power // 5 for g in top_5}
        naive_return = self.calculate_total_return(gauge_data, naive_alloc)

        return {
            "greedy": {"allocation": greedy_alloc, "return": greedy_return},
            "optimal": {"allocation": optimal_alloc, "return": optimal_return},
            "naive": {"allocation": naive_alloc, "return": naive_return},
            "improvement_vs_naive": optimal_return - naive_return,
            "improvement_pct": (
                ((optimal_return - naive_return) / naive_return * 100)
                if naive_return > 0
                else 0
            ),
        }
