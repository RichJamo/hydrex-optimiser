"""
P4 Optimizer Runner

Orchestrator to run full P3→P4 optimization pipeline and populate preboundary_forecasts table.
"""

import sqlite3
import logging
import argparse
from pathlib import Path
from datetime import datetime
from typing import Dict, Optional

from analysis.pre_boundary.features import build_snapshot_features
from analysis.pre_boundary.scenarios import (
    build_scenarios_for_epoch,
    validate_scenarios,
)
from analysis.pre_boundary.optimizer import optimize_allocation
from config.preboundary_settings import (
    LAMBDA_RISK,
    K_MAX,
    MIN_VOTES_PER_POOL,
    DECISION_WINDOWS,
    make_logging_dir,
)

logger = logging.getLogger(__name__)


def populate_forecasts_for_epoch(
    conn: sqlite3.Connection,
    epoch: int,
    voting_power: float = 1_000_000,
    cache_dir: str = "data/preboundary_cache",
    log_file: Optional[str] = None,
) -> Dict[str, int]:
    """
    Run full P3→P4 pipeline for a single epoch:
    1. Load features (P3)
    2. Build scenarios
    3. Optimize allocation per window
    4. Populate preboundary_forecasts table

    Returns:
        Dict[window] → rows_inserted
    """
    results = {}

    try:
        logger.info(f"Processing epoch {epoch}")

        # For each decision window
        for window in DECISION_WINDOWS.keys():
            logger.info(f"🔄 Window: {window}")

            # Load features
            features = build_snapshot_features(conn, epoch, [window])

            if not features.get(window):
                logger.warning(f"No features for epoch {epoch}, window {window}")
                results[window] = 0
                continue

            feature_list = features[window]
            logger.info(f"  Features: {len(feature_list)}")

            # Build scenarios
            scenarios = build_scenarios_for_epoch(
                conn, epoch, window, cache_dir=cache_dir
            )

            # Validate scenarios
            is_valid, warnings = validate_scenarios(scenarios)
            if not is_valid:
                logger.warning(f"  Scenario validation issues: {len(warnings)}")
                for w in warnings[:3]:
                    logger.warning(f"    - {w}")

            # Run optimization
            opt_result = optimize_allocation(
                feature_list,
                scenarios,
                voting_power=voting_power,
                lambda_risk=LAMBDA_RISK,
                k_max=K_MAX,
                min_votes_per_pool=MIN_VOTES_PER_POOL,
            )

            # Insert into forecasts table
            allocation = opt_result.get("allocation", {})
            num_inserted = _upsert_forecasts(
                conn,
                epoch=epoch,
                decision_window=window,
                allocation=allocation,
                opt_result=opt_result,
            )

            results[window] = num_inserted
            logger.info(
                f"  ✓ {num_inserted} rows inserted, "
                f"return={opt_result.get('expected_return', 0.0):.6f}"
            )

    except Exception as e:
        logger.error(f"Error processing epoch {epoch}: {e}", exc_info=True)

    return results


def _upsert_forecasts(
    conn: sqlite3.Connection,
    epoch: int,
    decision_window: str,
    allocation: Dict[str, float],
    opt_result: Dict,
) -> int:
    """Upsert allocation results into preboundary_forecasts table."""
    cursor = conn.cursor()
    rows_inserted = 0

    try:
        for gauge_address, votes_recommended in allocation.items():
            cursor.execute(
                """
                INSERT INTO preboundary_forecasts (
                    epoch, decision_window, gauge_address,
                    votes_recommended,
                    portfolio_return_bps, portfolio_downside_bps, 
                    optimizer_status,
                    source_tag, computed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(epoch, decision_window, gauge_address) DO UPDATE SET
                    votes_recommended = excluded.votes_recommended,
                    portfolio_return_bps = excluded.portfolio_return_bps,
                    portfolio_downside_bps = excluded.portfolio_downside_bps,
                    optimizer_status = excluded.optimizer_status,
                    computed_at = excluded.computed_at
                """,
                (
                    epoch,
                    decision_window,
                    gauge_address,
                    int(votes_recommended),
                    int(opt_result.get("expected_return", 0.0)),
                    int(opt_result.get("downside_return", 0.0)),
                    opt_result.get("optimizer_status", "unknown"),
                    "P4_optimizer",
                    datetime.utcnow().isoformat(),
                ),
            )
            rows_inserted += 1

        conn.commit()

    except Exception as e:
        logger.error(f"Error upserting forecasts for epoch {epoch}, window {decision_window}: {e}")
        conn.rollback()

    return rows_inserted


def cli_main():
    """
    CLI entry point for P4 optimization.

    Usage:
      python -m analysis.pre_boundary.optimizer_runner \
        --db-path data/db/preboundary_dev.db \
        --cache-dir data/preboundary_cache \
        --voting-power 1000000 \
        --recent-epochs 1 \
        --log-file data/db/logs/optimizer.log
    """
    parser = argparse.ArgumentParser(
        description="Run P4 optimization and populate preboundary_forecasts"
    )
    parser.add_argument(
        "--db-path",
        default="data/db/preboundary_dev.db",
        help="Path to preboundary database",
    )
    parser.add_argument(
        "--cache-dir",
        default="data/preboundary_cache",
        help="Directory with cached proxies from P3",
    )
    parser.add_argument(
        "--voting-power",
        type=float,
        default=1_000_000,
        help="Total voting power to allocate",
    )
    parser.add_argument(
        "--recent-epochs",
        type=int,
        help="Process N most recent epochs",
    )
    parser.add_argument(
        "--epoch",
        type=int,
        help="Process single epoch",
    )
    parser.add_argument(
        "--log-file",
        default="data/db/logs/optimizer.log",
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
    logger.info("P4 Optimizer Started")
    logger.info(f"DB: {args.db_path}")
    logger.info(f"Cache: {args.cache_dir}")
    logger.info(f"Voting power: {args.voting_power:,.0f}")
    logger.info("=" * 80)

    try:
        conn = sqlite3.connect(args.db_path)

        # Determine epochs to process
        epochs_to_process = []
        if args.epoch:
            epochs_to_process = [args.epoch]
        elif args.recent_epochs:
            cursor = conn.cursor()
            cursor.execute(
                f"SELECT DISTINCT epoch FROM preboundary_snapshots ORDER BY epoch DESC LIMIT {args.recent_epochs}"
            )
            epochs_to_process = [row[0] for row in cursor.fetchall()]

        if not epochs_to_process:
            logger.error("No epochs to process")
            return

        logger.info(f"Processing {len(epochs_to_process)} epochs: {epochs_to_process}")

        total_rows = 0
        for epoch in epochs_to_process:
            results = populate_forecasts_for_epoch(
                conn,
                epoch,
                voting_power=args.voting_power,
                cache_dir=args.cache_dir,
            )
            total_rows += sum(results.values())

        conn.close()

        logger.info("=" * 80)
        logger.info(f"✓ P4 Optimization complete")
        logger.info(f"  Total rows inserted: {total_rows}")
        logger.info("=" * 80)

    except Exception as e:
        logger.error(f"✗ P4 Optimization failed: {e}", exc_info=True)
        raise


if __name__ == "__main__":
    cli_main()
