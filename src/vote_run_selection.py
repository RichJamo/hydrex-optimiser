"""Pick which auto_vote_runs row to credit as the executed vote for an epoch.

A run is recorded "tx_sent" the moment its vote is broadcast, then updated to
"tx_success" or "tx_failed" once the receipt arrives (scripts/auto_voter.py). A phase
killed by the boundary monitor between broadcast and receipt leaves its run at "tx_sent"
forever, even though its vote may be the one that actually landed on-chain — a later
phase's vote replaces an earlier phase's. The post-mortem must resolve such a row itself
rather than ignore it, by checking the transaction's receipt directly.

Shared by scripts/preboundary_epoch_review.py and the inline Python in
scripts/shell/run_preboundary_analysis_pipeline.sh, which otherwise ran the same
selection query twice.
"""

import sqlite3
from typing import Callable, Optional, Tuple


def is_tx_confirmed_successful(
    w3, tx_hash: Optional[str], warn: Optional[Callable[[str], None]] = None
) -> Optional[bool]:
    """True/False once the on-chain outcome is known, None if it cannot be read."""
    if not tx_hash or w3 is None:
        return None
    try:
        receipt = w3.eth.get_transaction_receipt(tx_hash)
    except Exception as exc:
        if warn:
            warn(f"could not read receipt for {tx_hash}: {exc}")
        return None
    if not receipt:
        return None
    return int(receipt["status"]) == 1


def select_executed_run(
    conn: sqlite3.Connection,
    vote_epoch: int,
    epoch: int,
    w3=None,
    warn: Optional[Callable[[str], None]] = None,
) -> Optional[Tuple[int, int, Optional[str], Optional[float]]]:
    """The auto_vote_runs row whose vote should be credited for `epoch`, or None.

    Candidates are runs with status "tx_success" or "tx_sent" whose vote_sent_at falls in
    [vote_epoch, epoch); they are tried most-recent first, since a later phase's vote
    replaces an earlier phase's on-chain. A "tx_success" row is trusted as recorded (the
    voter already saw its receipt). A "tx_sent" row is credited only if its transaction is
    now confirmed successful on-chain; an unreadable or reverted receipt excludes it and
    the search continues at the next older candidate.

    Returns (run_id, vote_sent_at, tx_hash, expected_return_usd).
    """
    cur = conn.cursor()
    rows = cur.execute(
        """
        SELECT id, vote_sent_at, tx_hash, expected_return_usd, status
        FROM auto_vote_runs
        WHERE status IN ('tx_success', 'tx_sent')
          AND vote_sent_at IS NOT NULL
          AND vote_sent_at >= ?
          AND vote_sent_at < ?
        ORDER BY vote_sent_at DESC
        """,
        (int(vote_epoch), int(epoch)),
    ).fetchall()

    for run_id, vote_sent_at, tx_hash, expected_return_usd, status in rows:
        if status == "tx_success" or is_tx_confirmed_successful(w3, tx_hash, warn=warn):
            return int(run_id), int(vote_sent_at), tx_hash, expected_return_usd

    return None
