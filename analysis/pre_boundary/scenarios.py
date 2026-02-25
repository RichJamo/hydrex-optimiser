"""
P4 Scenario Engine

Build conservative/base/aggressive forecast scenarios using P3 proxy quantiles.
"""

import sqlite3
import json
import logging
from dataclasses import dataclass, asdict
from typing import Dict, List, Tuple, Optional, Any
from pathlib import Path

from config.preboundary_settings import SCENARIO_WEIGHTS

logger = logging.getLogger(__name__)


@dataclass
class ForecastScenario:
    """Forecast scenario (conservative, base, aggressive)."""

    scenario_name: str  # "conservative" / "base" / "aggressive"
    gauge_address: str
    decision_window: str

    # Drift assumptions for this scenario
    vote_drift: float

    # Uplift assumptions for this scenario
    reward_uplift: float

    # Derived forecast state
    votes_final_estimate: float
    rewards_final_estimate: float

    # Metadata
    source: str
    confidence_penalty: float

    def to_dict(self) -> Dict[str, Any]:
        """Serialize for storage."""
        return asdict(self)


def build_scenarios_for_epoch(
    conn: sqlite3.Connection,
    epoch: int,
    decision_window: str,
    cache_dir: str = "data/preboundary_cache",
) -> Dict[str, List[ForecastScenario]]:
    """
    Build all 3 scenarios for a single decision window.

    Load cached proxies from P3.
    For each gauge/feature:
    - Conservative: drift_p75 (high dilution), uplift_p25 (low bribes)
    - Base: drift_p50, uplift_p50
    - Aggressive: drift_p25 (low dilution), uplift_p75 (high bribes)

    Args:
        conn: SQLite connection
        epoch: boundary epoch
        decision_window: decision window name ("day", "T-1", "boundary")
        cache_dir: path to cached proxies from P3

    Returns:
        Dict[scenario_name] → List[ForecastScenario]
    """
    scenarios = {"conservative": [], "base": [], "aggressive": []}

    try:
        # Load cached drift estimates
        drift_file = Path(cache_dir) / f"drift_estimates_{decision_window}.json"
        if not drift_file.exists():
            logger.error(f"Drift cache not found: {drift_file}")
            return scenarios

        with open(drift_file, "r") as f:
            drift_data = json.load(f)
        drift_by_gauge = {
            est["gauge_address"]: est for est in drift_data.get("estimates", [])
        }

        # Load cached uplift estimates
        uplift_file = Path(cache_dir) / f"uplift_estimates_{decision_window}.json"
        if not uplift_file.exists():
            logger.error(f"Uplift cache not found: {uplift_file}")
            return scenarios

        with open(uplift_file, "r") as f:
            uplift_data = json.load(f)
        uplift_by_gauge = {
            est["gauge_address"]: est for est in uplift_data.get("estimates", [])
        }

        # Load features for this epoch/window
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT
                gauge_address,
                votes_now_raw,
                rewards_now_usd,
                data_quality_score
            FROM preboundary_snapshots
            WHERE epoch = ? AND decision_window = ?
            ORDER BY gauge_address
            """,
            (epoch, decision_window),
        )
        features = cursor.fetchall()
        logger.info(
            f"✓ Loaded {len(features)} features for epoch {epoch}, window {decision_window}"
        )

        # Build scenarios for each gauge
        for gauge_address, votes_now, rewards_now, quality_score in features:
            drift_est = drift_by_gauge.get(gauge_address)
            uplift_est = uplift_by_gauge.get(gauge_address)

            if not drift_est or not uplift_est:
                logger.warning(
                    f"Missing proxy estimates for gauge {gauge_address} ({decision_window})"
                )
                continue

            # Conservative: high drift (p75), low uplift (p25)
            conservative = ForecastScenario(
                scenario_name="conservative",
                gauge_address=gauge_address,
                decision_window=decision_window,
                vote_drift=float(drift_est["drift_p75"]),
                reward_uplift=float(uplift_est["uplift_p25"]),
                votes_final_estimate=votes_now * (1.0 + float(drift_est["drift_p75"])),
                rewards_final_estimate=rewards_now
                * (1.0 + float(uplift_est["uplift_p25"])),
                source=drift_est.get("source", "unknown"),
                confidence_penalty=max(
                    float(drift_est.get("confidence_penalty", 0.0)),
                    float(uplift_est.get("confidence_penalty", 0.0)),
                ),
            )
            scenarios["conservative"].append(conservative)

            # Base: median drift (p50), median uplift (p50)
            base = ForecastScenario(
                scenario_name="base",
                gauge_address=gauge_address,
                decision_window=decision_window,
                vote_drift=float(drift_est["drift_p50"]),
                reward_uplift=float(uplift_est["uplift_p50"]),
                votes_final_estimate=votes_now * (1.0 + float(drift_est["drift_p50"])),
                rewards_final_estimate=rewards_now * (1.0 + float(uplift_est["uplift_p50"])),
                source=drift_est.get("source", "unknown"),
                confidence_penalty=max(
                    float(drift_est.get("confidence_penalty", 0.0)),
                    float(uplift_est.get("confidence_penalty", 0.0)),
                ),
            )
            scenarios["base"].append(base)

            # Aggressive: low drift (p25), high uplift (p75)
            aggressive = ForecastScenario(
                scenario_name="aggressive",
                gauge_address=gauge_address,
                decision_window=decision_window,
                vote_drift=float(drift_est["drift_p25"]),
                reward_uplift=float(uplift_est["uplift_p75"]),
                votes_final_estimate=votes_now * (1.0 + float(drift_est["drift_p25"])),
                rewards_final_estimate=rewards_now
                * (1.0 + float(uplift_est["uplift_p75"])),
                source=drift_est.get("source", "unknown"),
                confidence_penalty=max(
                    float(drift_est.get("confidence_penalty", 0.0)),
                    float(uplift_est.get("confidence_penalty", 0.0)),
                ),
            )
            scenarios["aggressive"].append(aggressive)

    except Exception as e:
        logger.error(f"Error building scenarios for epoch {epoch}, window {decision_window}: {e}")

    logger.info(
        f"✓ Built scenarios for {len(scenarios['base'])} gauges "
        f"(conservative: {len(scenarios['conservative'])}, base: {len(scenarios['base'])}, aggressive: {len(scenarios['aggressive'])})"
    )
    return scenarios


def compute_scenario_returns(
    features: List[Dict],
    scenarios: Dict[str, List[Dict]],
    voting_power: float = 1_000_000,
) -> Dict[str, Dict[str, float]]:
    """
    Compute per-gauge marginal return for each scenario.

    Return formula (for marginal 1-unit allocation):
    Marginal_return_i = Rewards_final_i / (Votes_final_i + 1)

    Args:
        features: List of feature dicts with votes_now_raw, rewards_now_usd
        scenarios: Dict[scenario_name] → List[ForecastScenario dicts]
        voting_power: total voting power (for context)

    Returns:
        Dict[scenario_name] → Dict[gauge_address] → marginal_return_bps (basis points)
    """
    returns = {"conservative": {}, "base": {}, "aggressive": {}}

    try:
        for scenario_name, scenario_list in scenarios.items():
            if not scenario_list:
                logger.warning(f"Empty scenario list for {scenario_name}")
                continue

            for scenario in scenario_list:
                if isinstance(scenario, dict):
                    gauge_address = scenario.get("gauge_address")
                    votes_final = scenario.get("votes_final_estimate", 0.0)
                    rewards_final = scenario.get("rewards_final_estimate", 0.0)
                else:
                    gauge_address = scenario.gauge_address
                    votes_final = scenario.votes_final_estimate
                    rewards_final = scenario.rewards_final_estimate

                # Avoid division by zero
                if votes_final <= 0:
                    marginal_return = 0.0
                else:
                    # Marginal return for 1 unit allocation
                    marginal_return = rewards_final / (votes_final + 1.0)

                # Convert to basis points
                return_bps = int(marginal_return * 10_000)
                returns[scenario_name][gauge_address] = return_bps

    except Exception as e:
        logger.error(f"Error computing scenario returns: {e}")

    return returns


def validate_scenarios(
    scenarios_by_name: Dict[str, List[ForecastScenario]],
) -> Tuple[bool, List[str]]:
    """
    Validate scenario consistency and reasonableness.

    Checks:
    - All gauges present in all 3 scenarios
    - votes_final > 0, rewards_final >= 0
    - Conservative has higher votes_final than Base than Aggressive
    - Conservative has lower rewards_final than Base than Aggressive
    - No NaN/Inf values

    Returns:
        (is_valid: bool, warnings: List[str])
    """
    warnings = []
    is_valid = True

    try:
        # Check all scenarios present
        if len(scenarios_by_name) != 3:
            warnings.append(
                f"⚠ Expected 3 scenarios, got {len(scenarios_by_name)}"
            )
            is_valid = False

        # Get gauge sets
        gauge_sets = {
            name: {s.gauge_address for s in scenarios}
            for name, scenarios in scenarios_by_name.items()
        }

        # Check gauge consistency
        if gauge_sets["conservative"] != gauge_sets["base"]:
            missing_in_base = gauge_sets["conservative"] - gauge_sets["base"]
            if missing_in_base:
                warnings.append(
                    f"⚠ {len(missing_in_base)} gauges in conservative but not base"
                )
                is_valid = False

        if gauge_sets["base"] != gauge_sets["aggressive"]:
            missing_in_aggressive = gauge_sets["base"] - gauge_sets["aggressive"]
            if missing_in_aggressive:
                warnings.append(
                    f"⚠ {len(missing_in_aggressive)} gauges in base but not aggressive"
                )
                is_valid = False

        # Check for each gauge
        for scenario_name, scenarios in scenarios_by_name.items():
            for scenario in scenarios:
                gauge = scenario.gauge_address

                # Check positive votes
                if scenario.votes_final_estimate <= 0:
                    warnings.append(
                        f"⚠ {gauge} ({scenario_name}): votes_final <= 0"
                    )
                    is_valid = False

                # Check non-negative rewards
                if scenario.rewards_final_estimate < 0:
                    warnings.append(
                        f"⚠ {gauge} ({scenario_name}): rewards_final < 0"
                    )
                    is_valid = False

                # Check NaN/Inf
                for attr in ["votes_final_estimate", "rewards_final_estimate"]:
                    val = getattr(scenario, attr)
                    if val != val:  # NaN
                        warnings.append(
                            f"⚠ {gauge} ({scenario_name}): NaN in {attr}"
                        )
                        is_valid = False
                    elif val == float("inf") or val == float("-inf"):
                        warnings.append(
                            f"⚠ {gauge} ({scenario_name}): Inf in {attr}"
                        )
                        is_valid = False

        # Check monotonicity: conservative votes > base > aggressive
        # (conservative has higher drift = higher votes)
        for gauge in gauge_sets.get("base", set()):
            cons_scenario = next(
                (s for s in scenarios_by_name["conservative"] if s.gauge_address == gauge),
                None,
            )
            base_scenario = next(
                (s for s in scenarios_by_name["base"] if s.gauge_address == gauge),
                None,
            )
            agg_scenario = next(
                (s for s in scenarios_by_name["aggressive"] if s.gauge_address == gauge),
                None,
            )

            if cons_scenario and base_scenario:
                if cons_scenario.votes_final_estimate < base_scenario.votes_final_estimate:
                    warnings.append(
                        f"⚠ {gauge}: conservative votes < base votes (ordering violated)"
                    )

            if base_scenario and agg_scenario:
                if base_scenario.votes_final_estimate < agg_scenario.votes_final_estimate:
                    warnings.append(
                        f"⚠ {gauge}: base votes < aggressive votes (ordering violated)"
                    )

    except Exception as e:
        logger.error(f"Error validating scenarios: {e}")
        is_valid = False

    if is_valid and not warnings:
        total_scenarios = sum(len(s) for s in scenarios_by_name.values())
        logger.info(
            f"✓ Scenario validation passed: {total_scenarios} total scenarios"
        )

    return is_valid, warnings
