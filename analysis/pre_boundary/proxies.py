"""
P3 Proxy Learning Layer

Learn historical vote drift and reward uplift distributions per gauge and decision window.
Apply fallback hierarchy for sparse data coverage.
"""

import sqlite3
import logging
import numpy as np
from dataclasses import dataclass, asdict
from typing import Dict, List, Tuple, Optional, Any, Sequence

from config.preboundary_settings import (
    CONFIDENCE_PENALTY_SPARSE_HISTORY,
    CONFIDENCE_PENALTY_HIGH_VARIANCE,
    CONFIDENCE_PENALTY_CAP,
)

logger = logging.getLogger(__name__)


@dataclass
class VoteDriftEstimate:
    """Per-gauge vote denominator drift estimate."""

    gauge_address: str
    decision_window: str

    # Quantiles (p25, p50, p75)
    drift_p25: float  # pessimistic (high final votes, high dilution)
    drift_p50: float  # median
    drift_p75: float  # optimistic (low final votes, low dilution)

    # Coverage metadata
    num_observations: int
    sample_variance: float
    confidence_penalty: float  # penalty for high variance / sparse data
    source: str  # "gauge_level" / "cluster_fallback" / "global_fallback"

    def to_dict(self) -> Dict[str, Any]:
        """Serialize for JSON caching."""
        return asdict(self)


@dataclass
class RewardUpliftEstimate:
    """Per-gauge reward uplift estimate (late bribe additions)."""

    gauge_address: str
    decision_window: str

    # Quantiles (p25, p50, p75)
    uplift_p25: float  # pessimistic (few late bribes)
    uplift_p50: float  # median
    uplift_p75: float  # optimistic (many late bribes)

    # Metadata
    num_observations: int
    sample_variance: float
    confidence_penalty: float
    source: str

    def to_dict(self) -> Dict[str, Any]:
        """Serialize for JSON caching."""
        return asdict(self)


