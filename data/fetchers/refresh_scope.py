"""
Decide which gauges the phase-2/3 targeted bribe refresh re-queries.

The refresh re-reads every known (bribe, token) pair, and its cost grows with the
gauge universe (7.7s at 291 gauges, 11.1s at 414). Phase 3 has little slack before
the boundary, so gauges that have shown no bribes for a while are skipped there.
Phase 1 still reads every gauge, so a dormant gauge is only missed if it is bribed
between phase 1 (~T-240s) and the boundary.

A backtest over 19 epochs (4-epoch look-back, boundary data) skipped about 159 of
291 gauges and lost $34 of modelled return in one epoch, nothing in the others.

Contract for select_refresh_gauges():
  Preconditions:
    - live_gauge_snapshots holds the current snapshot (snapshot_ts).
  Postconditions:
    - Returns a subset of `gauges` (never adds addresses). A gauge is kept if any:
        1. rewards_normalized_total > 0 in the current snapshot;
        2. a non-zero live_reward_token_samples row in the previous N vote epochs;
        3. total_usd > 0 in boundary_gauge_values for those vote epochs;
        4. it is missing from `gauges`, has no created_at, or was added < N weeks ago.
  Invariants:
    - Fails open: if N <= 0, or any of the previous N vote epochs has no live
      snapshot, every gauge is kept.
    - A skipped gauge is $0 in the current snapshot (rule 1), so the refresh zeroing
      its rows does not change its value.
"""

import sqlite3
from dataclasses import dataclass
from typing import Iterable, Optional, Set

from config.settings import WEEK


@dataclass
class RefreshScope:
    gauges: Set[str]
    skipped: int
    reason: str  # "pruned" or why pruning was not applied


def select_refresh_gauges(
    conn: sqlite3.Connection,
    snapshot_ts: int,
    vote_epoch: int,
    gauges: Iterable[str],
    lookback_epochs: int,
    now_ts: int,
) -> RefreshScope:
    all_gauges = {str(g).lower() for g in gauges}
    n = int(lookback_epochs)
    if n <= 0:
        return RefreshScope(all_gauges, 0, "disabled")

    first_epoch = int(vote_epoch) - n * WEEK
    last_epoch = int(vote_epoch) - WEEK
    covered = {
        int(r[0])
        for r in conn.execute(
            "SELECT DISTINCT vote_epoch FROM live_gauge_snapshots WHERE vote_epoch BETWEEN ? AND ?",
            (first_epoch, last_epoch),
        )
    }
    missing = [e for e in range(first_epoch, last_epoch + 1, WEEK) if e not in covered]
    if missing:
        return RefreshScope(all_gauges, 0, f"no live snapshot for vote epochs {missing}")

    active: Set[str] = set()
    active |= _lower_set(conn.execute(
        "SELECT gauge_address FROM live_gauge_snapshots WHERE snapshot_ts = ? AND rewards_normalized_total > 0",
        (int(snapshot_ts),),
    ))
    active |= _lower_set(conn.execute(
        "SELECT DISTINCT gauge_address FROM live_reward_token_samples "
        "WHERE vote_epoch BETWEEN ? AND ? AND rewards_normalized > 0",
        (first_epoch, last_epoch),
    ))
    if _table_exists(conn, "boundary_gauge_values"):
        active |= _lower_set(conn.execute(
            "SELECT DISTINCT gauge_address FROM boundary_gauge_values "
            "WHERE vote_epoch BETWEEN ? AND ? AND total_usd > 0",
            (first_epoch, last_epoch),
        ))

    created = {
        str(addr).lower(): created_at
        for addr, created_at in conn.execute("SELECT address, created_at FROM gauges")
    }
    young_cutoff = int(now_ts) - n * WEEK
    for g in all_gauges:
        created_at: Optional[int] = created.get(g)
        if created_at is None or int(created_at) >= young_cutoff:
            active.add(g)

    kept = all_gauges & active
    return RefreshScope(kept, len(all_gauges) - len(kept), "pruned")


def _lower_set(rows) -> Set[str]:
    return {str(r[0]).lower() for r in rows if r and r[0]}


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
    ).fetchone() is not None
