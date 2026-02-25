"""
P4 Risk-Aware Optimizer

Solve portfolio allocation problem with scenario-weighted returns and downside control.
"""

import sqlite3
import logging
import numpy as np
from typing import Dict, List, Tuple, Optional, Any
from scipy.optimize import minimize, LinearConstraint, Bounds

from config.preboundary_settings import (
    SCENARIO_WEIGHTS,
    LAMBDA_RISK,
    K_MAX,
    MIN_VOTES_PER_POOL,
)

logger = logging.getLogger(__name__)


def optimize_allocation(
    features: List[Dict[str, Any]],
    scenarios: Dict[str, List[Dict[str, Any]]],
    voting_power: float = 1_000_000,
    lambda_risk: float = 0.20,
    k_max: int = 5,
    min_votes_per_pool: int = 50_000,
) -> Dict[str, Any]:
    """
    Solve risk-aware allocation optimization.

    Maximize: E[Return(x)] - λ * Downside(x)

    Subject to:
    - sum(x) = voting_power
    - x >= 0
    - x_i > 0 => x_i >= min_votes_per_pool (for active gauges)
    - num_nonzero(x) <= k_max

    Args:
        features: List of feature dicts from P3
        scenarios: Dict[scenario_name] → List[ForecastScenario dicts]
        voting_power: total voting power to allocate
        lambda_risk: risk penalty coefficient
        k_max: maximum number of pools to allocate to
        min_votes_per_pool: minimum votes per active pool

    Returns:
        {
            'allocation': Dict[gauge_address] → votes_allocated,
            'expected_return': float,
            'downside_return': float,
            'risk_adjustment': float,
            'num_gauges': int,
            'validation_warnings': List[str],
        }
    """
    result = {
        "allocation": {},
        "expected_return": 0.0,
        "downside_return": 0.0,
        "risk_adjustment": 0.0,
        "num_gauges": 0,
        "validation_warnings": [],
        "optimizer_status": "unknown",
    }

    try:
        if not features or not scenarios:
            logger.warning("Empty features or scenarios")
            return result

        gauge_addresses = [f["gauge_address"] for f in features]
        n_gauges = len(gauge_addresses)
        logger.info(f"Optimizing allocation for {n_gauges} gauges")

        # Compute per-gauge marginal returns for each scenario
        scenario_returns = _compute_scenario_returns(features, scenarios)

        if not scenario_returns:
            logger.warning("No scenario returns computed")
            return result

        # Use greedy allocation as MVP solver (faster than full optimization)
        allocation = _greedy_allocation(
            gauge_addresses,
            scenario_returns,
            voting_power,
            k_max,
            min_votes_per_pool,
        )

        # Validate allocation
        is_valid, warnings = apply_optimizer_guardrails(
            allocation, features, voting_power, k_max, min_votes_per_pool
        )
        result["validation_warnings"] = warnings

        if not is_valid:
            logger.warning(f"Allocation failed guardrails: {warnings}")
            result["optimizer_status"] = "failed_guardrails"
            return result

        # Compute returns
        returns = compute_downside_metrics(allocation, scenarios, scenario_returns)

        result["allocation"] = allocation
        result["expected_return"] = returns.get("return_weighted", 0.0)
        result["downside_return"] = returns.get("return_p10", 0.0)
        result["risk_adjustment"] = (
            lambda_risk * max(0.0, returns.get("return_weighted", 0.0) - returns.get("return_p10", 0.0))
        )
        result["num_gauges"] = len([x for x in allocation.values() if x > 0])
        result["optimizer_status"] = "success"

        logger.info(
            f"✓ Optimization complete: {result['num_gauges']} gauges, "
            f"return={result['expected_return']:.6f}, downside={result['downside_return']:.6f}"
        )

    except Exception as e:
        logger.error(f"Error in optimization: {e}")
        result["optimizer_status"] = f"error: {str(e)}"

    return result


