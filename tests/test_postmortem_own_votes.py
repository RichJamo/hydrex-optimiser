"""Post-mortem and review paths must not count our own votes as other voters' competition.

A pool total read on-chain after we voted includes our votes. The live voter subtracts
them (tests/test_standing_votes.py); these are the three reporting sites that did not:

  - the T-1 prediction (preboundary_epoch_review): its snapshot block has ranged from 8s
    to 120s before the boundary, so it can hold phase 1's, phase 2's or phase 3's votes;
  - weekly_allocation_review: boundary votes_raw holds our binding vote, and every
    return then added our votes on top, counting them twice;
  - fetch_live_snapshot.print_allocation: the standalone recommendation table.

Contract under test:
  - select_run_landed_by_block picks the latest run whose receipt succeeded at or before
    the block. Send time cannot decide it: in epoch 1790208000 the T-1 block was ~8s
    before the boundary, after phase 3 landed, while a send-time rule picked phase 1.
    Without receipts it raises rather than guess.
  - subtract_t1_standing_votes removes that run's votes from the T-1 states, and returns
    the states unchanged (with a warning) when the run cannot be decided.
  - load_states_without_our_votes scores the weekly review against other voters only.
  - print_allocation shows base votes with our standing votes removed.
"""

import io
import logging
import sqlite3

import pytest
from rich.console import Console
from web3.exceptions import TransactionNotFound

import data.fetchers.fetch_live_snapshot as fls
from scripts.preboundary_epoch_review import (
    load_executed_votes,
    subtract_t1_standing_votes,
)
from scripts.weekly_allocation_review import (
    compute_portfolio_return,
    load_states_without_our_votes,
)
from src.vote_run_selection import select_run_landed_by_block

EPOCH = 1_792_627_200
VOTE_EPOCH = 1_792_022_400
BOUNDARY_BLOCK = 52_918_927
GAUGE_A = "0x" + "a" * 40
GAUGE_B = "0x" + "b" * 40
GAUGE_C = "0x" + "c" * 40

# Epoch 1792627200's schedule: (run, status, sent at, landed in block, votes), plus
# two tx_sent rows a killed phase can leave behind: one reverted on-chain and one never
# mined. Neither ever stood, so neither may be chosen or block the choice.
RUNS = [
    (194, "tx_success", EPOCH - 12_554, 52_912_650, {GAUGE_A: 50_000}),
    (195, "tx_success", EPOCH - 204, 52_918_825, {GAUGE_A: 120_000}),
    (196, "tx_success", EPOCH - 37, 52_918_909, {GAUGE_B: 130_000}),
    (300, "tx_sent", EPOCH - 30, "reverted", {GAUGE_A: 777_777}),
    (301, "tx_sent", EPOCH - 25, "dropped", {GAUGE_A: 666_666}),
    (197, "tx_success", EPOCH - 13, 52_918_921, {GAUGE_C: 140_000}),
    (198, "dry_run_success", EPOCH - 5, None, {GAUGE_A: 999_999}),
    (199, "tx_failed", EPOCH - 3, None, {GAUGE_A: 888_888}),
]


class FakeReceipts:
    """w3 stand-in answering get_transaction_receipt from RUNS."""

    def __init__(self, unreadable=(), not_found=()):
        self.eth = self
        self.unreadable = set(unreadable)
        self.not_found = set(not_found)
        self.landed = {_tx(r): block for r, _s, _t, block, _v in RUNS}

    def get_transaction_receipt(self, tx_hash):
        if tx_hash in self.unreadable:
            raise ConnectionError(
                "Max retries exceeded with url: https://base-mainnet.g.alchemy.com/v2/SECRETKEY123"
            )
        block = "dropped" if tx_hash in self.not_found else self.landed[tx_hash]
        if block == "dropped" or block is None:
            raise TransactionNotFound(f"Transaction with hash {tx_hash} not found")
        if block == "reverted":
            return {"status": 0, "blockNumber": 52_918_912}
        return {"status": 1, "blockNumber": block}


def _tx(run_id):
    return f"0x{run_id:064x}"