def learn_vote_drift_by_window(
    conn: sqlite3.Connection,
    decision_window: str,
    min_sample_size: int = 6,
) -> Dict[str, VoteDriftEstimate]:
    """
    Learn vote drift (votes_final / votes_now - 1) by gauge and decision window.

    Algorithm:
    1. Query preboundary_truth_labels and preboundary_snapshots
    2. For each gauge:
       - Compute realized drift = (final_votes - votes_now) / votes_now
       - Estimate (p25, p50, p75) quantiles
       - Count observations
       - Apply confidence penalty if num_observations < min_sample_size
    3. Apply fallback hierarchy:
       - If gauge has < min_sample_size observations: back off to cluster average
       - If cluster has < 4 observations: back off to global average
    4. Return Dict[gauge_address] → VoteDriftEstimate
    """
    estimates = {}

    cursor = conn.cursor()
    try:
        # Query realized drifts per gauge
        cursor.execute(
            """
            SELECT
                s.gauge_address,
                s.pool_address,
                s.votes_now_raw,
                t.final_votes_raw
            FROM preboundary_snapshots s
            LEFT JOIN preboundary_truth_labels t
                ON s.epoch = t.epoch AND s.gauge_address = t.gauge_address
            WHERE s.decision_window = ? AND t.final_votes_raw IS NOT NULL
                AND s.votes_now_raw > 0
            ORDER BY s.gauge_address
            """,
            (decision_window,),
        )
        rows = cursor.fetchall()
        logger.debug(f"✓ Loaded {len(rows)} drift observations for window {decision_window}")

        # Aggregate by gauge: compute drifts and quantiles
        gauge_drifts = {}
        gauge_pools = {}
        for gauge_address, pool_address, votes_now, final_votes in rows:
            if gauge_address not in gauge_drifts:
                gauge_drifts[gauge_address] = []
                gauge_pools[gauge_address] = pool_address

            drift = (final_votes - votes_now) / float(votes_now)
            gauge_drifts[gauge_address].append(drift)

        # Compute estimates per gauge
        gauge_level_estimates = {}
        for gauge_address, drifts in gauge_drifts.items():
            if len(drifts) >= min_sample_size:
                # Gauge-level estimate
                p25, p50, p75 = np.percentile(drifts, [25, 50, 75])
                variance = float(np.var(drifts))
                confidence_penalty = 0.0
                source = "gauge_level"

                estimate = VoteDriftEstimate(
                    gauge_address=gauge_address,
                    decision_window=decision_window,
                    drift_p25=float(p25),
                    drift_p50=float(p50),
                    drift_p75=float(p75),
                    num_observations=len(drifts),
                    sample_variance=variance,
                    confidence_penalty=confidence_penalty,
                    source=source,
                )
                gauge_level_estimates[gauge_address] = estimate
                estimates[gauge_address] = estimate
            else:
                # Mark for fallback
                gauge_level_estimates[gauge_address] = None

        # Fallback: compute cluster (pool-level) averages
        pool_drifts = {}
        for gauge_address, pool_address in gauge_pools.items():
            if gauge_level_estimates.get(gauge_address) is None:
                if pool_address not in pool_drifts:
                    pool_drifts[pool_address] = []
                if gauge_address in gauge_drifts:
                    pool_drifts[pool_address].extend(gauge_drifts[gauge_address])

        pool_estimates = {}
        for pool_address, drifts in pool_drifts.items():
            if len(drifts) >= 4:  # cluster threshold
                p25, p50, p75 = np.percentile(drifts, [25, 50, 75])
                variance = float(np.var(drifts))
                pool_estimates[pool_address] = {
                    "p25": float(p25),
                    "p50": float(p50),
                    "p75": float(p75),
                    "variance": variance,
                    "num_observations": len(drifts),
                }

        # Assign fallback estimates
        for gauge_address, pool_address in gauge_pools.items():
            if gauge_level_estimates.get(gauge_address) is None:
                if pool_address in pool_estimates:
                    # Cluster fallback
                    est = pool_estimates[pool_address]
                    penalty = apply_confidence_penalty(
                        gauge_drifts.get(gauge_address, []),
                        min_sample_size,
                        base_penalty=CONFIDENCE_PENALTY_SPARSE_HISTORY,
                    )
                    estimate = VoteDriftEstimate(
                        gauge_address=gauge_address,
                        decision_window=decision_window,
                        drift_p25=est["p25"],
                        drift_p50=est["p50"],
                        drift_p75=est["p75"],
                        num_observations=len(gauge_drifts.get(gauge_address, [])),
                        sample_variance=est["variance"],
                        confidence_penalty=penalty,
                        source="cluster_fallback",
                    )
                    estimates[gauge_address] = estimate
                else:
                    # Global fallback: assume 0 drift
                    penalty = min(
                        CONFIDENCE_PENALTY_SPARSE_HISTORY + CONFIDENCE_PENALTY_HIGH_VARIANCE,
                        CONFIDENCE_PENALTY_CAP,
                    )
                    estimate = VoteDriftEstimate(
                        gauge_address=gauge_address,
                        decision_window=decision_window,
                        drift_p25=-0.05,
                        drift_p50=0.0,
                        drift_p75=0.05,
                        num_observations=len(gauge_drifts.get(gauge_address, [])),
                        sample_variance=0.001,
                        confidence_penalty=penalty,
                        source="global_fallback",
                    )
                    estimates[gauge_address] = estimate

    except Exception as e:
        logger.error(f"Error learning vote drift for window {decision_window}: {e}")

    logger.info(
        f"✓ Vote drift estimates computed for {len(estimates)} gauges (window {decision_window})"
    )
    return estimates


