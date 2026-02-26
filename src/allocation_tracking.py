"""
Allocation tracking helpers for weekly predicted/executed/realized analysis.
"""

import sqlite3
import time
from typing import Dict, Iterable, Optional, Tuple

from config.settings import WEEK


def ensure_allocation_tracking_tables(conn: sqlite3.Connection) -> None:
    cur = conn.cursor()

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS predicted_allocations (
            epoch INTEGER NOT NULL,
            vote_epoch INTEGER NOT NULL,
            snapshot_ts INTEGER,
            query_block INTEGER,
            strategy_tag TEXT NOT NULL,
            rank INTEGER NOT NULL,
            gauge_address TEXT NOT NULL,
            pool_address TEXT NOT NULL,
            predicted_votes INTEGER NOT NULL,
            created_at INTEGER NOT NULL,
            PRIMARY KEY (epoch, strategy_tag, gauge_address)
        )
        """
    )
    cur.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_predicted_allocations_epoch
        ON predicted_allocations(epoch, strategy_tag)
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS executed_allocations (
            epoch INTEGER NOT NULL,
            strategy_tag TEXT NOT NULL,
            rank INTEGER NOT NULL,
            gauge_address TEXT NOT NULL,
            pool_address TEXT NOT NULL,
            executed_votes INTEGER NOT NULL,
            source TEXT NOT NULL,
            tx_hash TEXT,
            recorded_at INTEGER NOT NULL,
            PRIMARY KEY (epoch, strategy_tag, gauge_address)
        )
        """
    )
    cur.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_executed_allocations_epoch
        ON executed_allocations(epoch, strategy_tag)
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS allocation_performance_metrics (
            epoch INTEGER NOT NULL,
            strategy_tag TEXT NOT NULL,
            metric_name TEXT NOT NULL,
            metric_value REAL NOT NULL,
            computed_at INTEGER NOT NULL,
            notes TEXT,
            PRIMARY KEY (epoch, strategy_tag, metric_name)
        )
        """
    )
    cur.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_allocation_performance_epoch
        ON allocation_performance_metrics(epoch, strategy_tag)
        """
    )

    conn.commit()


def save_predicted_allocation(
    conn: sqlite3.Connection,
    vote_epoch: int,
    snapshot_ts: int,
    query_block: int,
    strategy_tag: str,
    rows: Iterable[Tuple[int, str, str, int]],
) -> int:
    """
    Save predicted allocation rows.

    rows entries: (rank, gauge_address, pool_address, predicted_votes)
    Returns inserted row count.
    """
    ensure_allocation_tracking_tables(conn)
    cur = conn.cursor()
    now_ts = int(time.time())

    vote_epoch_i = int(vote_epoch)
    epoch_i = int(vote_epoch_i + WEEK)
    strategy = str(strategy_tag or "preboundary_equal").strip()

    cur.execute(
        "DELETE FROM predicted_allocations WHERE epoch = ? AND strategy_tag = ?",
        (epoch_i, strategy),
    )

    payload = []
    for rank, gauge_address, pool_address, predicted_votes in rows:
        payload.append(
            (
                epoch_i,
                vote_epoch_i,
                int(snapshot_ts),
                int(query_block),
                strategy,
                int(rank),
                str(gauge_address).lower(),
                str(pool_address).lower(),
                int(predicted_votes),
                now_ts,
            )
        )

    cur.executemany(
        """
        INSERT OR REPLACE INTO predicted_allocations(
            epoch, vote_epoch, snapshot_ts, query_block,
            strategy_tag, rank, gauge_address, pool_address,
            predicted_votes, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        payload,
    )
    conn.commit()
    return len(payload)


def save_executed_allocation(
    conn: sqlite3.Connection,
    epoch: int,
    strategy_tag: str,
    rows: Iterable[Tuple[int, str, str, int]],
    source: str = "manual",
    tx_hash: Optional[str] = None,
) -> int:
    """
    Save executed allocation rows.

    rows entries: (rank, gauge_address, pool_address, executed_votes)
    Returns inserted row count.
    """
    ensure_allocation_tracking_tables(conn)
    cur = conn.cursor()
    now_ts = int(time.time())

    epoch_i = int(epoch)
    strategy = str(strategy_tag or "manual").strip()

    cur.execute(
        "DELETE FROM executed_allocations WHERE epoch = ? AND strategy_tag = ?",
        (epoch_i, strategy),
    )

    payload = []
    for rank, gauge_address, pool_address, executed_votes in rows:
        payload.append(
            (
                epoch_i,
                strategy,
                int(rank),
                str(gauge_address).lower(),
                str(pool_address).lower(),
                int(executed_votes),
                str(source),
                tx_hash,
                now_ts,
            )
        )

    cur.executemany(
        """
        INSERT OR REPLACE INTO executed_allocations(
            epoch, strategy_tag, rank, gauge_address, pool_address,
            executed_votes, source, tx_hash, recorded_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        payload,
    )
    conn.commit()
    return len(payload)


def save_performance_metrics(
    conn: sqlite3.Connection,
    epoch: int,
    strategy_tag: str,
    metrics: Dict[str, float],
    notes: str = "",
) -> int:
    ensure_allocation_tracking_tables(conn)
    cur = conn.cursor()
    now_ts = int(time.time())

    epoch_i = int(epoch)
    strategy = str(strategy_tag or "manual").strip()

    payload = [
        (epoch_i, strategy, str(metric_name), float(metric_value), now_ts, notes)
        for metric_name, metric_value in metrics.items()
    ]

    cur.executemany(
        """
        INSERT OR REPLACE INTO allocation_performance_metrics(
            epoch, strategy_tag, metric_name, metric_value, computed_at, notes
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        payload,
    )
    conn.commit()
    return len(payload)
