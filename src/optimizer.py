"""
Vote allocation optimizer using greedy and quadratic optimization algorithms.
"""

import logging
import math
from dataclasses import dataclass
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
        return expected_return_usd(total_bribes_usd, float(gauge_votes), float(your_votes))

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

        # Apply ROI/1k floor: skip pools with known poor historical performance.
        # Gauges pass if no history is available (field absent → 999 sentinel).
        roi_floor = Config.ROI_FLOOR_PER_1K
        pre_floor_count = len(filtered_gauges)
        filtered_gauges = [
            g for g in filtered_gauges
            if g.get("historical_roi_per_1k", 999.0) >= roi_floor
        ]
        if len(filtered_gauges) < pre_floor_count:
            logger.info(
                "ROI floor (%.2f/1k) dropped %d gauge(s)",
                roi_floor, pre_floor_count - len(filtered_gauges),
            )
        if not filtered_gauges:
            logger.warning("All gauges below ROI floor — running without floor")
            filtered_gauges = [g for g in gauge_data if g["bribes_usd"] > 0]

        # Limit to top gauges by ROI (bribes / current_votes), not raw bribe size.
        # Sorting by raw bribes selects the most-competed pools; ROI sort avoids that.
        filtered_gauges = sorted(
            filtered_gauges,
            key=lambda x: x["bribes_usd"] / max(x["current_votes"], 100_000),
            reverse=True,
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

        # Bounds: 0 <= votes[i] <= voting_power, with a tighter cap on
        # high-competition pools to force diversification.
        high_thresh = Config.HIGH_COMPETITION_VOTES_THRESHOLD
        cap_ratio   = Config.HIGH_COMPETITION_VOTE_CAP_RATIO
        max_cap     = int(self.voting_power * cap_ratio)
        bounds = [
            (0, max_cap if g["current_votes"] > high_thresh else self.voting_power)
            for g in filtered_gauges
        ]

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


# ---------------------------------------------------------------------------
# Canonical module-level voting math — single source of truth
# ---------------------------------------------------------------------------


def expected_return_usd(total_usd: float, base_votes: float, your_votes: float) -> float:
    if your_votes <= 0:
        return 0.0
    denom = float(base_votes) + float(your_votes)
    if denom <= 0:
        return 0.0
    return float(total_usd) * (float(your_votes) / denom)


def marginal_gain_usd(
    total_usd: float, base_votes: float, current_votes: float, delta_votes: float
) -> float:
    """Expected return gain from adding delta votes at current allocation level."""
    if delta_votes <= 0:
        return 0.0
    current = expected_return_usd(total_usd, base_votes, current_votes)
    after = expected_return_usd(total_usd, base_votes, current_votes + delta_votes)
    return max(0.0, after - current)


def marginal_loss_usd(
    total_usd: float, base_votes: float, current_votes: float, delta_votes: float
) -> float:
    """Expected return loss from removing delta votes at current allocation level."""
    if delta_votes <= 0 or current_votes <= 0:
        return 0.0
    before = expected_return_usd(total_usd, base_votes, current_votes)
    after = expected_return_usd(total_usd, base_votes, max(0.0, current_votes - delta_votes))
    return max(0.0, before - after)


@dataclass
class GaugeBoundaryState:
    gauge: str
    pool: str
    votes_raw: float
    total_usd: float


def solve_marginal_allocation(
    states: List[Tuple[str, str, float, float]],
    total_votes: int,
    min_per_pool: int,
    max_selected_pools: int,
    chunk_size: int = 1000,
) -> List[int]:
    """Discrete marginal allocator using vote chunks with dynamic pool entry/swap.

    - Seeds top pools with minimum allocation floor.
    - Allocates remaining votes in chunked marginal-return steps.
    - Allows inactive candidates to replace active pools when beneficial.
    - Uses exact budget by assigning final remainder to best active candidate.
    """
    n = len(states)
    if n == 0:
        return []

    total_votes_i = int(total_votes)
    if total_votes_i <= 0:
        return [0] * n

    max_selected = max(1, min(int(max_selected_pools), n))
    step = max(1, int(chunk_size))
    min_per_pool_i = int(max(0, min_per_pool))
    if max_selected * min_per_pool_i > total_votes_i:
        raise ValueError("Infeasible allocation: k * min_per_pool exceeds voting power")

    rewards = [max(float(s[3]), 0.0) for s in states]
    base_votes_list = [max(float(s[2]), 0.0) for s in states]

    allocations = [0] * n
    floors = [0] * n

    seed_count = min(max_selected, n)
    for idx in range(seed_count):
        floors[idx] = min_per_pool_i
        allocations[idx] = min_per_pool_i

    used = sum(allocations)
    if used > total_votes_i:
        raise ValueError("Infeasible seeded allocation")

    remaining = total_votes_i - used

    def active_indices() -> List[int]:
        return [idx for idx, votes in enumerate(allocations) if votes > 0]

    def best_add_candidate(delta_votes: int) -> Tuple[int, float]:
        best_idx = -1
        best_gain = -1.0
        active = set(active_indices())
        active_count = len(active)
        for idx in range(n):
            if idx not in active and active_count >= max_selected:
                continue
            gain = marginal_gain_usd(
                rewards[idx], base_votes_list[idx], float(allocations[idx]), float(delta_votes)
            )
            if gain > best_gain:
                best_gain = gain
                best_idx = idx
        return best_idx, max(0.0, best_gain)

    def best_active_add(delta_votes: int) -> Tuple[int, float]:
        best_idx = -1
        best_gain = -1.0
        for idx in active_indices():
            gain = marginal_gain_usd(
                rewards[idx], base_votes_list[idx], float(allocations[idx]), float(delta_votes)
            )
            if gain > best_gain:
                best_gain = gain
                best_idx = idx
        return best_idx, max(0.0, best_gain)

    def worst_removable_active(delta_votes: int) -> Tuple[int, float]:
        worst_idx = -1
        worst_loss = float("inf")
        for idx in active_indices():
            if allocations[idx] - floors[idx] < delta_votes:
                continue
            loss = marginal_loss_usd(
                rewards[idx], base_votes_list[idx], float(allocations[idx]), float(delta_votes)
            )
            if loss < worst_loss:
                worst_loss = loss
                worst_idx = idx
        if worst_idx < 0:
            return -1, 0.0
        return worst_idx, max(0.0, worst_loss)

    while remaining >= step:
        active = set(active_indices())
        active_count = len(active)
        candidate_idx, candidate_gain = best_add_candidate(step)
        if candidate_idx < 0:
            break

        if candidate_idx in active or active_count < max_selected:
            allocations[candidate_idx] += step
            remaining -= step
            continue

        removable_idx, removable_loss = worst_removable_active(step)
        if removable_idx >= 0 and candidate_gain > removable_loss:
            allocations[removable_idx] -= step
            allocations[candidate_idx] += step
            continue

        fallback_idx, _fallback_gain = best_active_add(step)
        if fallback_idx < 0:
            break
        allocations[fallback_idx] += step
        remaining -= step

    if remaining > 0:
        active = active_indices()
        if len(active) < max_selected:
            idx, _gain = best_add_candidate(remaining)
        else:
            idx, _gain = best_active_add(remaining)
        if idx < 0:
            idx = 0
        allocations[idx] += remaining
        remaining = 0

    total_alloc = sum(allocations)
    if total_alloc != total_votes_i:
        drift = total_votes_i - total_alloc
        target_idx = active_indices()[0] if active_indices() else 0
        allocations[target_idx] += drift

    return [int(v) for v in allocations]


def solve_alloc_for_set(
    states: List[GaugeBoundaryState], total_votes: int, min_per_pool: int
) -> List[float]:
    """Solve optimal allocation for K pools using Lagrange multiplier method."""
    k = len(states)
    if k * min_per_pool > total_votes:
        raise ValueError("Infeasible: k * min_per_pool > voting power")

    floors = [float(min_per_pool)] * k
    if k == 0:
        return []

    remaining = float(total_votes - k * min_per_pool)
    if remaining <= 0:
        return floors

    B = [max(s.total_usd, 0.0) for s in states]
    V = [max(float(s.votes_raw), 0.0) for s in states]

    def alloc_for_lambda(lmbd: float) -> List[float]:
        out = []
        for i in range(k):
            if B[i] <= 0 or V[i] <= 0:
                out.append(floors[i])
                continue
            x = math.sqrt((B[i] * V[i]) / lmbd) - V[i]
            out.append(max(x, floors[i]))
        return out

    lo = 1e-18
    hi = 1.0
    for _ in range(120):
        if sum(alloc_for_lambda(hi)) <= total_votes:
            break
        hi *= 2.0

    for _ in range(160):
        mid = (lo + hi) / 2.0
        if sum(alloc_for_lambda(mid)) > total_votes:
            lo = mid
        else:
            hi = mid

    alloc = alloc_for_lambda(hi)
    s = sum(alloc)
    if s <= 0:
        return floors

    if abs(s - total_votes) > 1e-8:
        scale = total_votes / s
        alloc = [max(f, a * scale) for a, f in zip(alloc, floors)]

    return alloc
