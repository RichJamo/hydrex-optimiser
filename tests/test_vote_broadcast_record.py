"""Recording a vote the moment it is broadcast, and crediting it in the post-mortem (#12).

Found while fixing phase timeouts (#9): the monitor SIGKILLs a phase that overruns its
time limit. `build_and_send_vote_transaction` broadcasts the vote, then waits up to 300s
for the receipt, and only after the receipt arrives does `auto_voter.py:main()` write the
run's outcome to `auto_vote_runs` and its allocation to `executed_allocations`. A phase
killed during that wait leaves its vote on-chain but the run stuck at "started" with no
allocation recorded, so the post-mortem (`load_executed_votes` /
`run_preboundary_analysis_pipeline.sh`, both selecting `WHERE status = 'tx_success'`)
silently credits the wrong allocation to the epoch.

The fix is an `on_broadcast` callback into `build_and_send_vote_transaction`, fired right
after `send_raw_transaction` and before the receipt wait, that records the run as
`tx_sent` with its hash and allocation. The post-mortem then has to treat a `tx_sent` row
as possibly-executed: `src/vote_run_selection.py:select_executed_run` confirms it on-chain
before crediting it, since a later phase's `tx_sent` vote may be the one that actually
landed, replacing an earlier phase's confirmed `tx_success` vote.

No existing suite touches `scripts/auto_voter.py` or `scripts/preboundary_epoch_review.py`
at all (grepped for "auto_voter" and "build_and_send_vote_transaction" across tests/ before
writing this), so nothing here duplicates existing coverage.
"""

import sqlite3
from pathlib import Path

import pytest
from hexbytes import HexBytes
from web3 import Web3

from scripts.auto_voter import (
    build_and_send_vote_transaction,
    create_auto_vote_run,
    ensure_auto_vote_runs_table,
    persist_executed_allocation_for_run,
    update_auto_vote_run,
)
from src.db import apply_schema
from src.vote_run_selection import select_executed_run

POOL = Web3.to_checksum_address("0x1111111111111111111111111111111111111111")
TARGET = Web3.to_checksum_address("0x2222222222222222222222222222222222222222")
WALLET_ADDRESS = Web3.to_checksum_address("0x3333333333333333333333333333333333333333")

# (gauge_address, pool_address, votes, current_votes, current_rewards, expected_to_us)
ALLOCATION = [
    (
        "0x4444444444444444444444444444444444444444",
        "0x5555555555555555555555555555555555555555",
        1_000_000,
        500_000.0,
        50.0,
        25.0,
    )
]


class FakeSignedTx:
    rawTransaction = b"\x01\x02\x03"


class FakeWallet:
    address = WALLET_ADDRESS

    def sign_transaction(self, tx):
        return FakeSignedTx()


class FakeVoteCall:
    def call(self, *args, **kwargs):
        return None  # no revert

    def build_transaction(self, tx):
        return dict(tx)


class FakeFunctions:
    def vote(self, pool_addresses, vote_proportions):
        return FakeVoteCall()


class FakeVoteContract:
    functions = FakeFunctions()


class FakeSendEth:
    """Answers everything build_and_send_vote_transaction needs up to and including send."""

    def __init__(self, tx_hash_hex: str, receipt=None, receipt_error=None):
        self.gas_price = 1_000_000_000
        self.chain_id = 8453
        self.block_number = 1_000
        self._tx_hash = HexBytes(tx_hash_hex)
        self._receipt = receipt
        self._receipt_error = receipt_error

    def get_transaction_count(self, address):
        return 7

    def estimate_gas(self, call):
        return 100_000

    def get_balance(self, address):
        return 10**18

    def send_raw_transaction(self, raw):
        return self._tx_hash

    def wait_for_transaction_receipt(self, tx_hash, timeout=300):
        if self._receipt_error is not None:
            raise self._receipt_error
        return self._receipt


class FakeW3:
    def __init__(self, eth):
        self.eth = eth


def _db(tmp_path: Path) -> sqlite3.Connection:
    db_path = tmp_path / "test.db"
    apply_schema(str(db_path))
    conn = sqlite3.connect(str(db_path))
    ensure_auto_vote_runs_table(conn)
    return conn


def _send_vote(w3, conn, run_id, vote_epoch):
    """Call the real function the way auto_voter.py:main() does, with a real on_broadcast."""

    def on_broadcast(tx_hash_hex, sent_at):
        update_auto_vote_run(
            conn, run_id, status="tx_sent", tx_hash=tx_hash_hex, vote_sent_at=sent_at
        )
        persist_executed_allocation_for_run(
            conn=conn,
            run_id=run_id,
            vote_epoch=vote_epoch,
            allocation=ALLOCATION,
            tx_hash=tx_hash_hex,
            source=f"auto_voter:run_id={run_id}",
        )

    return build_and_send_vote_transaction(
        w3=w3,
        vote_contract=FakeVoteContract(),
        wallet=FakeWallet(),
        pool_addresses=[POOL],
        vote_proportions=[1_000_000],
        max_gas_price_gwei=1_000.0,
        vote_target_address=TARGET,
        gas_limit=500_000,
        gas_buffer_multiplier=1.2,
        dry_run=False,
        simulate_from_address="",
        vote_epoch=0,  # skips the boundary guard; unrelated to this fix
        on_broadcast=on_broadcast,
    )


