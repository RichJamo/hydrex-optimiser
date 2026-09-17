"""Which contract Phase 3 claims through, and why it is not VOTE_FROM.

Found 2026-09-17 on epoch 1789603200. The signer voted by delegation, so the runbook and
the script both assumed rewards accrued to the signer and defaulted to
`--claim-source voter`. They do not. Bribe contracts book an epoch's entitlement against
the account that *held* the votes — for a delegated veNFT, still its owner. Measured at
the time: `earned(19435, wtFGI)` = 12.356981 (the full entitlement) while
`earnedOwner(signer, wtFGI, ...)` = 0. The resulting Voter self-claims burned ~510k gas
each, emitted zero logs, returned status=1, and claimed nothing.

Contract under test (choose_claim_source):
  - an explicit choice is always returned unchanged;
  - "voter" is chosen only when the signer owns the veNFT or is approved for it;
  - an escrow-owned veNFT resolves to "escrow" regardless of VOTE_FROM;
  - unreadable ownership degrades to the configured escrow rather than guessing "voter".
"""

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "claim_and_swap_rewards.py"

SIGNER = "0xAB75E66C63307396FE8456Ea7c42CBBF3CF36298"
ESCROW = "0x768a675B8542F23C428C6672738E380176E7635C"
STRANGER = "0x00000000000000000000000000000000000000c0"


@pytest.fixture(scope="module")
def mod():
    spec = importlib.util.spec_from_file_location("claim_and_swap_rewards", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("explicit", ["escrow", "voter", "distributor"])
def test_explicit_choice_is_never_overridden(mod, explicit):
    """The operator override must survive any ownership state, including a hostile one."""
    source, reason = mod.choose_claim_source(
        explicit=explicit,
        signer_address=SIGNER,
        escrow_address=ESCROW,
        venft_owner=STRANGER,
        signer_is_approved_or_owner=False,
    )
    assert source == explicit
    assert "explicit" in reason


def test_escrow_owned_venft_resolves_to_escrow(mod):
    """The epoch-1789603200 case: this is the resolution that had to change."""
    source, reason = mod.choose_claim_source(
        explicit=None,
        signer_address=SIGNER,
        escrow_address=ESCROW,
        venft_owner=ESCROW,
        signer_is_approved_or_owner=False,
    )
    assert source == "escrow"
    assert ESCROW in reason


def test_escrow_ownership_matched_case_insensitively(mod):
    """ownerOf returns checksummed addresses; config may not be."""
    source, _ = mod.choose_claim_source(
        explicit=None,
        signer_address=SIGNER,
        escrow_address=ESCROW.lower(),
        venft_owner=ESCROW.upper().replace("0X", "0x"),
        signer_is_approved_or_owner=False,
    )
    assert source == "escrow"


def test_signer_owned_venft_resolves_to_voter(mod):
    source, _ = mod.choose_claim_source(
        explicit=None,
        signer_address=SIGNER,
        escrow_address=ESCROW,
        venft_owner=SIGNER,
        signer_is_approved_or_owner=True,
    )
    assert source == "voter"


def test_approved_signer_may_self_claim_a_third_party_venft(mod):
    source, _ = mod.choose_claim_source(
        explicit=None,
        signer_address=SIGNER,
        escrow_address="",
        venft_owner=STRANGER,
        signer_is_approved_or_owner=True,
    )
    assert source == "voter"


def test_unapproved_signer_never_resolves_to_voter_when_escrow_exists(mod):
    """The invariant that makes the silent no-op impossible to reach by default."""
    source, _ = mod.choose_claim_source(
        explicit=None,
        signer_address=SIGNER,
        escrow_address=ESCROW,
        venft_owner=STRANGER,
        signer_is_approved_or_owner=False,
    )
    assert source == "escrow"


def test_unreadable_ownership_falls_back_to_configured_escrow(mod):
    source, reason = mod.choose_claim_source(
        explicit=None,
        signer_address=SIGNER,
        escrow_address=ESCROW,
        venft_owner=None,
        signer_is_approved_or_owner=None,
    )
    assert source == "escrow"
    assert "unreadable" in reason


def test_unreadable_ownership_without_escrow_falls_back_to_voter(mod):
    source, _ = mod.choose_claim_source(
        explicit=None,
        signer_address=SIGNER,
        escrow_address="",
        venft_owner=None,
        signer_is_approved_or_owner=None,
    )
    assert source == "voter"


def test_every_resolution_returns_a_valid_source(mod):
    """Postcondition: the returned source is always dispatchable by Phase 3."""
    for owner in (None, SIGNER, ESCROW, STRANGER):
        for approved in (None, True, False):
            for escrow in ("", ESCROW):
                source, reason = mod.choose_claim_source(
                    explicit=None,
                    signer_address=SIGNER,
                    escrow_address=escrow,
                    venft_owner=owner,
                    signer_is_approved_or_owner=approved,
                )
                assert source in mod.CLAIM_SOURCES
                assert reason
