"""Pre-boundary storage helpers.

Implements Phase P1 requirements:
- Schema creation for preboundary tables
- Idempotent upsert helpers
- Completeness checks per epoch/window
"""

from __future__ import annotations

import sqlite3
import time
from typing import Dict, List, Optional, Sequence, Tuple

DEFAULT_WINDOWS: Tuple[str, ...] = ("day", "T-1", "boundary")
DEFAULT_SCENARIOS: Tuple[str, ...] = ("conservative", "base", "aggressive")


def ensure_preboundary_tables(conn: sqlite3.Connection) -> None:
    cur = conn.cursor()

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS preboundary_snapshots (
            epoch INTEGER NOT NULL,
            decision_window TEXT NOT NULL,
            decision_timestamp INTEGER NOT NULL,
            decision_block INTEGER NOT NULL,
            boundary_timestamp INTEGER NOT NULL,
            boundary_block INTEGER,
            gauge_address TEXT NOT NULL,
            pool_address TEXT,
            votes_now_raw REAL NOT NULL,
            rewards_now_usd REAL NOT NULL,
            inclusion_prob REAL,
            data_quality_score REAL,
            source_tag TEXT,
            computed_at INTEGER NOT NULL,
            PRIMARY KEY (epoch, decision_window, gauge_address)
        )
        """
    )

    cur.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_preboundary_snapshots_lookup
        ON preboundary_snapshots(epoch, decision_window, decision_timestamp, gauge_address)
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS preboundary_forecasts (
            epoch INTEGER NOT NULL,
            decision_window TEXT NOT NULL,
            gauge_address TEXT NOT NULL,
            scenario TEXT NOT NULL,
            votes_final_raw REAL NOT NULL,
            rewards_final_usd REAL NOT NULL,
            expected_return_usd REAL NOT NULL,
            confidence_penalty REAL NOT NULL,
            computed_at INTEGER NOT NULL,
            PRIMARY KEY (epoch, decision_window, gauge_address, scenario)
        )
        """
    )

    # Backward compatibility: some existing DBs have a legacy preboundary_forecasts
    # schema without the `scenario` column.
    forecast_cols = {
        row[1]
        for row in cur.execute("PRAGMA table_info(preboundary_forecasts)").fetchall()
    }
    if "scenario" in forecast_cols:
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_preboundary_forecasts_lookup
            ON preboundary_forecasts(epoch, decision_window, scenario, gauge_address)
            """
        )
    else:
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_preboundary_forecasts_lookup_legacy
            ON preboundary_forecasts(epoch, decision_window, gauge_address)
            """
        )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS preboundary_recommendations (
            epoch INTEGER NOT NULL,
            decision_window TEXT NOT NULL,
            run_id TEXT NOT NULL,
            gauge_address TEXT NOT NULL,
            alloc_votes REAL NOT NULL,
            expected_return_usd REAL NOT NULL,
            downside_p10_usd REAL,
            inclusion_risk TEXT,
            delta_votes REAL,
            no_change_flag INTEGER NOT NULL,
            computed_at INTEGER NOT NULL,
            PRIMARY KEY (epoch, decision_window, run_id, gauge_address)
        )
        """
    )

    cur.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_preboundary_recommendations_lookup
        ON preboundary_recommendations(epoch, decision_window, run_id, gauge_address)
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS preboundary_backtest_results (
            epoch INTEGER NOT NULL,
            decision_window TEXT NOT NULL,
            run_id TEXT NOT NULL,
            expected_return_usd REAL NOT NULL,
            realized_return_usd REAL,
            p10_return_usd REAL,
            regret_usd REAL,
            calibration_error REAL,
            computed_at INTEGER NOT NULL,
            PRIMARY KEY (epoch, decision_window, run_id)
        )
        """
    )

    cur.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_preboundary_backtest_lookup
        ON preboundary_backtest_results(epoch, decision_window, run_id)
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS preboundary_truth_labels (
            epoch INTEGER NOT NULL,
            vote_epoch INTEGER NOT NULL,
            gauge_address TEXT NOT NULL,
            final_votes_raw REAL NOT NULL,
            final_rewards_usd REAL NOT NULL,
            source_tag TEXT,
            computed_at INTEGER NOT NULL,
            PRIMARY KEY (epoch, vote_epoch, gauge_address)
        )
        """
    )

    cur.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_preboundary_truth_labels_lookup
        ON preboundary_truth_labels(epoch, vote_epoch, gauge_address)
        """
    )

    conn.commit()


