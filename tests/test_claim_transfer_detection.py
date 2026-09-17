"""A claim that transfers nothing must not be reported as a successful claim.

Found 2026-09-17 on epoch 1789603200. Two Voter self-claims
(0xac19d9de7115bcfa..., 0xb3668595708b0b4b...) burned 510,681 and 510,559 gas, returned
status=1, and emitted zero logs, because the entitlement belonged to the veNFT rather
than the signer. Every layer above reported success: "Broadcast ... status=success",
"Claim Batches: 2", "Phase 1-6 completed successfully". Only comparing wallet balances
to the operator table revealed that nothing had been claimed.

Contract under test:
  - count_reward_transfers counts ERC-20 Transfer logs crediting the recipient, and only
    those — not transfers out, not other events, not transfers to third parties;
  - assert_claims_moved_tokens raises when every broadcast claim succeeded and the total
    credited is zero, and stays silent otherwise.
"""

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "claim_and_swap_rewards.py"

RECIPIENT = "0xAB75E66C63307396FE8456Ea7c42CBBF3CF36298"
OTHER = "0x00000000000000000000000000000000000000c0"
TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
APPROVAL = "0x8c5be1e5ebec7d5bd14f71427d1e84f3dd0314c0f7b2291e5b200ac8c7c3b925"


@pytest.fixture(scope="module")
def mod():
    spec = importlib.util.spec_from_file_location("claim_and_swap_rewards", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _topic(address: str) -> str:
    return "0x" + "0" * 24 + address[2:].lower()


class _Receipt:
    def __init__(self, logs):
        self.logs = logs
        self.status = 1


def _log(topic0, to_address, from_address=OTHER):
    return {"topics": [topic0, _topic(from_address), _topic(to_address)]}


def test_zero_log_receipt_counts_no_transfers(mod):
    """The exact epoch-1789603200 receipt shape: status=1, logs=[]."""
    assert mod.count_reward_transfers(_Receipt([]), RECIPIENT) == 0


def test_counts_only_transfers_crediting_the_recipient(mod):
    receipt = _Receipt([
        _log(TRANSFER, RECIPIENT),
        _log(TRANSFER, RECIPIENT),
        _log(TRANSFER, OTHER),
        _log(APPROVAL, RECIPIENT),
        {"topics": [TRANSFER]},
    ])
    assert mod.count_reward_transfers(receipt, RECIPIENT) == 2


def test_transfer_out_of_the_recipient_does_not_count(mod):
    receipt = _Receipt([_log(TRANSFER, OTHER, from_address=RECIPIENT)])
    assert mod.count_reward_transfers(receipt, RECIPIENT) == 0


def test_recipient_matched_case_insensitively(mod):
    receipt = _Receipt([_log(TRANSFER, RECIPIENT.lower())])
    assert mod.count_reward_transfers(receipt, RECIPIENT.upper().replace("0X", "0x")) == 1


def test_missing_recipient_counts_nothing(mod):
    assert mod.count_reward_transfers(_Receipt([_log(TRANSFER, RECIPIENT)]), "") == 0


def test_successful_claims_that_moved_nothing_raise(mod):
    """The regression: two successes, zero transfers, must not pass."""
    results = [
        {"status": "success", "transfers_in": 0},
        {"status": "success", "transfers_in": 0},
    ]
    with pytest.raises(mod.ClaimMovedNothingError) as excinfo:
        mod.assert_claims_moved_tokens(results, RECIPIENT, "voter")
    message = str(excinfo.value)
    assert "--claim-source escrow" in message, "must name the likely fix"
    assert RECIPIENT in message


def test_any_transfer_clears_the_check(mod):
    results = [
        {"status": "success", "transfers_in": 0},
        {"status": "success", "transfers_in": 6},
    ]
    mod.assert_claims_moved_tokens(results, RECIPIENT, "escrow")


def test_dry_run_results_do_not_trip_the_check(mod):
    """A dry run broadcasts nothing, so it can never have moved tokens."""
    mod.assert_claims_moved_tokens(
        [{"status": "dry_run"}, {"status": "dry_run"}], RECIPIENT, "voter"
    )


def test_no_claims_at_all_does_not_trip_the_check(mod):
    mod.assert_claims_moved_tokens([], RECIPIENT, "voter")


def test_failed_claims_are_not_treated_as_silent_success(mod):
    """Errors and reverts are reported by their own paths, not by this check."""
    mod.assert_claims_moved_tokens(
        [{"status": "error", "error": "boom"}, {"status": "reverted"}],
        RECIPIENT,
        "voter",
    )


def test_summarize_counts_successes_and_transfers(mod):
    successes, transfers = mod.summarize_claim_transfers([
        {"status": "success", "transfers_in": 3},
        {"status": "success", "transfers_in": 0},
        {"status": "dry_run"},
        {"status": "error"},
    ])
    assert (successes, transfers) == (2, 3)


def test_transfer_topic_matches_the_erc20_signature(mod):
    """Guards the constant against a typo that would silently disable the check."""
    from eth_utils import keccak

    digest = keccak(text="Transfer(address,address,uint256)").hex()
    expected = "0x" + (digest[2:] if digest.startswith("0x") else digest)
    assert len(expected) == 66
    assert mod.ERC20_TRANSFER_TOPIC == expected
