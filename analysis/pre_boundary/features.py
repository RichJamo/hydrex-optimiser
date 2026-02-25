"""
P3 Feature Engineering Layer

Load pre-boundary snapshots and construct model-ready features with data quality validation.
"""

import sqlite3
import logging
from typing import Dict, List, Tuple, Any, Sequence, Optional
from pathlib import Path

logger = logging.getLogger(__name__)


def build_snapshot_features(
    conn: sqlite3.Connection,
    epoch: int,
    decision_windows: Sequence[str] = ("day", "T-1", "boundary"),
    min_data_quality_score: float = 0.5,
) -> Dict[str, List[Dict[str, Any]]]:
    """
    Load preboundary_snapshots for epoch across all decision_windows.
    Filter rows where data_quality_score >= min_data_quality_score.

    Args:
        conn: SQLite connection
        epoch: boundary epoch to load
        decision_windows: windows to load ("day", "T-1", "boundary")
        min_data_quality_score: minimum quality threshold (0.0-1.0)

    Returns:
        Dict[window_name] → List[feature dicts]
        Feature dict schema:
        {
            'gauge_address': str,
            'pool_address': str,
            'votes_now_raw': int,
            'rewards_now_usd': float,
            'inclusion_prob': float,
            'data_quality_score': float,
        }
    """
    features_by_window = {}

    for window in decision_windows:
        cursor = conn.cursor()
        try:
            cursor.execute(
                """
                SELECT
                    gauge_address,
                    pool_address,
                    votes_now_raw,
                    rewards_now_usd,
                    inclusion_prob,
                    data_quality_score,
                    decision_timestamp,
                    decision_block
                FROM preboundary_snapshots
                WHERE epoch = ? AND decision_window = ? AND data_quality_score >= ?
                ORDER BY gauge_address
                """,
                (epoch, window, min_data_quality_score),
            )
            rows = cursor.fetchall()
            logger.debug(
                f"✓ Loaded {len(rows)} features for epoch {epoch}, window {window}"
            )

            features = []
            for row in rows:
                feature_dict = {
                    "gauge_address": row[0],
                    "pool_address": row[1],
                    "votes_now_raw": row[2],
                    "rewards_now_usd": row[3],
                    "inclusion_prob": row[4],
                    "data_quality_score": row[5],
                    "decision_timestamp": row[6],
                    "decision_block": row[7],
                }
                features.append(feature_dict)

            features_by_window[window] = features

        except Exception as e:
            logger.error(f"Error loading features for epoch {epoch}, window {window}: {e}")
            features_by_window[window] = []

    return features_by_window


def compute_feature_statistics(
    conn: sqlite3.Connection,
    epoch: int,
) -> Dict[str, Any]:
    """
    Compute per-window statistics for diagnostics.

    Returns:
        {
            'window': {
                'count': int,
                'votes_p50': float,
                'rewards_p50': float,
                'data_quality_p50': float,
                'inclusion_prob_p50': float,
            }
        }
    """
    stats = {}
    windows = ["day", "T-1", "boundary"]

    for window in windows:
        cursor = conn.cursor()
        try:
            cursor.execute(
                """
                SELECT
                    COUNT(*) as count,
                    CAST(SUM(votes_now_raw) AS FLOAT) / COUNT(*) as votes_avg,
                    CAST(SUM(rewards_now_usd) AS FLOAT) / COUNT(*) as rewards_avg,
                    CAST(SUM(data_quality_score) AS FLOAT) / COUNT(*) as quality_avg,
                    CAST(SUM(inclusion_prob) AS FLOAT) / COUNT(*) as inclusion_avg
                FROM preboundary_snapshots
                WHERE epoch = ? AND decision_window = ?
                """,
                (epoch, window),
            )
            row = cursor.fetchone()
            if row and row[0] > 0:
                stats[window] = {
                    "count": int(row[0]),
                    "votes_avg": row[1],
                    "rewards_avg": row[2],
                    "data_quality_avg": row[3],
                    "inclusion_prob_avg": row[4],
                }
            else:
                stats[window] = {
                    "count": 0,
                    "votes_avg": None,
                    "rewards_avg": None,
                    "data_quality_avg": None,
                    "inclusion_prob_avg": None,
                }
        except Exception as e:
            logger.error(f"Error computing stats for epoch {epoch}, window {window}: {e}")
            stats[window] = {"count": 0}

    return stats


def validate_features(
    features_by_window: Dict[str, List[Dict]],
    min_features_per_window: int = 10,
) -> Tuple[bool, List[str]]:
    """
    Validate feature completeness + quality.

    Checks:
    - All required keys present
    - No NaN/Inf values
    - Sufficient feature count per window

    Returns:
        (is_valid: bool, warnings: List[str])
    """
    required_keys = {
        "gauge_address",
        "pool_address",
        "votes_now_raw",
        "rewards_now_usd",
        "inclusion_prob",
        "data_quality_score",
    }
    warnings = []
    all_valid = True

    for window, features in features_by_window.items():
        if len(features) < min_features_per_window:
            warnings.append(
                f"⚠ Window {window}: only {len(features)} features (expected >= {min_features_per_window})"
            )
            all_valid = False

        for i, feature in enumerate(features):
            # Check required keys
            if not required_keys.issubset(feature.keys()):
                missing = required_keys - feature.keys()
                warnings.append(
                    f"⚠ Window {window}, feature {i}: missing keys {missing}"
                )
                all_valid = False

            # Check for NaN/Inf
            for key in required_keys:
                if key in feature:
                    val = feature[key]
                    if isinstance(val, float):
                        if val != val:  # NaN check
                            warnings.append(
                                f"⚠ Window {window}, feature {i}, {key}: NaN value"
                            )
                            all_valid = False
                        elif val == float("inf") or val == float("-inf"):
                            warnings.append(
                                f"⚠ Window {window}, feature {i}, {key}: Inf value"
                            )
                            all_valid = False

    if all_valid and not warnings:
        logger.info(
            f"✓ Features validation passed: {sum(len(f) for f in features_by_window.values())} total features across {len(features_by_window)} windows"
        )

    return all_valid, warnings
