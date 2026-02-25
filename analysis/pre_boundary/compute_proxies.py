"""
P3 Proxy Pre-Computation & Caching

Pre-compute vote drift and reward uplift estimates for all decision windows
and cache as JSON for fast retrieval during backtest.
"""

import sqlite3
import json
import logging
import argparse
from pathlib import Path
from datetime import datetime
from typing import Dict, Any, Optional

from analysis.pre_boundary.proxies import (
    learn_vote_drift_by_window,
    learn_reward_uplift_by_window,
)
from config.preboundary_settings import DECISION_WINDOWS, make_logging_dir

logger = logging.getLogger(__name__)


def compute_and_cache_proxies(
    conn: sqlite3.Connection,
    db_path: str = "data/db/preboundary_dev.db",
    output_dir: str = "data/preboundary_cache",
) -> Dict[str, str]:
    """
    Pre-compute all vote drift and reward uplift proxies for all decision windows.
    Cache as JSON in output_dir.

    Args:
        conn: SQLite connection
        db_path: path to database (for reference)
        output_dir: where to write cache files

    Returns:
        Dict[window] → cache_file_path
        Files produced:
        - data/preboundary_cache/drift_estimates_day.json
        - data/preboundary_cache/uplift_estimates_day.json
        - data/preboundary_cache/drift_estimates_T-1.json
        - data/preboundary_cache/uplift_estimates_T-1.json
        - data/preboundary_cache/drift_estimates_boundary.json
        - data/preboundary_cache/uplift_estimates_boundary.json
    """
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)

    cache_files = {}

    try:
        for window in DECISION_WINDOWS.keys():
            logger.info(f"🔄 Computing proxies for window: {window}")

            # Learn drift
            drift_estimates = learn_vote_drift_by_window(
                conn, window, min_sample_size=6
            )

            # Learn uplift
            uplift_estimates = learn_reward_uplift_by_window(
                conn, window, min_sample_size=6
            )

            # Cache drift
            drift_file = output_path / f"drift_estimates_{window}.json"
            drift_data = {
                "metadata": {
                    "computed_at": datetime.utcnow().isoformat() + "Z",
                    "decision_window": window,
                    "num_gauges": len(drift_estimates),
                    "db_path": str(db_path),
                },
                "estimates": [
                    est.to_dict() for est in drift_estimates.values()
                ],
            }
            with open(drift_file, "w") as f:
                json.dump(drift_data, f, indent=2)
            logger.info(f"✓ Cached drift estimates: {drift_file}")
            cache_files[f"drift_{window}"] = str(drift_file)

            # Cache uplift
            uplift_file = output_path / f"uplift_estimates_{window}.json"
            uplift_data = {
                "metadata": {
                    "computed_at": datetime.utcnow().isoformat() + "Z",
                    "decision_window": window,
                    "num_gauges": len(uplift_estimates),
                    "db_path": str(db_path),
                },
                "estimates": [
                    est.to_dict() for est in uplift_estimates.values()
                ],
            }
            with open(uplift_file, "w") as f:
                json.dump(uplift_data, f, indent=2)
            logger.info(f"✓ Cached uplift estimates: {uplift_file}")
            cache_files[f"uplift_{window}"] = str(uplift_file)

    except Exception as e:
        logger.error(f"Error computing proxies: {e}")
        raise

    logger.info(f"✓ Proxy computation complete: {len(cache_files)} cache files created")
    return cache_files


def load_proxy_cache(
    cache_dir: str = "data/preboundary_cache",
    decision_window: str = "day",
    proxy_type: str = "drift",  # or "uplift"
) -> Dict[str, Any]:
    """
    Load cached proxies from JSON file.

    Args:
        cache_dir: directory containing cache files
        decision_window: which window ("day", "T-1", "boundary")
        proxy_type: "drift" or "uplift"

    Returns:
        Dict with metadata + estimates list
    """
    cache_file = Path(cache_dir) / f"{proxy_type}_estimates_{decision_window}.json"

    if not cache_file.exists():
        logger.warning(f"Cache file not found: {cache_file}")
        return {}

    try:
        with open(cache_file, "r") as f:
            data = json.load(f)
        logger.debug(f"✓ Loaded cache: {cache_file}")
        return data
    except Exception as e:
        logger.error(f"Error loading cache {cache_file}: {e}")
        return {}


def cli_main():
    """
    CLI entry point for proxy pre-computation.
    
    Usage:
      python -m analysis.pre_boundary.compute_proxies \
        --db-path data/db/preboundary_dev.db \
        --output-dir data/preboundary_cache \
        --log-file data/db/logs/compute_proxies.log
    """
    parser = argparse.ArgumentParser(
        description="Pre-compute and cache proxy estimates for all decision windows"
    )
    parser.add_argument(
        "--db-path",
        default="data/db/preboundary_dev.db",
        help="Path to preboundary database",
    )
    parser.add_argument(
        "--output-dir",
        default="data/preboundary_cache",
        help="Directory to write cache files",
    )
    parser.add_argument(
        "--log-file",
        default="data/db/logs/compute_proxies.log",
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
    logger.info("P3 Proxy Pre-Computation Started")
    logger.info(f"DB: {args.db_path}")
    logger.info(f"Output: {args.output_dir}")
    logger.info("=" * 80)

    try:
        conn = sqlite3.connect(args.db_path)
        cache_files = compute_and_cache_proxies(
            conn,
            db_path=args.db_path,
            output_dir=args.output_dir,
        )
        conn.close()

        logger.info("=" * 80)
        logger.info(f"✓ Proxy pre-computation complete")
        logger.info(f"Cache files: {len(cache_files)}")
        for key, path in cache_files.items():
            logger.info(f"  - {key}: {path}")
        logger.info("=" * 80)

    except Exception as e:
        logger.error(f"✗ Proxy pre-computation failed: {e}", exc_info=True)
        raise


if __name__ == "__main__":
    cli_main()