def _compute_scenario_returns(
    features: List[Dict],
    scenarios: Dict[str, List[Dict]],
) -> Dict[str, Dict[str, float]]:
    """
    Compute per-gauge marginal returns for each scenario.

    Returns: Dict[scenario_name] → Dict[gauge_address] → return (fraction)
    """
    returns = {}

    for scenario_name, scenario_list in scenarios.items():
        returns[scenario_name] = {}

        for scenario in scenario_list:
            # Handle both dict and ForecastScenario objects
            if isinstance(scenario, dict):
                gauge_address = scenario.get("gauge_address")
                votes_final = scenario.get("votes_final_estimate", 1.0)
                rewards_final = scenario.get("rewards_final_estimate", 0.0)
            else:
                gauge_address = scenario.gauge_address
                votes_final = scenario.votes_final_estimate
                rewards_final = scenario.rewards_final_estimate

            if votes_final <= 0:
                marginal_return = 0.0
            else:
                marginal_return = rewards_final / (votes_final + 1.0)

            returns[scenario_name][gauge_address] = marginal_return

    return returns


def _greedy_allocation(
    gauge_addresses: List[str],
    scenario_returns: Dict[str, Dict[str, float]],
    voting_power: float,
    k_max: int = 5,
    min_votes_per_pool: int = 50_000,
) -> Dict[str, float]:
    """
    Greedy allocation: rank gauges by weighted return and allocate greedily.

    Algorithm:
    1. Compute weighted return for each gauge across scenarios
    2. Sort by return (descending)
    3. Allocate min_votes to top gauges until k_max reached or voting_power exhausted
    4. Distribute remaining votes to top gauge
    """
    allocation = {gauge: 0 for gauge in gauge_addresses}

    try:
        # Compute weighted returns per gauge
        weighted_returns = {}
        for gauge in gauge_addresses:
            ret = 0.0
            for scenario_name, weight in SCENARIO_WEIGHTS.items():
                ret += weight * scenario_returns.get(scenario_name, {}).get(gauge, 0.0)
            weighted_returns[gauge] = ret

        # Sort by return (descending)
        sorted_gauges = sorted(weighted_returns.items(), key=lambda x: x[1], reverse=True)

        # Allocate to top-K gauges
        remaining_votes = voting_power
        num_allocated = 0

        for gauge, _ in sorted_gauges:
            if num_allocated >= k_max:
                break

            if remaining_votes >= min_votes_per_pool:
                allocation[gauge] = min_votes_per_pool
                remaining_votes -= min_votes_per_pool
                num_allocated += 1

        # Allocate remaining votes to top gauge
        if num_allocated > 0 and remaining_votes > 0:
            top_gauge = sorted_gauges[0][0]
            allocation[top_gauge] += remaining_votes

        logger.debug(
            f"Greedy allocation: {num_allocated} gauges, "
            f"returns={[f'{weighted_returns[g]:.6f}' for g, _ in sorted_gauges[:3]]}"
        )

    except Exception as e:
        logger.error(f"Error in greedy allocation: {e}")

    return allocation