def upsert_preboundary_snapshots(
    conn: sqlite3.Connection,
    rows: Sequence[Tuple[int, str, int, int, int, Optional[int], str, Optional[str], float, float, Optional[float], Optional[float], Optional[str]]],
) -> int:
    if not rows:
        return 0

    now_ts = int(time.time())
    cur = conn.cursor()
    cur.executemany(
        """
        INSERT OR REPLACE INTO preboundary_snapshots(
            epoch, decision_window, decision_timestamp, decision_block,
            boundary_timestamp, boundary_block, gauge_address, pool_address,
            votes_now_raw, rewards_now_usd, inclusion_prob, data_quality_score,
            source_tag, computed_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [(*row, now_ts) for row in rows],
    )
    conn.commit()
    return len(rows)


def upsert_preboundary_forecasts(
    conn: sqlite3.Connection,
    rows: Sequence[Tuple[int, str, str, str, float, float, float, float]],
) -> int:
    if not rows:
        return 0

    now_ts = int(time.time())
    cur = conn.cursor()
    cur.executemany(
        """
        INSERT OR REPLACE INTO preboundary_forecasts(
            epoch, decision_window, gauge_address, scenario,
            votes_final_raw, rewards_final_usd, expected_return_usd,
            confidence_penalty, computed_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [(*row, now_ts) for row in rows],
    )
    conn.commit()
    return len(rows)


def upsert_preboundary_recommendations(
    conn: sqlite3.Connection,
    rows: Sequence[Tuple[int, str, str, str, float, float, Optional[float], Optional[str], Optional[float], int]],
) -> int:
    if not rows:
        return 0

    now_ts = int(time.time())
    cur = conn.cursor()
    cur.executemany(
        """
        INSERT OR REPLACE INTO preboundary_recommendations(
            epoch, decision_window, run_id, gauge_address,
            alloc_votes, expected_return_usd, downside_p10_usd,
            inclusion_risk, delta_votes, no_change_flag, computed_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [(*row, now_ts) for row in rows],
    )
    conn.commit()
    return len(rows)


def upsert_preboundary_backtest_results(
    conn: sqlite3.Connection,
    rows: Sequence[Tuple[int, str, str, float, Optional[float], Optional[float], Optional[float], Optional[float]]],
) -> int:
    if not rows:
        return 0

    now_ts = int(time.time())
    cur = conn.cursor()
    cur.executemany(
        """
        INSERT OR REPLACE INTO preboundary_backtest_results(
            epoch, decision_window, run_id,
            expected_return_usd, realized_return_usd,
            p10_return_usd, regret_usd, calibration_error,
            computed_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [(*row, now_ts) for row in rows],
    )
    conn.commit()
    return len(rows)


def upsert_truth_labels_from_boundary(
    conn: sqlite3.Connection,
    epoch: int,
    vote_epoch: int,
    active_only: int = 1,
) -> int:
    """Materialize post-boundary truth labels from existing boundary tables.

    Source tables:
    - boundary_gauge_values (final denominator votes)
    - boundary_reward_snapshots (final rewards aggregated to gauge)
    """
    cur = conn.cursor()
    now_ts = int(time.time())

    cur.execute(
        """
        INSERT OR REPLACE INTO preboundary_truth_labels(
            epoch, vote_epoch, gauge_address,
            final_votes_raw, final_rewards_usd,
            source_tag, computed_at
        )
        SELECT
            g.epoch,
            g.vote_epoch,
            lower(g.gauge_address) AS gauge_address,
            COALESCE(g.votes_raw, 0.0) AS final_votes_raw,
            COALESCE(r.total_rewards_usd, 0.0) AS final_rewards_usd,
            'boundary_tables' AS source_tag,
            ? AS computed_at
        FROM boundary_gauge_values g
        LEFT JOIN (
            SELECT
                epoch,
                vote_epoch,
                active_only,
                lower(gauge_address) AS gauge_address,
                SUM(COALESCE(total_usd, 0.0)) AS total_rewards_usd
            FROM boundary_reward_snapshots
            GROUP BY epoch, vote_epoch, active_only, lower(gauge_address)
        ) r
          ON r.epoch = g.epoch
         AND r.vote_epoch = g.vote_epoch
         AND r.active_only = g.active_only
         AND r.gauge_address = lower(g.gauge_address)
        WHERE g.epoch = ?
          AND g.vote_epoch = ?
          AND g.active_only = ?
        """,
        (now_ts, int(epoch), int(vote_epoch), int(active_only)),
    )

    rows = cur.rowcount if cur.rowcount is not None else 0
    conn.commit()
    return int(rows)


def get_truth_label_coverage(conn: sqlite3.Connection, epoch: int, vote_epoch: int) -> Dict[str, object]:
    cur = conn.cursor()
    labels = cur.execute(
        """
        SELECT COUNT(*)
        FROM preboundary_truth_labels
        WHERE epoch = ? AND vote_epoch = ?
        """,
        (int(epoch), int(vote_epoch)),
    ).fetchone()[0]

    boundary_rows = cur.execute(
        """
        SELECT COUNT(*)
        FROM boundary_gauge_values
        WHERE epoch = ? AND vote_epoch = ? AND active_only = 1
        """,
        (int(epoch), int(vote_epoch)),
    ).fetchone()[0]

    return {
        "epoch": int(epoch),
        "vote_epoch": int(vote_epoch),
        "truth_label_rows": int(labels),
        "boundary_gauge_rows": int(boundary_rows),
        "coverage_ratio": (float(labels) / float(boundary_rows)) if boundary_rows > 0 else 0.0,
        "labels_complete": bool(boundary_rows > 0 and labels >= boundary_rows),
    }


def get_preboundary_completeness(
    conn: sqlite3.Connection,
    epoch: int,
    expected_windows: Sequence[str] = DEFAULT_WINDOWS,
    expected_scenarios: Sequence[str] = DEFAULT_SCENARIOS,
) -> Dict[str, object]:
    cur = conn.cursor()

    snapshot_windows = set(
        row[0]
        for row in cur.execute(
            """
            SELECT DISTINCT decision_window
            FROM preboundary_snapshots
            WHERE epoch = ?
            """,
            (epoch,),
        ).fetchall()
    )

    forecast_windows = set(
        row[0]
        for row in cur.execute(
            """
            SELECT DISTINCT decision_window
            FROM preboundary_forecasts
            WHERE epoch = ?
            """,
            (epoch,),
        ).fetchall()
    )

    rec_windows = set(
        row[0]
        for row in cur.execute(
            """
            SELECT DISTINCT decision_window
            FROM preboundary_recommendations
            WHERE epoch = ?
            """,
            (epoch,),
        ).fetchall()
    )

    scenario_counts = {
        window: cur.execute(
            """
            SELECT COUNT(DISTINCT scenario)
            FROM preboundary_forecasts
            WHERE epoch = ? AND decision_window = ?
            """,
            (epoch, window),
        ).fetchone()[0]
        for window in expected_windows
    }

    snapshots_complete = all(window in snapshot_windows for window in expected_windows)
    forecasts_complete = all(
        window in forecast_windows and scenario_counts.get(window, 0) >= len(expected_scenarios)
        for window in expected_windows
    )
    recommendations_complete = all(window in rec_windows for window in expected_windows)

    return {
        "epoch": epoch,
        "expected_windows": list(expected_windows),
        "snapshots_windows_present": sorted(snapshot_windows),
        "forecasts_windows_present": sorted(forecast_windows),
        "recommendations_windows_present": sorted(rec_windows),
        "forecast_scenario_counts": scenario_counts,
        "snapshots_complete": snapshots_complete,
        "forecasts_complete": forecasts_complete,
        "recommendations_complete": recommendations_complete,
        "epoch_complete": snapshots_complete and forecasts_complete and recommendations_complete,
    }


def materialize_preboundary_snapshots_for_epoch(
    conn: sqlite3.Connection,
    epoch: int,
    decision_windows: Sequence[str] = ("day", "T-1", "boundary"),
    min_reward_usd: float = 100.0,
    snapshot_source: str = "raw_asof",
) -> Dict[str, List[Tuple]]:
    """
        Materialize snapshot rows for a single epoch across all decision windows.

        Supported sources:
        - raw_asof: uses raw `votes` and `bribes` filtered up to each decision timestamp
            (prevents look-ahead leakage)
        - boundary_derived: legacy mode using boundary-final tables for values

    Args:
        conn: database connection (should be connected to preboundary_dev.db)
        epoch: boundary epoch to materialize snapshots for
        decision_windows: windows to include ("day", "T-1", "boundary")
        min_reward_usd: minimum total USD reward per gauge to include

    Returns:
        Dict[window_name] -> List[snapshot_row_tuples]
        Each row tuple has 13 elements:
          (epoch, decision_window, decision_timestamp, decision_block,
           boundary_timestamp, boundary_block, gauge_address, pool_address,
           votes_now_raw, rewards_now_usd, inclusion_prob, data_quality_score, source_tag)

        Data construction:
            - decision_timestamp = boundary_timestamp - window_seconds_before
            - decision_block ≈ boundary_block - (window_seconds_before // 12)
            - inclusion_prob from config heuristics per window
            - data_quality_score based on votes/rewards availability per gauge/window
    """
    from config.preboundary_settings import (
        DECISION_WINDOWS,
        INCLUSION_PROB_BY_WINDOW,
        BLOCK_TIME_ESTIMATE_SECONDS,
    )

    if snapshot_source not in {"raw_asof", "boundary_derived"}:
        raise ValueError(f"Unsupported snapshot_source={snapshot_source}")

    result = {window: [] for window in decision_windows}

    cur = conn.cursor()

    # Fetch gauge universe + boundary metadata for this epoch
    gauge_query = """
        SELECT
            epoch,
            vote_epoch,
            boundary_block,
            lower(gauge_address) AS gauge_address,
            pool_address
        FROM boundary_gauge_values
        WHERE epoch = ? AND active_only = 1 AND COALESCE(votes_raw, 0) > 0
    """

    gauge_rows = {}
    for row in cur.execute(gauge_query, (epoch,)).fetchall():
        epoch_val, vote_epoch, boundary_block, gauge_addr, pool_addr = row
        gauge_rows[gauge_addr.lower()] = {
            "epoch": epoch_val,
            "vote_epoch": vote_epoch,
            "boundary_block": boundary_block,
            "gauge_address": gauge_addr.lower(),
            "pool_address": pool_addr if pool_addr else gauge_addr,
        }

    # Get boundary timestamp (epoch value IS the boundary timestamp)
    boundary_timestamp = int(epoch)

    # Legacy source: boundary-final data copied to all windows (kept for comparison/debugging)
    if snapshot_source == "boundary_derived":
        reward_query = """
            SELECT
                lower(gauge_address) AS gauge_address,
                SUM(COALESCE(total_usd, 0.0)) AS total_rewards_usd
            FROM boundary_reward_snapshots
            WHERE epoch = ? AND active_only = 1
            GROUP BY lower(gauge_address)
        """

        rewards_by_gauge = {}
        for row in cur.execute(reward_query, (epoch,)).fetchall():
            gauge_addr, total_usd = row
            rewards_by_gauge[gauge_addr.lower()] = float(total_usd) if total_usd else 0.0

        votes_by_gauge = {}
        votes_query = """
            SELECT lower(gauge_address) AS gauge_address, COALESCE(votes_raw, 0)
            FROM boundary_gauge_values
            WHERE epoch = ? AND active_only = 1
        """
        for row in cur.execute(votes_query, (epoch,)).fetchall():
            votes_by_gauge[row[0].lower()] = float(row[1] or 0.0)

        for gauge_addr, gauge_data in gauge_rows.items():
            rewards_now_usd = rewards_by_gauge.get(gauge_addr, 0.0)
            if rewards_now_usd < min_reward_usd:
                continue

            votes_now_raw = votes_by_gauge.get(gauge_addr, 0.0)
            if votes_now_raw > 0 and rewards_now_usd > 0 and gauge_data["pool_address"]:
                data_quality_score = 1.0
            elif votes_now_raw > 0 and rewards_now_usd > 0:
                data_quality_score = 0.8
            else:
                data_quality_score = 0.5

            for window in decision_windows:
                if window not in DECISION_WINDOWS:
                    continue

                window_config = DECISION_WINDOWS[window]
                seconds_before = window_config["seconds_before_boundary"]
                decision_timestamp = boundary_timestamp - seconds_before
                decision_block = max(
                    0,
                    gauge_data["boundary_block"] - (seconds_before // BLOCK_TIME_ESTIMATE_SECONDS),
                )
                inclusion_prob = INCLUSION_PROB_BY_WINDOW.get(window, 0.5)

                row_tuple = (
                    epoch,
                    window,
                    int(decision_timestamp),
                    int(decision_block),
                    boundary_timestamp,
                    gauge_data["boundary_block"],
                    gauge_addr,
                    gauge_data["pool_address"],
                    float(votes_now_raw),
                    float(rewards_now_usd),
                    float(inclusion_prob),
                    float(data_quality_score),
                    "boundary_tables",  # source_tag
                )
                result[window].append(row_tuple)

        return result

    # Source: raw_asof (default)
    for window in decision_windows:
        if window not in DECISION_WINDOWS:
            continue

        window_config = DECISION_WINDOWS[window]
        seconds_before = int(window_config["seconds_before_boundary"])
        decision_timestamp = int(boundary_timestamp - seconds_before)

        votes_asof_query = """
            SELECT v.gauge_address, v.total_votes
            FROM (
                SELECT lower(v1.gauge) AS gauge_address, v1.total_votes, v1.indexed_at
                FROM votes v1
                JOIN (
                    SELECT lower(gauge) AS gauge_address, MAX(indexed_at) AS latest_indexed_at
                    FROM votes
                    WHERE epoch = ? AND indexed_at <= ?
                    GROUP BY lower(gauge)
                ) latest
                ON lower(v1.gauge) = latest.gauge_address AND v1.indexed_at = latest.latest_indexed_at
                WHERE v1.epoch = ?
            ) v
        """
        votes_by_gauge = {
            row[0].lower(): float(row[1] or 0.0)
            for row in cur.execute(votes_asof_query, (epoch, decision_timestamp, epoch)).fetchall()
            if row and row[0]
        }

        rewards_asof_query = """
            SELECT lower(gauge_address) AS gauge_address, SUM(COALESCE(usd_value, 0.0)) AS total_rewards_usd
            FROM bribes
            WHERE epoch = ?
              AND timestamp <= ?
              AND gauge_address IS NOT NULL
              AND gauge_address != ''
            GROUP BY lower(gauge_address)
        """
        rewards_by_gauge = {
            row[0].lower(): float(row[1] or 0.0)
            for row in cur.execute(rewards_asof_query, (epoch, decision_timestamp)).fetchall()
            if row and row[0]
        }

        for gauge_addr, gauge_data in gauge_rows.items():
            votes_now_raw = float(votes_by_gauge.get(gauge_addr, 0.0))
            rewards_now_usd = float(rewards_by_gauge.get(gauge_addr, 0.0))

            if votes_now_raw <= 0 and rewards_now_usd < min_reward_usd:
                continue

            if votes_now_raw > 0 and rewards_now_usd > 0:
                data_quality_score = 1.0
            elif votes_now_raw > 0 or rewards_now_usd > 0:
                data_quality_score = 0.75
            else:
                data_quality_score = 0.5

            decision_block = max(
                0,
                int(gauge_data["boundary_block"]) - (seconds_before // BLOCK_TIME_ESTIMATE_SECONDS),
            )
            inclusion_prob = INCLUSION_PROB_BY_WINDOW.get(window, 0.5)

            row_tuple = (
                epoch,
                window,
                int(decision_timestamp),
                int(decision_block),
                boundary_timestamp,
                int(gauge_data["boundary_block"]),
                gauge_addr,
                gauge_data["pool_address"],
                float(votes_now_raw),
                float(rewards_now_usd),
                float(inclusion_prob),
                float(data_quality_score),
                "raw_asof_tables",  # source_tag
            )
            result[window].append(row_tuple)

    return result


def get_gauges_for_epoch_with_mapping(
    conn: sqlite3.Connection,
    epoch: int,
    active_only: int = 1,
) -> Dict[str, Tuple[str, str, str]]:
    """
    Fetch gauge list for epoch with pool/bribe mappings.

    Attempts to use gauge_bribe_mapping table first, falls back to gauges+bribes join.

    Args:
        conn: database connection
        epoch: boundary epoch
        active_only: filter by active_only flag

    Returns:
        Dict[gauge_address] -> (pool_address, internal_bribe, external_bribe)
    """
    cur = conn.cursor()

    # Try gauge_bribe_mapping table first
    try:
        cur.execute(
            """
            SELECT
                m.gauge_address,
                lower(COALESCE(g.pool, g.address)) AS pool_address,
                m.internal_bribe,
                m.external_bribe
            FROM gauge_bribe_mapping m
            JOIN gauges g ON lower(g.address) = m.gauge_address
            ORDER BY lower(g.address)
            """
        )
        rows = cur.fetchall()
        if rows:
            return {
                row[0].lower(): (row[1], row[2], row[3])
                for row in rows
                if row and row[0]
            }
    except sqlite3.OperationalError:
        pass

    # Fallback: gauges + bribes join
    cur.execute(
        """
        SELECT DISTINCT
            lower(g.address) AS gauge_address,
            lower(COALESCE(g.pool, g.address)) AS pool_address,
            lower(COALESCE(g.internal_bribe, '')) AS internal_bribe,
            lower(COALESCE(g.external_bribe, '')) AS external_bribe
        FROM gauges g
        JOIN bribes b ON lower(b.gauge_address) = lower(g.address)
        WHERE b.epoch = ?
        """,
        (epoch,),
    )
    rows = cur.fetchall()
    return {
        row[0].lower(): (row[1], row[2], row[3])
        for row in rows
        if row and row[0]
    }


def get_preboundary_epoch_snapshot_count(
    conn: sqlite3.Connection,
    epoch: int,
    decision_window: str,
) -> int:
    """
    Count existing snapshot rows for (epoch, decision_window).

    Used for resume/completeness checks.

    Args:
        conn: database connection
        epoch: boundary epoch
        decision_window: window name

    Returns:
        Row count for that partition key
    """
    cur = conn.cursor()
    result = cur.execute(
        """
        SELECT COUNT(*)
        FROM preboundary_snapshots
        WHERE epoch = ? AND decision_window = ?
        """,
        (epoch, decision_window),
    ).fetchone()
    return result[0] if result else 0


def get_incomplete_decision_windows(
    conn: sqlite3.Connection,
    epoch: int,
    expected_windows: Sequence[str] = ("day", "T-1", "boundary"),
) -> List[str]:
    """
    Return windows that have zero snapshot rows for epoch.

    Used by fetcher to decide which windows to materialize.

    Args:
        conn: database connection
        epoch: boundary epoch
        expected_windows: set of windows to check

    Returns:
        List of window names with zero rows (needs materialization)
    """
    incomplete = []
    for window in expected_windows:
        count = get_preboundary_epoch_snapshot_count(conn, epoch, window)
        if count == 0:
            incomplete.append(window)
    return incomplete
