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
  - fetch_standing_votes sends lastVoted + votes(account, pool) for every pool in one
    Multicall3 aggregate at the given block, scales by 1e18, keys pools lowercase, and
    raises on a failed or malformed read.
  - read_standing_votes_or_uncorrected reads with the voting account and the epoch and
    block stored on the snapshot rows, and on any failure returns {} (uncorrected
    totals) without raising and without printing the RPC URL.
"""

import io
import sqlite3

import pytest
from rich.console import Console
from web3 import Web3

import scripts.auto_voter as av
from config.settings import WEEK
from scripts.auto_voter import (
    calculate_optimal_allocation,
    read_standing_votes_or_uncorrected,
)
from src.voting_power import (
    MULTICALL3_ADDRESS,
    StandingVotes,
    fetch_standing_votes,
    standing_votes_for_epoch,
)

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
SNAPSHOT_BLOCK = 52_918_909
TOKEN = "0x" + "c" * 40


def _snapshot_db(votes_by_pool):
    """Two gauges with $1,000 of rewards each, priced at the snapshot."""
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE live_gauge_snapshots (snapshot_ts INTEGER, gauge_address TEXT,
            pool_address TEXT, votes_raw REAL, rewards_normalized_total REAL,
            is_alive INTEGER, vote_epoch INTEGER, query_block INTEGER);
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
            "INSERT INTO live_gauge_snapshots VALUES (?, ?, ?, ?, 1000.0, 1, ?, ?)",
            (SNAPSHOT_TS, gauge, pool, votes, VOTE_EPOCH, SNAPSHOT_BLOCK),
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


VOTER = "0xc69E3eF39E3fFBcE2A1c570f8d3ADF76909ef17b"
SIGNER = "0x1111111111111111111111111111111111111111"


def _word(value):
    return int(value).to_bytes(32, "big")


def _addr_word(address):
    return bytes.fromhex(address[2:].lower().rjust(64, "0"))


class FakeMulticallW3:
    """Answers Multicall3.aggregate; records the calls and block it was asked for."""

    def __init__(self, return_data=None, error=None):
        self.return_data = return_data
        self.error = error
        self.calls = None
        self.block = None
        self.contract_address = None
        self.eth = self

    def contract(self, address, abi):
        self.contract_address = address
        return self

    @property
    def functions(self):
        return self

    def aggregate(self, calls):
        self.calls = calls
        return self

    def call(self, block_identifier):
        self.block = block_identifier
        if self.error:
            raise self.error
        return block_identifier, self.return_data


def test_fetch_reads_every_pool_in_one_aggregate_at_the_block():
    w3 = FakeMulticallW3(
        return_data=[
            _word(VOTE_EPOCH + 100),
            _word(200_000 * 10**18),
            _word(0),
        ]
    )
    pools = [POOL_B, "0x" + "A" * 40, POOL_B]

    result = fetch_standing_votes(w3, VOTER, SIGNER, VOTE_EPOCH, pools, SNAPSHOT_BLOCK)

    assert w3.contract_address == MULTICALL3_ADDRESS
    assert w3.block == SNAPSHOT_BLOCK
    last_voted_sel = bytes(Web3.keccak(text="lastVoted(address)")[:4])
    votes_sel = bytes(Web3.keccak(text="votes(address,address)")[:4])
    assert [(target, bytes(data)) for target, data in w3.calls] == [
        (VOTER, last_voted_sel + _addr_word(SIGNER)),
        (VOTER, votes_sel + _addr_word(SIGNER) + _addr_word(POOL_A)),
        (VOTER, votes_sel + _addr_word(SIGNER) + _addr_word(POOL_B)),
    ]
    assert result == StandingVotes(VOTE_EPOCH + 100, {POOL_A: 200_000.0})


def test_fetch_raises_on_a_failed_read():
    w3 = FakeMulticallW3(error=RuntimeError("execution reverted"))
    with pytest.raises(RuntimeError):
        fetch_standing_votes(w3, VOTER, SIGNER, VOTE_EPOCH, [POOL_A], SNAPSHOT_BLOCK)


def test_fetch_raises_on_a_short_result():
    w3 = FakeMulticallW3(return_data=[_word(VOTE_EPOCH)])
    with pytest.raises(ValueError):
        fetch_standing_votes(w3, VOTER, SIGNER, VOTE_EPOCH, [POOL_A], SNAPSHOT_BLOCK)


@pytest.fixture
def recorded_console(monkeypatch):
    console = Console(file=io.StringIO(), width=400)
    monkeypatch.setattr(av, "console", console)
    monkeypatch.setattr(av, "VOTE_FROM", "signer")
    return console


def test_read_uses_the_snapshot_epoch_block_and_voting_account(recorded_console):
    conn = _snapshot_db({POOL_A: 300_000.0, POOL_B: 200_000.0})
    seen = {}

    def fake_fetch(w3, voter, account, epoch, pools, block):
        seen.update(voter=voter, account=account, epoch=epoch, block=block)
        seen["pools"] = sorted(pools)
        return StandingVotes(VOTE_EPOCH + 100, {POOL_A: 200_000.0})

    standing = read_standing_votes_or_uncorrected(
        conn, object(), SNAPSHOT_TS, SIGNER, fetch=fake_fetch
    )

    assert standing == {POOL_A: 200_000.0}
    # The stored epoch, not main()'s: --skip-fresh-fetch can advance vote_epoch past a
    # stale snapshot while votes_raw still holds the old epoch's weights.
    assert seen == {
        "voter": av.VOTER_ADDRESS,
        "account": SIGNER,
        "epoch": VOTE_EPOCH,
        "block": SNAPSHOT_BLOCK,
        "pools": [POOL_A, POOL_B],
    }
    assert "200,000 votes across 1 pools" in recorded_console.file.getvalue()


def test_a_failed_read_falls_back_to_uncorrected_totals(recorded_console):
    conn = _snapshot_db({POOL_A: 300_000.0})

    def failing_fetch(*_args):
        raise ConnectionError(
            "Max retries exceeded with url: https://base-mainnet.g.alchemy.com/v2/SECRETKEY123"
        )

    standing = read_standing_votes_or_uncorrected(
        conn, object(), SNAPSHOT_TS, SIGNER, fetch=failing_fetch
    )

    assert standing == {}
    output = recorded_console.file.getvalue()
    assert "uncorrected pool totals" in output
    assert "SECRETKEY123" not in output


def test_a_snapshot_read_at_two_blocks_falls_back(recorded_console):
    conn = _snapshot_db({POOL_A: 300_000.0, POOL_B: 200_000.0})
    conn.execute(
        "UPDATE live_gauge_snapshots SET query_block = ? WHERE pool_address = ?",
        (SNAPSHOT_BLOCK + 1, POOL_B),
    )

    # The function swallows every exception, so a raising fake could not prove the read
    # was skipped; record calls instead.
    reads = []

    def recording_fetch(*args):
        reads.append(args)
        return StandingVotes(VOTE_EPOCH + 100, {POOL_A: 200_000.0})

    standing = read_standing_votes_or_uncorrected(
        conn, object(), SNAPSHOT_TS, SIGNER, fetch=recording_fetch
    )

    assert standing == {}
    assert reads == []
    output = recorded_console.file.getvalue()
    assert "uncorrected pool totals" in output
    assert "2 (vote_epoch, query_block) pairs" in output