def apply_optimizer_guardrails(
    allocation: Dict[str, float],
    features: List[Dict],
    voting_power: float,
    k_max: int = 5,
    min_votes_per_pool: int = 50_000,
) -> Tuple[bool, List[str]]:
    """
    Validate allocation against guardrails.

    Checks:
    - sum(allocation) == voting_power (within 1 vote tolerance)
    - all(allocation >= 0)
    - all(allocation > 0 implies allocation >= min_votes_per_pool)
    - num_nonzero(allocation) <= k_max
    - all gauges in features

    Returns:
        (is_valid: bool, warnings: List[str])
    """
    warnings = []
    is_valid = True

    try:
        # Check sum
        total_allocated = sum(allocation.values())
        if abs(total_allocated - voting_power) > 1.0:
            warnings.append(
                f"⚠ Sum constraint violated: {total_allocated} vs {voting_power}"
            )
            is_valid = False

        # Check non-negativity
        negative_allocations = {k: v for k, v in allocation.items() if v < 0}
        if negative_allocations:
            warnings.append(f"⚠ Negative allocations: {negative_allocations}")
            is_valid = False

        # Check min_votes constraint
        feature_set = {f["gauge_address"] for f in features}
        invalid_min_votes = {}
        for gauge, votes in allocation.items():
            if 0 < votes < min_votes_per_pool:
                invalid_min_votes[gauge] = votes
        if invalid_min_votes:
            warnings.append(
                f"⚠ Min votes constraint violated for {len(invalid_min_votes)} gauges"
            )
            is_valid = False

        # Check K_max constraint
        num_active = len([v for v in allocation.values() if v > 0])
        if num_active > k_max:
            warnings.append(
                f"⚠ K_max constraint violated: {num_active} gauges > {k_max}"
            )
            is_valid = False

        # Check all gauges in features
        invalid_gauges = set(allocation.keys()) - feature_set
        if invalid_gauges:
            warnings.append(
                f"⚠ {len(invalid_gauges)} gauges not in features: {invalid_gauges}"
            )
            is_valid = False

    except Exception as e:
        logger.error(f"Error validating guardrails: {e}")
        is_valid = False

    return is_valid, warnings


def compute_downside_metrics(
    allocation: Dict[str, float],
    scenarios: Dict[str, List[Dict]],
    scenario_returns: Dict[str, Dict[str, float]],
) -> Dict[str, float]:
    """
    Compute downside risk metrics for allocation.

    Returns:
        {
            'return_conservative': float,  # bps
            'return_base': float,          # bps
            'return_aggressive': float,    # bps
            'return_p10': float,           # bps (min of 3 scenarios)
            'return_weighted': float,      # bps (scenario-weighted average)
        }
    """
    metrics = {
        "return_conservative": 0.0,
        "return_base": 0.0,
        "return_aggressive": 0.0,
        "return_p10": 0.0,
        "return_weighted": 0.0,
    }

    try:
        total_allocated = sum(v for v in allocation.values() if v > 0)
        if total_allocated <= 0:
            return metrics

        for scenario_name in ["conservative", "base", "aggressive"]:
            scenario_ret_usd = 0.0

            for gauge, votes_allocated in allocation.items():
                if votes_allocated <= 0:
                    continue

                marginal_return = scenario_returns.get(scenario_name, {}).get(gauge, 0.0)
                scenario_ret_usd += votes_allocated * marginal_return

            scenario_ret_per_vote = scenario_ret_usd / total_allocated
            scenario_ret_bps = scenario_ret_per_vote * 10_000.0
            metrics[f"return_{scenario_name}"] = scenario_ret_bps

        # Compute weighted return
        weighted_return = sum(
            SCENARIO_WEIGHTS[scenario] * metrics[f"return_{scenario}"]
            for scenario in ["conservative", "base", "aggressive"]
        )
        metrics["return_weighted"] = weighted_return

        # P10 = min of scenarios (worst case)
        returns_list = [
            metrics["return_conservative"],
            metrics["return_base"],
            metrics["return_aggressive"],
        ]
        metrics["return_p10"] = min(returns_list)

    except Exception as e:
        logger.error(f"Error computing downside metrics: {e}")

    return metrics


def compute_portfolio_return(
    allocation: Dict[str, float],
    forecast_scenario: List[Dict],
    scenario_returns: Dict[str, Dict[str, float]],
) -> Tuple[float, Dict[str, float]]:
    """
    Compute total portfolio return for a given scenario.

    Returns:
        (total_return, per_gauge_returns)
    """
    per_gauge_returns = {}
    total_return = 0.0

    try:
        for gauge, votes_allocated in allocation.items():
            if votes_allocated > 0:
                marginal_ret = scenario_returns.get(gauge, 0.0)
                per_gauge_return = votes_allocated * marginal_ret
                per_gauge_returns[gauge] = per_gauge_return
                total_return += per_gauge_return

    except Exception as e:
        logger.error(f"Error computing portfolio return: {e}")

    return total_return, per_gauge_returns
