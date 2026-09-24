"""Voting account resolution and the epoch-start voting power preflight.

Regression cover for epoch 1788998400: veNFT #19435's votes were delegated from the
PartnerEscrow to the signer on 2026-09-09. balanceOfNFT still showed the full 1.81M, but
VoterV5 counts getPastVotes(msg.sender, _epochTimestamp()), which was 0 for the escrow,
so every escrow vote reverted with InsufficientVotingPower().

Contract under test (src/voting_power.py):
  - VOTE_FROM picks both the voting account and the transaction target; bad config raises.
  - The check reads votes at the Voter's epoch start for the voting account.
  - Zero votes (or a failed read) is not ok and names the current delegate.
  - A shortfall against the configured power is reported but stays ok.
  - The check never raises.
"""

import pytest
from hexbytes import HexBytes
from web3 import Web3

from src import voting_power as vp

VOTER = "0xc69E3eF39E3fFBcE2A1c570f8d3ADF76909ef17b"
VE = "0x25B2ED7149fb8A05f6eF9407d9c8F878f59cd1e1"
ESCROW = "0x768a675B8542F23C428C6672738E380176E7635C"
SIGNER = "0xAB75E66C63307396FE8456Ea7c42CBBF3CF36298"
EPOCH_START = 1_788_998_400


def _sel(sig):
    return bytes(Web3.keccak(text=sig)[:4])


def _addr_word(a):
    return bytes.fromhex(a[2:].lower().rjust(64, "0"))


class FakeChain:
    """Answers the four reads the check makes; `votes` maps (account, epoch) -> wei."""

    def __init__(self, votes, delegates=None, fail=None):
        self.votes = {(a.lower(), e): v for (a, e), v in votes.items()}
        self.delegates = {k.lower(): v for k, v in (delegates or {}).items()}
        self.fail = fail
        self.calls = []

    def call(self, tx):
        # Real eth.call returns HexBytes (whose .hex() is 0x-prefixed here); match that.
        return HexBytes(self._answer(tx))

    def _answer(self, tx):
        data = bytes(tx["data"])
        to = tx["to"].lower()
        self.calls.append((to, data[:4]))
        if self.fail and data[:4] == _sel(self.fail):
            raise RuntimeError("rpc down")
        if to == VOTER.lower() and data[:4] == _sel("_ve()"):
            return _addr_word(VE)
        if to == VOTER.lower() and data[:4] == _sel("_epochTimestamp()"):
            return EPOCH_START.to_bytes(32, "big")
        if to == VE.lower() and data[:4] == _sel("getPastVotes(address,uint256)"):
            account = "0x" + data[4:36].hex()[-40:]
            epoch = int.from_bytes(data[36:68], "big")
            return self.votes.get((account, epoch), 0).to_bytes(32, "big")
        if to == VE.lower() and data[:4] == _sel("delegates(address)"):
            account = "0x" + data[4:36].hex()[-40:]
            return _addr_word(self.delegates.get(account, account))
        raise AssertionError(f"unexpected call to {to} selector {data[:4].hex()}")


class FakeW3:
    def __init__(self, chain):
        self.eth = chain


FULL = 1_813_774_782_668_000_000_000_000


def test_signer_mode_votes_from_signer_and_targets_voter():
    assert vp.resolve_voting_account("signer", ESCROW, SIGNER) == SIGNER
    assert vp.resolve_vote_target("signer", ESCROW, VOTER) == VOTER


def test_escrow_mode_votes_from_and_targets_escrow():
    assert vp.resolve_voting_account("escrow", ESCROW, SIGNER) == ESCROW
    assert vp.resolve_vote_target("escrow", ESCROW, VOTER) == ESCROW


@pytest.mark.parametrize(
    "mode,escrow,signer",
    [("partner", ESCROW, SIGNER), ("signer", ESCROW, ""), ("escrow", "", SIGNER)],
)
def test_misconfiguration_raises(mode, escrow, signer):
    with pytest.raises(ValueError):
        vp.resolve_voting_account(mode, escrow, signer)


def test_delegated_away_escrow_fails_and_names_delegate():
    """The 1788998400 incident: escrow has 0 epoch-start votes, signer holds them."""
    chain = FakeChain({(SIGNER, EPOCH_START): FULL}, delegates={ESCROW: SIGNER})
    ok, detail, votes = vp.check_epoch_voting_power(
        FakeW3(chain), VOTER, ESCROW, 1_813_743
    )
    assert ok is False and votes == 0
    assert "InsufficientVotingPower" in detail
    assert SIGNER in detail


def test_signer_with_delegated_votes_passes():
    chain = FakeChain({(SIGNER, EPOCH_START): FULL}, delegates={ESCROW: SIGNER})
    ok, detail, votes = vp.check_epoch_voting_power(
        FakeW3(chain), VOTER, SIGNER, 1_813_743
    )
    assert ok is True and votes == FULL
    assert "below" not in detail


def test_uses_votes_at_the_voter_epoch_start_not_another_epoch():
    """Votes held only at a previous epoch start must not count."""
    chain = FakeChain({(ESCROW, EPOCH_START - 604_800): FULL})
    ok, _detail, votes = vp.check_epoch_voting_power(
        FakeW3(chain), VOTER, ESCROW, 1_813_743
    )
    assert ok is False and votes == 0


def test_shortfall_is_reported_but_ok():
    chain = FakeChain({(SIGNER, EPOCH_START): 1_000_000 * 10**18})
    ok, detail, _votes = vp.check_epoch_voting_power(
        FakeW3(chain), VOTER, SIGNER, 1_813_743
    )
    assert ok is True
    assert "below the configured 1,813,743" in detail


@pytest.mark.parametrize(
    "failing", ["_ve()", "_epochTimestamp()", "getPastVotes(address,uint256)"]
)
def test_read_failure_is_not_ok_and_does_not_raise(failing):
    chain = FakeChain({(SIGNER, EPOCH_START): FULL}, fail=failing)
    ok, detail, votes = vp.check_epoch_voting_power(
        FakeW3(chain), VOTER, SIGNER, 1_813_743
    )
    assert ok is False and votes == 0
    assert "could not read voting power" in detail


def test_delegate_lookup_failure_does_not_block_a_passing_check():
    chain = FakeChain({(SIGNER, EPOCH_START): FULL}, fail="delegates(address)")
    ok, _detail, _votes = vp.check_epoch_voting_power(
        FakeW3(chain), VOTER, SIGNER, 1_813_743
    )
    assert ok is True
