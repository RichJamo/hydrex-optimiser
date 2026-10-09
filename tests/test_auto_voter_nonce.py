"""Which nonce the voter uses, given that the monitor can kill a phase mid-flight.

Since PR #9 the boundary monitor kills a phase that overruns its window. A phase killed
just after broadcasting leaves its vote pending. If the next phase read the "latest"
(confirmed-only) count it would reuse that nonce, and its vote would be rejected as an
underpriced replacement, losing the most up-to-date phase. Counting "pending"
transactions queues the new vote behind the old one instead.

Contract under test:
  - next_nonce asks the node for the "pending" transaction count;
  - build_and_send_vote_transaction takes its nonce from next_nonce.
"""

import importlib.util
import inspect
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def auto_voter():
    spec = importlib.util.spec_from_file_location(
        "auto_voter", ROOT / "scripts" / "auto_voter.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _RecordingEth:
    def __init__(self):
        self.calls = []

    def get_transaction_count(self, address, block_identifier="latest"):
        self.calls.append((address, block_identifier))
        return 5085 if block_identifier == "pending" else 5084


class _FakeW3:
    def __init__(self):
        self.eth = _RecordingEth()


def test_next_nonce_counts_pending_transactions(auto_voter):
    w3 = _FakeW3()
    signer = "0xAB75E66C63307396FE8456Ea7c42CBBF3CF36298"

    # One phase-2 vote is broadcast but unconfirmed: confirmed count 5084, pending 5085.
    assert auto_voter.next_nonce(w3, signer) == 5085
    assert w3.eth.calls == [(signer, "pending")]


def test_vote_transaction_takes_its_nonce_from_next_nonce(auto_voter):
    source = inspect.getsource(auto_voter.build_and_send_vote_transaction)

    assert "next_nonce(w3, from_address)" in source
    assert "get_transaction_count" not in source
