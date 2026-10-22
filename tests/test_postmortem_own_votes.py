"""Post-mortem and review paths must not count our own votes as other voters' competition.

A pool total read on-chain after we voted includes our votes. The live voter subtracts
them (tests/test_standing_votes.py); these are the three reporting sites that did not:

  - the T-1 prediction (preboundary_epoch_review): its snapshot is 60s before the
    boundary, after phase 1 (T-240) has voted, so it held phase 1's votes;
  - weekly_allocation_review: boundary votes_raw holds our binding vote, and every
    return then added our votes on top, counting them twice;
  - fetch_live_snapshot.print_allocation: the standalone recommendation table.

Contract under test:
  - load_standing_votes_before(conn, epoch, before_ts) returns the votes of the latest
    successful run sent in [vote_epoch, before_ts); dry runs and failed runs never count.
  - load_decision_timestamp raises unless the snapshot has exactly one timestamp.
  - subtract_our_votes removes our votes per gauge, floored at zero, so a return scored
    on the result is our true share.
  - print_allocation shows base votes with our standing votes removed.
"""

import io
import sqlite3

import pytest
from rich.console import Console

import data.fetchers.fetch_live_snapshot as fls
from scripts.preboundary_epoch_review import (
    load_decision_timestamp,
    load_executed_votes,
    load_standing_votes_before,
)
from scripts.weekly_allocation_review import (
    compute_portfolio_return,
    subtract_our_votes,
)
from src.optimizer import GaugeBoundaryState

EPOCH = 1_792_627_200
VOTE_EPOCH = 1_792_022_400
GAUGE_A = "0x" + "a" * 40
GAUGE_B = "0x" + "b" * 40


def _runs_db():
    """Epoch 1792627200's schedule: backup, phase 1 (T-204), phase 2 (T-37), phase 3 (T-13)."""
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE epoch_boundaries (epoch INTEGER, boundary_block INTEGER,
            boundary_timestamp INTEGER, vote_epoch INTEGER, reward_epoch INTEGER,
            source_tag TEXT, computed_at INTEGER);
        CREATE TABLE auto_vote_runs (id INTEGER, status TEXT, vote_sent_at INTEGER,
            tx_hash TEXT, expected_return_usd REAL);
        CREATE TABLE executed_allocations (epoch INTEGER, strategy_tag TEXT,
            gauge_address TEXT, executed_votes INTEGER);
        """
    )
    conn.execute(
        "INSERT INTO epoch_boundaries VALUES (?, 52918927, ?, ?, ?, 'test', 0)",
        (EPOCH, EPOCH + 1, VOTE_EPOCH, EPOCH),
    )
    runs = [
        (194, "tx_success", EPOCH - 12_554, {GAUGE_A: 50_000}),
        (195, "tx_success", EPOCH - 204, {GAUGE_A: 120_000}),
        (196, "tx_success", EPOCH - 37, {GAUGE_A: 130_000, GAUGE_B: 10_000}),
        (197, "tx_success", EPOCH - 13, {GAUGE_B: 140_000}),
        (198, "dry_run_success", EPOCH - 5, {GAUGE_A: 999_999}),
        (199, "tx_failed", EPOCH - 3, {GAUGE_A: 888_888}),
    ]
    for run_id, status, sent_at, votes in runs:
        conn.execute(
            "INSERT INTO auto_vote_runs VALUES (?, ?, ?, NULL, 0.0)",
            (run_id, status, sent_at),
        )
        for gauge, amount in votes.items():
            conn.execute(
                "INSERT INTO executed_allocations VALUES (?, ?, ?, ?)",
                (EPOCH, f"auto_voter_run_{run_id}", gauge, amount),
            )
    return conn


def test_standing_votes_at_the_t1_snapshot_are_phase_1s():
    # T-1 is 60s before the boundary: phase 1 (T-204) had voted, phase 2 (T-37) not yet.
    standing = load_standing_votes_before(_runs_db(), EPOCH, EPOCH - 60)
    assert standing == {GAUGE_A: 120_000}


def test_votes_standing_at_the_boundary_are_the_binding_run():
    conn = _runs_db()
    assert load_standing_votes_before(conn, EPOCH, EPOCH) == {GAUGE_B: 140_000}
    assert load_executed_votes(conn, EPOCH) == {GAUGE_B: 140_000}


def test_nothing_is_standing_before_our_first_vote():
    assert load_standing_votes_before(_runs_db(), EPOCH, VOTE_EPOCH + 60) == {}


def _snapshot_db(timestamps):
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE preboundary_snapshots (epoch INTEGER, decision_window TEXT,"
        " decision_timestamp INTEGER, gauge_address TEXT)"
    )
    for i, ts in enumerate(timestamps):
        conn.execute(
            "INSERT INTO preboundary_snapshots VALUES (?, 'T-1', ?, ?)",
            (EPOCH, ts, f"0x{i:040x}"),
        )
    return conn


def test_decision_timestamp_is_read_from_the_snapshot():
    assert load_decision_timestamp(_snapshot_db([EPOCH - 60] * 3), EPOCH, "T-1") == (
        EPOCH - 60
    )


@pytest.mark.parametrize("timestamps", [[], [EPOCH - 60, EPOCH - 61]])
def test_an_ambiguous_or_missing_snapshot_raises(timestamps):
    with pytest.raises(ValueError):
        load_decision_timestamp(_snapshot_db(timestamps), EPOCH, "T-1")


def test_weekly_review_scores_our_true_share():
    # Boundary total 300k includes our 200k: we hold 200k of 300k, so 2/3 of $1,000.
    states = [GaugeBoundaryState(GAUGE_A, GAUGE_A, 300_000.0, 1_000.0)]
    ours = {GAUGE_A: 200_000}

    double_counted = compute_portfolio_return({GAUGE_A: states[0]}, ours)
    corrected_states = subtract_our_votes(states, ours)
    corrected = compute_portfolio_return({GAUGE_A: corrected_states[0]}, ours)

    assert double_counted == pytest.approx(400.0)
    assert corrected == pytest.approx(1_000.0 * 2 / 3)


def test_weekly_review_subtraction_floors_at_zero_and_leaves_other_gauges():
    states = [
        GaugeBoundaryState(GAUGE_A, GAUGE_A, 50_000.0, 1.0),
        GaugeBoundaryState(GAUGE_B, GAUGE_B, 70_000.0, 1.0),
    ]
    corrected = subtract_our_votes(states, {GAUGE_A: 80_000})
    assert [s.votes_raw for s in corrected] == [0.0, 70_000.0]


def test_display_table_shows_base_votes_without_ours(monkeypatch):
    console = Console(file=io.StringIO(), width=400)
    monkeypatch.setattr(fls, "console", console)
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE live_gauge_snapshots (snapshot_ts INTEGER, gauge_address TEXT,"
        " pool_address TEXT, votes_raw REAL, rewards_normalized_total REAL,"
        " is_alive INTEGER)"
    )
    conn.execute(
        "INSERT INTO live_gauge_snapshots VALUES (1, ?, ?, 300000.0, 10.0, 1)",
        (GAUGE_A, GAUGE_A),
    )

    fls.print_allocation(
        conn,
        1,
        your_voting_power=100_000,
        top_k=1,
        standing_votes_by_pool={GAUGE_A: 200_000.0},
    )

    output = console.file.getvalue()
    assert "100,000.00" in output
    assert "300,000.00" not in output
