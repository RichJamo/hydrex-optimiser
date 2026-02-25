"""
P3 Feature & Proxy Validator

Inspect and validate feature coverage, proxy completeness, and confidence distribution.
Manual inspection tool for data quality assurance.
"""

import sqlite3
import logging
import argparse
from pathlib import Path
from typing import Dict, List, Tuple, Optional, Any

from analysis.pre_boundary.features import (
    build_snapshot_features,
    compute_feature_statistics,
    validate_features,
)
from analysis.pre_boundary.proxies import (
    learn_vote_drift_by_window,
    learn_reward_uplift_by_window,
)
from config.preboundary_settings import DECISION_WINDOWS

logger = logging.getLogger(__name__)


def validate_epoch_features(
    conn: sqlite3.Connection,
    epoch: int,
    verbose: bool = True,
) -> Dict[str, Any]:
    """
    Validate features for a single epoch across all decision windows.

    Returns:
        diagnostics dict:
        {
            'epoch': int,
            'windows': {
                'day': {'features': 27, 'data_quality_p50': 0.85, ...},
                'T-1': {...},
                'boundary': {...},
            },
            'gauges_missing_drift': [...],
            'gauges_missing_uplift': [...],
            'high_confidence_penalty_count': int,
            'overall_coverage': float,
        }
    """
    diagnostics = {
        "epoch": epoch,
        "windows": {},
        "gauges_missing_drift": [],
        "gauges_missing_uplift": [],
        "high_confidence_penalty_count": 0,
        "total_features": 0,
    }

    try:
        # Load features
        features_by_window = build_snapshot_features(conn, epoch)
        stats = compute_feature_statistics(conn, epoch)

        # Validate features
        is_valid, warnings = validate_features(features_by_window)
        if not is_valid:
            logger.warning(f"Feature validation warnings for epoch {epoch}:")
            for w in warnings:
                logger.warning(f"  {w}")

        # Collect diagnostics per window
        all_gauge_addresses = set()
        for window in DECISION_WINDOWS.keys():
            features = features_by_window.get(window, [])
            window_stats = stats.get(window, {})

            diagnostics["windows"][window] = {
                "features": len(features),
                "votes_avg": window_stats.get("votes_avg"),
                "rewards_avg": window_stats.get("rewards_avg"),
                "data_quality_avg": window_stats.get("data_quality_avg"),
                "inclusion_prob_avg": window_stats.get("inclusion_prob_avg"),
            }

            diagnostics["total_features"] += len(features)
            for feature in features:
                all_gauge_addresses.add(feature.get("gauge_address"))

        # Check proxy coverage
        for window in DECISION_WINDOWS.keys():
            try:
                drift_estimates = learn_vote_drift_by_window(conn, window)
                uplift_estimates = learn_reward_uplift_by_window(conn, window)

                for gauge in all_gauge_addresses:
                    if gauge not in drift_estimates:
                        if gauge not in diagnostics["gauges_missing_drift"]:
                            diagnostics["gauges_missing_drift"].append(gauge)

                    if gauge not in uplift_estimates:
                        if gauge not in diagnostics["gauges_missing_uplift"]:
                            diagnostics["gauges_missing_uplift"].append(gauge)

                    # Check for high confidence penalties
                    if gauge in drift_estimates:
                        penalty = drift_estimates[gauge].confidence_penalty
                        if penalty > 0.15:
                            diagnostics["high_confidence_penalty_count"] += 1

                    if gauge in uplift_estimates:
                        penalty = uplift_estimates[gauge].confidence_penalty
                        if penalty > 0.15:
                            diagnostics["high_confidence_penalty_count"] += 1

            except Exception as e:
                logger.error(f"Error checking proxy coverage for window {window}: {e}")

        # Compute overall coverage
        if all_gauge_addresses:
            missing = len(
                diagnostics["gauges_missing_drift"] + diagnostics["gauges_missing_uplift"]
            )
            coverage = 1.0 - (missing / (2 * len(all_gauge_addresses)))
            diagnostics["overall_coverage"] = max(0.0, coverage)
        else:
            diagnostics["overall_coverage"] = 0.0

        if verbose:
            logger.info(f"Epoch {epoch} Validation Summary:")
            logger.info(f"  Total features: {diagnostics['total_features']}")
            logger.info(f"  Windows coverage: {list(diagnostics['windows'].keys())}")
            logger.info(f"  Overall proxy coverage: {diagnostics['overall_coverage']:.1%}")
            logger.info(
                f"  Gauges with high penalty: {diagnostics['high_confidence_penalty_count']}"
            )

    except Exception as e:
        logger.error(f"Error validating epoch {epoch}: {e}")

    return diagnostics