def _db():
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
        CREATE TABLE preboundary_snapshots (epoch INTEGER, decision_window TEXT,
            decision_block INTEGER, gauge_address TEXT);
        CREATE TABLE boundary_gauge_values (epoch INTEGER, gauge_address TEXT,
            pool_address TEXT, votes_raw REAL, total_usd REAL, active_only INTEGER);
        """
    )
    conn.execute(
        "INSERT INTO epoch_boundaries VALUES (?, ?, ?, ?, ?, 'test', 0)",
        (EPOCH, BOUNDARY_BLOCK, EPOCH + 1, VOTE_EPOCH, EPOCH),
    )
    for run_id, status, sent_at, _block, votes in RUNS:
        conn.execute(
            "INSERT INTO auto_vote_runs VALUES (?, ?, ?, ?, 0.0)",
            (run_id, status, sent_at, _tx(run_id)),
        )
        for gauge, amount in votes.items():
            conn.execute(
                "INSERT INTO executed_allocations VALUES (?, ?, ?, ?)",
                (EPOCH, f"auto_voter_run_{run_id}", gauge, amount),
            )
    return conn


@pytest.mark.parametrize(
    "snapshot_block, standing_run",
    [
        (52_918_903, 195),  # this epoch's T-1: ~48s out, between phases 1 and 2
        (52_918_909, 196),  # the block phase 2 landed in counts as standing
        (52_918_915, 196),  # after run 300 reverted and run 301 was dropped
        (52_918_923, 197),  # ~8s out, like epoch 1790208000: after phase 3 landed
        (52_912_649, None),  # before our first vote of the epoch
    ],
)
def test_the_standing_run_is_decided_by_receipt_block(snapshot_block, standing_run):
    conn = _db()
    assert (
        select_run_landed_by_block(
            conn, VOTE_EPOCH, EPOCH, snapshot_block, FakeReceipts()
        )
        == standing_run
    )


def test_without_receipts_the_standing_run_is_not_guessed():
    with pytest.raises(ValueError):
        select_run_landed_by_block(_db(), VOTE_EPOCH, EPOCH, 52_918_903, None)
    with pytest.raises(ValueError) as unreadable:
        select_run_landed_by_block(
            _db(),
            VOTE_EPOCH,
            EPOCH,
            52_918_903,
            FakeReceipts(unreadable={_tx(197)}),
        )
    # The RPC URL carries the API key and this text reaches the review's log file.
    assert "SECRETKEY123" not in str(unreadable.value)
    assert unreadable.value.__cause__ is None
    assert unreadable.value.__suppress_context__ is True


def test_a_confirmed_vote_the_rpc_cannot_find_is_not_skipped():
    # Run 197's receipt was seen when it ran. A null receipt now means the RPC cannot see
    # our transactions (wrong network, pruned index), not that the vote never stood, so
    # falling through to run 196 would subtract the wrong vote without a warning.
    with pytest.raises(ValueError, match="run 197"):
        select_run_landed_by_block(
            _db(),
            VOTE_EPOCH,
            EPOCH,
            52_918_923,
            FakeReceipts(not_found={_tx(197)}),
        )


T1_STATES = [
    (GAUGE_A, GAUGE_A, 300_000.0, 10.0),
    (GAUGE_B, GAUGE_B, 400_000.0, 10.0),
]


def _with_t1_snapshot(conn, blocks):
    for i, block in enumerate(blocks):
        conn.execute(
            "INSERT INTO preboundary_snapshots VALUES (?, 'T-1', ?, ?)",
            (EPOCH, block, f"0x{i:040x}"),
        )
    return conn


@pytest.mark.parametrize(
    "snapshot_block, expected_votes",
    [
        (52_918_903, [180_000.0, 400_000.0]),  # phase 1's 120k on A removed
        (52_918_915, [300_000.0, 270_000.0]),  # phase 2's 130k on B removed
    ],
)
def test_t1_states_lose_the_vote_standing_at_their_block(
    snapshot_block, expected_votes
):
    conn = _with_t1_snapshot(_db(), [snapshot_block] * 2)
    corrected = subtract_t1_standing_votes(
        conn, conn, EPOCH, "T-1", T1_STATES, FakeReceipts(), logging.getLogger("t")
    )
    assert [votes for _g, _p, votes, _r in corrected] == expected_votes


@pytest.mark.parametrize(
    "blocks, w3",
    [
        ([52_918_903, 52_918_904], FakeReceipts()),  # ambiguous snapshot block
        ([52_918_903], None),  # no RPC to read receipts
    ],
)
def test_undecidable_t1_states_are_left_unchanged_with_a_warning(blocks, w3, caplog):
    conn = _with_t1_snapshot(_db(), blocks)
    with caplog.at_level(logging.WARNING):
        corrected = subtract_t1_standing_votes(
            conn, conn, EPOCH, "T-1", T1_STATES, w3, logging.getLogger("t")
        )
    assert corrected == T1_STATES
    assert "own votes not subtracted" in caplog.text


def test_executed_votes_are_still_the_binding_run():
    assert load_executed_votes(_db(), EPOCH) == {GAUGE_C: 140_000}


def test_weekly_review_scores_our_true_share():
    # The boundary total of 300k on C includes our binding 140k from run 197: we hold
    # 140k of 300k, so 140/300 of $1,000, not 140/440.
    conn = _db()
    conn.execute(
        "INSERT INTO boundary_gauge_values VALUES (?, ?, ?, 300000.0, 1000.0, 1)",
        (EPOCH, GAUGE_C, GAUGE_C),
    )
    states = load_states_without_our_votes(conn, EPOCH, None)

    assert [s.votes_raw for s in states] == [160_000.0]
    returned = compute_portfolio_return({GAUGE_C: states[0]}, {GAUGE_C: 140_000})
    assert returned == pytest.approx(1_000.0 * 140 / 300)


def test_display_table_shows_base_votes_without_ours(monkeypatch):
    console = Console(file=io.StringIO(), width=400)
    monkeypatch.setattr(fls, "console", console)
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE live_gauge_snapshots (snapshot_ts INTEGER, gauge_address TEXT,"
        " pool_address TEXT, votes_raw REAL, rewards_normalized_total REAL,"
        " is_alive INTEGER)"
    )
    # Pool and gauge differ, so a lookup keyed by gauge would miss.
    conn.execute(
        "INSERT INTO live_gauge_snapshots VALUES (1, ?, ?, 300000.0, 10.0, 1)",
        (GAUGE_B, GAUGE_A),
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
