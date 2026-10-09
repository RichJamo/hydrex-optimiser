"""Replacing a stuck earlier vote (#11).

`build_and_send_vote_transaction` read its nonce with the confirmed count and sent at the
current gas price. If an earlier phase's vote was broadcast but not yet mined, the next
phase's vote reused its nonce at today's price and was rejected as an underpriced
replacement unless the price had risen ~10% or more, so the older (stuck) vote -- or
nothing -- landed instead of the newer one. Switching to the pending count is NOT the
fix: that queues the new vote behind the stuck one instead of replacing it (tried and
reverted in #9 after review).

The fix: before signing, compare the confirmed ("latest") and "pending" transaction
counts. If pending > confirmed, an earlier vote is stuck: reuse the confirmed nonce (not
the pending count) and bid at least 25% above the stuck transaction's own gas price
(read via `w3.eth.get_transaction` against the hash #12 now records at broadcast) and at
least 25% above today's price -- twice today's price if that lookup fails -- capped at
`max_gas_price_gwei`. With nothing stuck, both the nonce and the gas price must be
exactly what they are today; that case is pinned first, since it is the one that runs
every real week.

No existing suite touches `scripts/auto_voter.py` (grepped tests/ for "auto_voter" and
"build_and_send_vote_transaction" before writing this; see also
tests/test_vote_broadcast_record.py, which covers #12's broadcast-recording half of the
same fix), so nothing here duplicates existing coverage.
"""

from web3 import Web3

from scripts.auto_voter import build_and_send_vote_transaction
from tests.test_vote_broadcast_record import (
    POOL,
    TARGET,
    FakeFunctions,
    FakeVoteCall,
    FakeVoteContract,
    FakeWallet,
)


class FakeStuckEth:
    """A w3.eth whose confirmed/pending nonce and `get_transaction` are test-controlled."""

    def __init__(
        self,
        confirmed_nonce: int,
        pending_nonce: int,
        gas_price: int = 1_000_000_000,
        stuck_tx_gas_price=None,
        get_transaction_error: Exception = None,
    ):
        self.gas_price = gas_price
        self.chain_id = 8453
        self.block_number = 1_000
        self._confirmed_nonce = confirmed_nonce
        self._pending_nonce = pending_nonce
        self._stuck_tx_gas_price = stuck_tx_gas_price
        self._get_transaction_error = get_transaction_error
        self.sent_tx = None  # the tx dict actually signed/sent, for assertions

    def get_transaction_count(self, address, block_identifier="latest"):
        return (
            self._pending_nonce
            if block_identifier == "pending"
            else self._confirmed_nonce
        )

    def get_transaction(self, tx_hash):
        if self._get_transaction_error is not None:
            raise self._get_transaction_error
        return {"gasPrice": self._stuck_tx_gas_price}

    def estimate_gas(self, call):
        return 100_000

    def get_balance(self, address):
        return 10**18

    def send_raw_transaction(self, raw):
        return Web3.keccak(text="fake-tx")

    def wait_for_transaction_receipt(self, tx_hash, timeout=300):
        return {"status": 1, "blockNumber": 1, "gasUsed": 1}


class FakeStuckW3:
    def __init__(self, eth):
        self.eth = eth


class RecordingVoteCall(FakeVoteCall):
    """Records the tx dict build_transaction received, so the test can inspect it."""

    def __init__(self, sink: dict):
        self.sink = sink

    def build_transaction(self, tx):
        self.sink.update(tx)
        return dict(tx)


def _build_and_send(eth, stuck_tx_hash=None):
    sink: dict = {}

    class _Functions(FakeFunctions):
        def vote(self, pool_addresses, vote_proportions):
            return RecordingVoteCall(sink)

    class _Contract(FakeVoteContract):
        functions = _Functions()

    result = build_and_send_vote_transaction(
        w3=FakeStuckW3(eth),
        vote_contract=_Contract(),
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
        stuck_tx_hash=stuck_tx_hash,
    )
    return result, sink


def test_nothing_pending_nonce_and_gas_price_are_unchanged():
    """The property that matters most: the common case is byte-for-byte what it is today."""
    eth = FakeStuckEth(confirmed_nonce=7, pending_nonce=7, gas_price=2_000_000_000)
    (success, result, vote_sent_at, receipt_block, gas_used), sink = _build_and_send(
        eth
    )

    assert success is True
    assert sink["nonce"] == 7
    assert sink["gasPrice"] == 2_000_000_000  # today's gas price, untouched


def test_stuck_transaction_reuses_confirmed_nonce_and_bids_above_125pct():
    """A stuck earlier vote: same (confirmed) nonce, bid clears 125% of its own price."""
    eth = FakeStuckEth(
        confirmed_nonce=7,
        pending_nonce=8,
        gas_price=1_000_000_000,
        stuck_tx_gas_price=1_200_000_000,
    )
    (success, result, vote_sent_at, receipt_block, gas_used), sink = _build_and_send(
        eth, stuck_tx_hash="0x" + "11" * 32
    )

    assert success is True
    assert sink["nonce"] == 7  # the confirmed nonce, not the pending count (8)
    assert sink["gasPrice"] >= int(1_200_000_000 * 1.25)
    assert sink["gasPrice"] >= int(1_000_000_000 * 1.25)


def test_stuck_transaction_price_unreadable_bids_at_least_double_current():
    eth = FakeStuckEth(
        confirmed_nonce=7,
        pending_nonce=8,
        gas_price=1_000_000_000,
        get_transaction_error=LookupError("transaction not found"),
    )
    (success, result, vote_sent_at, receipt_block, gas_used), sink = _build_and_send(
        eth, stuck_tx_hash="0x" + "22" * 32
    )

    assert success is True
    assert sink["gasPrice"] >= 2_000_000_000


def test_stuck_transaction_no_hash_available_bids_at_least_double_current():
    """The caller has no earlier tx_sent run to look up (e.g. first vote of the epoch)."""
    eth = FakeStuckEth(confirmed_nonce=7, pending_nonce=8, gas_price=1_000_000_000)
    (success, result, vote_sent_at, receipt_block, gas_used), sink = _build_and_send(
        eth, stuck_tx_hash=None
    )

    assert success is True
    assert sink["gasPrice"] >= 2_000_000_000


def test_replacement_bid_never_exceeds_the_cap():
    eth = FakeStuckEth(
        confirmed_nonce=7,
        pending_nonce=8,
        gas_price=1_000_000_000,
        stuck_tx_gas_price=900_000_000_000,  # absurdly high stuck price
    )
    (success, result, vote_sent_at, receipt_block, gas_used), sink = _build_and_send(
        eth, stuck_tx_hash="0x" + "33" * 32
    )

    assert success is True
    cap_wei = int(1_000.0 * 1e9)  # max_gas_price_gwei=1_000.0 above
    assert sink["gasPrice"] == cap_wei