def learn_reward_uplift_by_window(
    conn: sqlite3.Connection,
    decision_window: str,
    min_sample_size: int = 6,
) -> Dict[str, RewardUpliftEstimate]:
    """
    Learn reward uplift (rewards_final / rewards_now - 1) by gauge and decision window.
    Similar structure to learn_vote_drift_by_window.
    """
    estimates = {}

    cursor = conn.cursor()
    try:
        # Query realized uplifts per gauge
        cursor.execute(
            """
            SELECT
                s.gauge_address,
                s.pool_address,
                s.rewards_now_usd,
                t.final_rewards_usd
            FROM preboundary_snapshots s
            LEFT JOIN preboundary_truth_labels t
                ON s.epoch = t.epoch AND s.gauge_address = t.gauge_address
            WHERE s.decision_window = ? AND t.final_rewards_usd IS NOT NULL
                AND s.rewards_now_usd > 0
            ORDER BY s.gauge_address
            """,
            (decision_window,),
        )
        rows = cursor.fetchall()
        logger.debug(f"✓ Loaded {len(rows)} uplift observations for window {decision_window}")

        # Aggregate by gauge: compute uplifts and quantiles
        gauge_uplifts = {}
        gauge_pools = {}
        for gauge_address, pool_address, rewards_now, final_rewards in rows:
            if gauge_address not in gauge_uplifts:
                gauge_uplifts[gauge_address] = []
                gauge_pools[gauge_address] = pool_address

            uplift = (final_rewards - rewards_now) / float(rewards_now)
            gauge_uplifts[gauge_address].append(uplift)

        # Compute estimates per gauge
        gauge_level_estimates = {}
        for gauge_address, uplifts in gauge_uplifts.items():
            if len(uplifts) >= min_sample_size:
                # Gauge-level estimate
                p25, p50, p75 = np.percentile(uplifts, [25, 50, 75])
                variance = float(np.var(uplifts))
                confidence_penalty = 0.0
                source = "gauge_level"

                estimate = RewardUpliftEstimate(
                    gauge_address=gauge_address,
                    decision_window=decision_window,
                    uplift_p25=float(p25),
                    uplift_p50=float(p50),
                    uplift_p75=float(p75),
                    num_observations=len(uplifts),
                    sample_variance=variance,
                    confidence_penalty=confidence_penalty,
                    source=source,
                )
                gauge_level_estimates[gauge_address] = estimate
                estimates[gauge_address] = estimate
            else:
                # Mark for fallback
                gauge_level_estimates[gauge_address] = None

        # Fallback: compute cluster (pool-level) averages
        pool_uplifts = {}
        for gauge_address, pool_address in gauge_pools.items():
            if gauge_level_estimates.get(gauge_address) is None:
                if pool_address not in pool_uplifts:
                    pool_uplifts[pool_address] = []
                if gauge_address in gauge_uplifts:
                    pool_uplifts[pool_address].extend(gauge_uplifts[gauge_address])

        pool_estimates = {}
        for pool_address, uplifts in pool_uplifts.items():
            if len(uplifts) >= 4:  # cluster threshold
                p25, p50, p75 = np.percentile(uplifts, [25, 50, 75])
                variance = float(np.var(uplifts))
                pool_estimates[pool_address] = {
                    "p25": float(p25),
                    "p50": float(p50),
                    "p75": float(p75),
                    "variance": variance,
                    "num_observations": len(uplifts),
                }

        # Assign fallback estimates
        for gauge_address, pool_address in gauge_pools.items():
            if gauge_level_estimates.get(gauge_address) is None:
                if pool_address in pool_estimates:
                    # Cluster fallback
                    est = pool_estimates[pool_address]
                    penalty = apply_confidence_penalty(
                        gauge_uplifts.get(gauge_address, []),
                        min_sample_size,
                        base_penalty=CONFIDENCE_PENALTY_SPARSE_HISTORY,
                    )
                    estimate = RewardUpliftEstimate(
                        gauge_address=gauge_address,
                        decision_window=decision_window,
                        uplift_p25=est["p25"],
                        uplift_p50=est["p50"],
                        uplift_p75=est["p75"],
                        num_observations=len(gauge_uplifts.get(gauge_address, [])),
                        sample_variance=est["variance"],
                        confidence_penalty=penalty,
                        source="cluster_fallback",
                    )
                    estimates[gauge_address] = estimate
                else:
                    # Global fallback: assume 0 uplift
                    penalty = min(
                        CONFIDENCE_PENALTY_SPARSE_HISTORY + CONFIDENCE_PENALTY_HIGH_VARIANCE,
                        CONFIDENCE_PENALTY_CAP,
                    )
                    estimate = RewardUpliftEstimate(
                        gauge_address=gauge_address,
                        decision_window=decision_window,
                        uplift_p25=0.0,
                        uplift_p50=0.0,
                        uplift_p75=0.10,
                        num_observations=len(gauge_uplifts.get(gauge_address, [])),
                        sample_variance=0.001,
                        confidence_penalty=penalty,
                        source="global_fallback",
                    )
                    estimates[gauge_address] = estimate

    except Exception as e:
        logger.error(f"Error learning reward uplift for window {decision_window}: {e}")

    logger.info(
        f"✓ Reward uplift estimates computed for {len(estimates)} gauges (window {decision_window})"
    )
    return estimates