def validate_proxy_consistency(
    conn: sqlite3.Connection,
) -> Tuple[bool, List[str]]:
    """
    Cross-check proxy estimates for monotonicity and reasonableness.

    Checks:
    - drift_p25 <= drift_p50 <= drift_p75
    - |drift_p75 - drift_p25| < 1.0 (sanity bound for 100% max swing)
    - No NaN/Inf values

    Returns:
        (is_consistent: bool, warnings: List[str])
    """
    warnings = []
    is_consistent = True

    try:
        for window in DECISION_WINDOWS.keys():
            drift_estimates = learn_vote_drift_by_window(conn, window)
            uplift_estimates = learn_reward_uplift_by_window(conn, window)

            for gauge, estimate in drift_estimates.items():
                # Check ordering
                if not (
                    estimate.drift_p25
                    <= estimate.drift_p50
                    <= estimate.drift_p75
                ):
                    warnings.append(
                        f"⚠ Drift ordering violation for {gauge} ({window}): "
                        f"p25={estimate.drift_p25:.3f}, p50={estimate.drift_p50:.3f}, p75={estimate.drift_p75:.3f}"
                    )
                    is_consistent = False

                # Check bounds
                if estimate.drift_p75 - estimate.drift_p25 > 1.0:
                    warnings.append(
                        f"⚠ Drift span too large for {gauge} ({window}): "
                        f"span={estimate.drift_p75 - estimate.drift_p25:.3f}"
                    )
                    is_consistent = False

                # Check NaN/Inf
                for attr in ["drift_p25", "drift_p50", "drift_p75"]:
                    val = getattr(estimate, attr)
                    if val != val:  # NaN
                        warnings.append(f"⚠ NaN in drift {attr} for {gauge} ({window})")
                        is_consistent = False
                    elif val == float("inf") or val == float("-inf"):
                        warnings.append(
                            f"⚠ Inf in drift {attr} for {gauge} ({window})"
                        )
                        is_consistent = False

            for gauge, estimate in uplift_estimates.items():
                # Check ordering
                if not (
                    estimate.uplift_p25
                    <= estimate.uplift_p50
                    <= estimate.uplift_p75
                ):
                    warnings.append(
                        f"⚠ Uplift ordering violation for {gauge} ({window}): "
                        f"p25={estimate.uplift_p25:.3f}, p50={estimate.uplift_p50:.3f}, p75={estimate.uplift_p75:.3f}"
                    )
                    is_consistent = False

                # Check NaN/Inf
                for attr in ["uplift_p25", "uplift_p50", "uplift_p75"]:
                    val = getattr(estimate, attr)
                    if val != val:  # NaN
                        warnings.append(
                            f"⚠ NaN in uplift {attr} for {gauge} ({window})"
                        )
                        is_consistent = False
                    elif val == float("inf") or val == float("-inf"):
                        warnings.append(
                            f"⚠ Inf in uplift {attr} for {gauge} ({window})"
                        )
                        is_consistent = False

    except Exception as e:
        logger.error(f"Error validating proxy consistency: {e}")
        is_consistent = False

    return is_consistent, warnings


def cli_inspect_epoch(
    db_path: str = "data/db/preboundary_dev.db",
    epoch: Optional[int] = None,
    log_file: Optional[str] = None,
):
    """
    CLI to inspect a single epoch's features and proxies.

    Usage:
      python -m analysis.pre_boundary.feature_validator \
        --db-path data/db/preboundary_dev.db \
        --epoch 1771372800 \
        --verbose \
        --log-file data/db/logs/feature_validator.log
    """
    parser = argparse.ArgumentParser(
        description="Validate features and proxies for a given epoch"
    )
    parser.add_argument(
        "--db-path",
        default="data/db/preboundary_dev.db",
        help="Path to preboundary database",
    )
    parser.add_argument(
        "--epoch",
        type=int,
        help="Epoch to inspect (if not provided, uses most recent)",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        default=True,
        help="Verbose output",
    )
    parser.add_argument(
        "--log-file",
        default="data/db/logs/feature_validator.log",
        help="Log file path",
    )

    args = parser.parse_args()

    # Setup logging
    log_file = Path(args.log_file)
    log_file.parent.mkdir(parents=True, exist_ok=True)

    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        handlers=[
            logging.FileHandler(log_file),
            logging.StreamHandler(),
        ],
    )

    logger.info("=" * 80)
    logger.info("P3 Feature Validator Started")
    logger.info(f"DB: {args.db_path}")
    logger.info("=" * 80)

    try:
        conn = sqlite3.connect(args.db_path)

        # Determine epoch to inspect
        inspect_epoch = args.epoch
        if not inspect_epoch:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT MAX(epoch) FROM preboundary_snapshots"
            )
            row = cursor.fetchone()
            inspect_epoch = row[0] if row and row[0] else None

        if not inspect_epoch:
            logger.error("No epochs found in database")
            return

        logger.info(f"Inspecting epoch: {inspect_epoch}")

        # Validate epoch features
        diagnostics = validate_epoch_features(conn, inspect_epoch, verbose=args.verbose)

        # Check proxy consistency
        is_consistent, warnings = validate_proxy_consistency(conn)

        logger.info("=" * 80)
        logger.info("Validation Results:")
        logger.info(f"  Total features: {diagnostics['total_features']}")
        logger.info(
            f"  Overall proxy coverage: {diagnostics['overall_coverage']:.1%}"
        )
        logger.info(
            f"  Gauges missing drift: {len(diagnostics['gauges_missing_drift'])}"
        )
        logger.info(
            f"  Gauges missing uplift: {len(diagnostics['gauges_missing_uplift'])}"
        )
        logger.info(f"  Proxy consistency: {'✓ VALID' if is_consistent else '✗ INVALID'}")

        if warnings:
            logger.warning(f"  Warnings ({len(warnings)}):")
            for w in warnings:
                logger.warning(f"    {w}")
        logger.info("=" * 80)

        conn.close()

    except Exception as e:
        logger.error(f"✗ Validation failed: {e}", exc_info=True)
        raise


if __name__ == "__main__":
    cli_inspect_epoch()
