"""Our own standing votes must not be counted as competition by the live optimizer.

Voter.weightsAt(pool, epoch) includes votes we already cast this epoch (a backup vote, or
an earlier boundary phase), and the live optimizer read it as the other voters' total. In
epoch 1792627200 phase 3 treated phase 2's 1.81M votes as competition; re-run with them
removed it was worth $1.62 more at the boundary, and far more where our share of a pool
is large. The post-mortem already subtracts our votes (preboundary_epoch_review.py).

Contract under test:
  - standing_votes_for_epoch keeps votes only when lastVoted falls in this vote epoch:
    Voter.votes(account, pool) still returns last epoch's votes after the flip, while
    weightsAt for the new epoch starts again from zero.
  - calculate_optimal_allocation subtracts standing votes per pool, floored at zero, so
    base_votes is the other voters' votes and the allocation follows them.
"""

import sqlite3

import pytest

from config.settings import WEEK
from scripts.auto_voter import calculate_optimal_allocation
from src.voting_power import standing_votes_for_epoch

VOTE_EPOCH = 1_792_022_400
POOL_A = "0x" + "a" * 40
POOL_B = "0x" + "b" * 40


@pytest.mark.parametrize(
    "last_voted", [VOTE_EPOCH, VOTE_EPOCH + 1_000, VOTE_EPOCH + WEEK - 1]
)
def test_votes_cast_this_epoch_are_standing(last_voted):
    checksummed_a = "0x" + "A" * 40
    standing = standing_votes_for_epoch(
        last_voted, {checksummed_a: 200_000.0, POOL_B: 0.0}, VOTE_EPOCH
    )
    assert standing == {POOL_A: 200_000.0}


@pytest.mark.parametrize(
    "last_voted",
    [
        # Measured 2026-10-22: lastVoted 1792627189 (the previous epoch's phase 3) while
        # votes(signer, pool) read 252,004 and the new epoch's weightsAt read 1,634.
        VOTE_EPOCH - 11,
        VOTE_EPOCH + WEEK,
        0,
    ],
)
def test_votes_from_another_epoch_are_not_standing(last_voted):
    assert standing_votes_for_epoch(last_voted, {POOL_A: 252_004.0}, VOTE_EPOCH) == {}


SNAPSHOT_TS = 1_792_626_967
TOKEN = "0x" + "c" * 40


def _snapshot_db(votes_by_pool):
    """Two gauges with $1,000 of rewards each, priced at the snapshot."""
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE live_gauge_snapshots (snapshot_ts INTEGER, gauge_address TEXT,
            pool_address TEXT, votes_raw REAL, rewards_normalized_total REAL, is_alive INTEGER);
        CREATE TABLE live_reward_token_samples (snapshot_ts INTEGER, gauge_address TEXT,
            reward_token TEXT, rewards_normalized REAL);
        CREATE TABLE historical_token_prices (token_address TEXT, timestamp INTEGER,
            usd_price REAL);
        """
    )
    conn.execute(
        "INSERT INTO historical_token_prices VALUES (?, ?, 1.0)", (TOKEN, SNAPSHOT_TS)
    )
    for pool, votes in votes_by_pool.items():
        gauge = pool.replace("0x", "0x9")[:42]
        conn.execute(
            "INSERT INTO live_gauge_snapshots VALUES (?, ?, ?, ?, 1000.0, 1)",
            (SNAPSHOT_TS, gauge, pool, votes),
        )
        conn.execute(
            "INSERT INTO live_reward_token_samples VALUES (?, ?, ?, 1000.0)",
            (SNAPSHOT_TS, gauge, TOKEN),
        )
    return conn


def _allocate(conn, standing):
    rows, _, _ = calculate_optimal_allocation(
        conn=conn,
        snapshot_ts=SNAPSHOT_TS,
        your_voting_power=100_000,
        top_k=2,
        candidate_pools=2,
        min_votes_per_pool=1_000,
        standing_votes_by_pool=standing,
    )
    return {pool: (votes, base) for _g, pool, votes, base, _r, _e in rows}


def test_allocation_sees_only_other_voters():
    # Pool A's 300k includes our 200k from an earlier phase: others hold 100k there,
    # against 200k on pool B. Counting our votes as competition pushes us onto B.
    conn = _snapshot_db({POOL_A: 300_000.0, POOL_B: 200_000.0})

    biased = _allocate(conn, {})
    corrected = _allocate(conn, {POOL_A: 200_000.0})

    assert biased[POOL_A][1] == 300_000.0
    assert biased[POOL_A][0] < biased[POOL_B][0]
    assert corrected[POOL_A][1] == 100_000.0
    assert corrected[POOL_B][1] == 200_000.0
    assert corrected[POOL_A][0] > corrected[POOL_B][0]


def test_standing_votes_above_the_pool_total_floor_at_zero():
    conn = _snapshot_db({POOL_A: 50_000.0, POOL_B: 200_000.0})
    corrected = _allocate(conn, {POOL_A: 80_000.0})
    assert corrected[POOL_A][1] == 0.0