def apply_confidence_penalty(
    observations: List[float],
    min_sample_size: int = 6,
    base_penalty: float = 0.10,
) -> float:
    """
    Compute confidence penalty based on sample size and variance.

    Penalty schedule:
    - num_observations >= min_sample_size: penalty = 0.0
    - num_observations == min_sample_size - 1: penalty = base_penalty
    - diminishing returns with fewer observations
    - capped at CONFIDENCE_PENALTY_CAP
    """
    num_obs = len(observations) if observations else 0

    if num_obs >= min_sample_size:
        return 0.0

    if num_obs == 0:
        return CONFIDENCE_PENALTY_CAP

    # Linear penalty: base_penalty per missing observation
    deficit = min_sample_size - num_obs
    penalty = base_penalty * deficit

    # Add variance penalty if high
    if len(observations) > 1:
        var = float(np.var(observations))
        if var > 0.01:  # high variance threshold
            penalty += CONFIDENCE_PENALTY_HIGH_VARIANCE

    return min(penalty, CONFIDENCE_PENALTY_CAP)


def attach_proxies_to_features(
    features_by_window: Dict[str, List[Dict]],
    drift_estimates: Dict[str, Dict[str, VoteDriftEstimate]],
    uplift_estimates: Dict[str, Dict[str, RewardUpliftEstimate]],
) -> Dict[str, List[Dict]]:
    """
    Augment feature dicts with proxy estimates.
    For each feature dict, add:
    - 'vote_drift_p25', 'vote_drift_p50', 'vote_drift_p75'
    - 'reward_uplift_p25', 'reward_uplift_p50', 'reward_uplift_p75'
    - 'confidence_penalty_drift', 'confidence_penalty_uplift', 'confidence_penalty_total'
    - 'drift_source', 'uplift_source'

    Args:
        features_by_window: Dict[window] → List[feature dicts]
        drift_estimates: Dict[window] → Dict[gauge] → VoteDriftEstimate
        uplift_estimates: Dict[window] → Dict[gauge] → RewardUpliftEstimate

    Returns:
        augmented features dict
    """
    augmented = {}

    for window, features in features_by_window.items():
        drift_est = drift_estimates.get(window, {})
        uplift_est = uplift_estimates.get(window, {})
        augmented[window] = []

        for feature in features:
            gauge_address = feature.get("gauge_address")
            augmented_feature = feature.copy()

            # Attach drift proxies
            if gauge_address in drift_est:
                de = drift_est[gauge_address]
                augmented_feature["vote_drift_p25"] = de.drift_p25
                augmented_feature["vote_drift_p50"] = de.drift_p50
                augmented_feature["vote_drift_p75"] = de.drift_p75
                augmented_feature["confidence_penalty_drift"] = de.confidence_penalty
                augmented_feature["drift_source"] = de.source
            else:
                augmented_feature["vote_drift_p25"] = None
                augmented_feature["vote_drift_p50"] = 0.0
                augmented_feature["vote_drift_p75"] = None
                augmented_feature["confidence_penalty_drift"] = CONFIDENCE_PENALTY_CAP
                augmented_feature["drift_source"] = "unknown"

            # Attach uplift proxies
            if gauge_address in uplift_est:
                ue = uplift_est[gauge_address]
                augmented_feature["reward_uplift_p25"] = ue.uplift_p25
                augmented_feature["reward_uplift_p50"] = ue.uplift_p50
                augmented_feature["reward_uplift_p75"] = ue.uplift_p75
                augmented_feature["confidence_penalty_uplift"] = ue.confidence_penalty
                augmented_feature["uplift_source"] = ue.source
            else:
                augmented_feature["reward_uplift_p25"] = None
                augmented_feature["reward_uplift_p50"] = 0.0
                augmented_feature["reward_uplift_p75"] = None
                augmented_feature["confidence_penalty_uplift"] = CONFIDENCE_PENALTY_CAP
                augmented_feature["uplift_source"] = "unknown"

            # Compute total penalty
            drift_pen = augmented_feature.get("confidence_penalty_drift", 0.0) or 0.0
            uplift_pen = augmented_feature.get("confidence_penalty_uplift", 0.0) or 0.0
            augmented_feature["confidence_penalty_total"] = min(
                drift_pen + uplift_pen, CONFIDENCE_PENALTY_CAP
            )

            augmented[window].append(augmented_feature)

    logger.info(f"✓ Proxies attached to {sum(len(f) for f in augmented.values())} features")
    return augmented