def test_kill_between_broadcast_and_receipt_leaves_tx_sent_with_hash_and_allocation(
    tmp_path,
):
    """#12's core scenario: the real SIGKILL never lets this function return at all, so

    what matters is what is already on disk by the time the receipt wait would run. A
    raising fake `wait_for_transaction_receipt` stands in for that: everything after it
    never executes, same as a real kill.
    """
    conn = _db(tmp_path)
    run_id = create_auto_vote_run(conn, initiated_at=1_700_000_000, dry_run=False)
    tx_hash_hex = "0x" + "ab" * 32

    w3 = FakeW3(
        FakeSendEth(tx_hash_hex, receipt_error=TimeoutError("receipt timed out"))
    )
    success, result, vote_sent_at, receipt_block, gas_used = _send_vote(
        w3, conn, run_id, vote_epoch=1_700_000_000
    )

    assert success is False
    assert vote_sent_at is not None
    assert receipt_block is None  # distinguishes "unknown" from a confirmed revert

    row = conn.execute(
        "SELECT status, tx_hash, vote_sent_at FROM auto_vote_runs WHERE id = ?",
        (run_id,),
    ).fetchone()
    assert row == ("tx_sent", tx_hash_hex, vote_sent_at)

    exec_rows = conn.execute(
        "SELECT gauge_address, pool_address, executed_votes, tx_hash "
        "FROM executed_allocations WHERE strategy_tag = ?",
        (f"auto_voter_run_{run_id}",),
    ).fetchall()
    assert exec_rows == [
        (
            ALLOCATION[0][0].lower(),
            ALLOCATION[0][1].lower(),
            ALLOCATION[0][2],
            tx_hash_hex,
        )
    ]


def test_confirmed_receipt_still_reaches_the_normal_return(tmp_path):
    """A normal, un-killed send still records tx_sent at broadcast and returns success."""
    conn = _db(tmp_path)
    run_id = create_auto_vote_run(conn, initiated_at=1_700_000_000, dry_run=False)
    tx_hash_hex = "0x" + "cd" * 32

    w3 = FakeW3(
        FakeSendEth(
            tx_hash_hex, receipt={"status": 1, "blockNumber": 42, "gasUsed": 99_999}
        )
    )
    success, result, vote_sent_at, receipt_block, gas_used = _send_vote(
        w3, conn, run_id, vote_epoch=1_700_000_000
    )

    assert (success, result, receipt_block, gas_used) == (True, tx_hash_hex, 42, 99_999)

    row = conn.execute(
        "SELECT status, tx_hash FROM auto_vote_runs WHERE id = ?", (run_id,)
    ).fetchone()
    # on_broadcast already wrote tx_sent; main() is what finalizes it to tx_success.
    assert row == ("tx_sent", tx_hash_hex)


def test_selection_prefers_confirmed_tx_sent_over_earlier_tx_success_and_ignores_unconfirmed(
    tmp_path,
):
    """The post-mortem side of #12.

    Three candidate runs in one epoch window, oldest to newest: a completed tx_success,
    a tx_sent left by a killed phase whose transaction is now confirmed successful
    on-chain, and a tx_sent whose receipt cannot be read at all (still pending, or the
    RPC call failed). The most recent vote replaces the earlier ones once confirmed, so
    the confirmed tx_sent run must win over the earlier tx_success run; the unreadable
    one must be excluded, with a warning, rather than silently accepted or silently
    breaking the search.
    """
    conn = _db(tmp_path)
    vote_epoch = 1_700_000_000
    epoch = vote_epoch + 604_800

    tx_success_hash = "0x" + "11" * 32
    confirmed_tx_sent_hash = "0x" + "22" * 32
    unconfirmed_tx_sent_hash = "0x" + "33" * 32

    run_old = create_auto_vote_run(conn, initiated_at=vote_epoch, dry_run=False)
    update_auto_vote_run(
        conn,
        run_old,
        status="tx_success",
        vote_sent_at=vote_epoch + 10,
        tx_hash=tx_success_hash,
        expected_return_usd=10.0,
    )

    run_confirmed = create_auto_vote_run(conn, initiated_at=vote_epoch, dry_run=False)
    update_auto_vote_run(
        conn,
        run_confirmed,
        status="tx_sent",
        vote_sent_at=vote_epoch + 35,
        tx_hash=confirmed_tx_sent_hash,
        expected_return_usd=20.0,
    )

    run_unconfirmed = create_auto_vote_run(conn, initiated_at=vote_epoch, dry_run=False)
    update_auto_vote_run(
        conn,
        run_unconfirmed,
        status="tx_sent",
        vote_sent_at=vote_epoch + 60,
        tx_hash=unconfirmed_tx_sent_hash,
        expected_return_usd=30.0,
    )

    class FakeReceiptEth:
        def get_transaction_receipt(self, tx_hash):
            if tx_hash == confirmed_tx_sent_hash:
                return {"status": 1}
            raise LookupError(f"transaction not found: {tx_hash}")

    w3 = FakeW3(FakeReceiptEth())
    warnings = []
    selected = select_executed_run(conn, vote_epoch, epoch, w3=w3, warn=warnings.append)

    assert selected == (
        run_confirmed,
        vote_epoch + 35,
        confirmed_tx_sent_hash,
        20.0,
    )
    assert warnings and unconfirmed_tx_sent_hash in warnings[0]


def test_selection_ignores_a_run_outside_the_epoch_window(tmp_path):
    conn = _db(tmp_path)
    vote_epoch = 1_700_000_000
    epoch = vote_epoch + 604_800

    run_id = create_auto_vote_run(conn, initiated_at=vote_epoch, dry_run=False)
    update_auto_vote_run(
        conn,
        run_id,
        status="tx_success",
        vote_sent_at=epoch + 1,  # after the window
        tx_hash="0x" + "44" * 32,
    )

    assert select_executed_run(conn, vote_epoch, epoch, w3=None) is None
